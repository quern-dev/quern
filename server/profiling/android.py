"""Android profiles: perfetto on the device, read by trace_processor (#245, phase 4).

The same start_profile/stop_profile as iOS, recorded by the `perfetto` on the
device. Measured on a Pixel 3 XL (Android 10, no root):

- **The shell cannot signal perfetto** ("kill: Permission denied" -- it runs in
  its own SELinux domain), so a recording is a *detached* session, named by a
  key: `perfetto --detach=KEY` starts it and returns at once, with no client
  process left behind; `--is_detached=KEY` answers 0 while it runs and 2 once
  it has ended; `--attach=KEY --stop` stops it and writes the trace (0.26 s),
  and answers 2, "re-attach failed", for one already over.
- **The config goes in on stdin** (`--txt -c -`): perfetto cannot read a config
  file the shell wrote. It streams to its file as it records (`write_into_file`),
  so a 300 s profile is not cut to what a ring buffer holds.
- **The app is not relaunched.** perfetto records the whole system; the app
  keeps running, unlike under xctrace.

**A hang is input the app did not finish handling.** InputDispatcher keeps a
wait queue per window -- events sent and not yet acknowledged -- and atrace's
`input` category records it as a counter, `wq:<id> <package>/<activity>`.
The probe's 12 s main-thread sleep held its window's queue at 1 for 10,571.6
ms, against InputDispatcher's own "spent 10571.9ms processing the last input
event"; ordinary taps cleared it in 1-8 ms. An idle app's main thread also
sleeps for seconds, so a sleeping main thread proves nothing; unanswered input
does. Without trace_processor, the hangs are InputDispatcher's own log lines
(phase 2's crash-buffer entries), which need logcat captured.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from server.device import performance

#: A package name, as the API takes it: what reaches a device shell and SQL.
PACKAGE_OK = re.compile(r"^[A-Za-z0-9_.-]+$")
#: Where the shell may have perfetto write, and where it is read back from.
REMOTE_DIR = "/data/misc/perfetto-traces"
#: Unanswered input at least this long is a hang. Android's own thresholds are
#: higher -- InputDispatcher logs from 2 s, an ANR is 5 s -- and a 250 ms stall
#: is already one a user sees; it is also Instruments' threshold for a Hang.
HANG_MS = 250
#: A frame longer than this missed a 60 Hz deadline.
FRAME_BUDGET_MS = 1000 / 60
LIST_LIMIT = 50
#: An adb call to the device: perfetto's own answers measured 0.2-0.3 s.
ADB_TIMEOUT_S = 30.0
#: A trace this much (or a tenth) shorter than its profile stopped early.
TRACE_SHORT_S = 3.0
#: Pulling the trace: 4.1 MB took 0.14 s; a 300 s profile can be 100+ MB.
PULL_TIMEOUT_S = 300.0

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Preset:
    purpose: str
    atrace: tuple[str, ...]
    ftrace: tuple[str, ...]
    #: SurfaceFlinger's FrameTimeline (Android 12+): each frame judged
    #: against its deadline, with why it missed. Android 10 has no producer
    #: for it, and perfetto records the rest as before (measured).
    frame_timeline: bool = False


PRESETS: dict[str, Preset] = {
    "hangs": Preset(
        "input the app left unanswered (hangs), its frames, its CPU, and its slowest work",
        ("input", "view", "am", "gfx"),
        ("sched/sched_switch", "task/task_newtask", "task/task_rename"), frame_timeline=True),
    "frames": Preset(
        "how long each frame took, and the frames that missed their deadline, with why "
        "(Android 12+)",
        ("gfx", "view", "input"),
        ("sched/sched_switch", "task/task_newtask", "task/task_rename"), frame_timeline=True),
    "cpu": Preset(
        "where the app's CPU time went, by thread",
        ("view", "am"),
        ("sched/sched_switch", "sched/sched_wakeup", "power/cpu_frequency",
         "task/task_newtask", "task/task_rename")),
}


def session_key(profile_id: str) -> str:
    return "quern_" + profile_id


def remote_trace(profile_id: str) -> str:
    return f"{REMOTE_DIR}/quern-{profile_id}.pftrace"


def config_text(preset: Preset, package: str, time_limit_s: int) -> str:
    """perfetto's text config: streamed to its file, ended by its own time
    limit, with the app's atrace so its own slices are recorded.

    A ring buffer, flushed every second. A DISCARD buffer stops taking data
    once it has filled, even while it is being written out, so it capped the
    whole trace at its 32 MiB: measured on a Pixel 5 (Android 14), a 25 s
    profile kept 19 s, and trace_processor named the config as data loss.
    The same profile with this one kept all 25 s and reported none."""
    events = " ".join(f'ftrace_events: "{e}"' for e in preset.ftrace)
    categories = " ".join(f'atrace_categories: "{c}"' for c in preset.atrace)
    return (
        "buffers { size_kb: 32768 fill_policy: RING_BUFFER }\n"
        "write_into_file: true\n"
        "file_write_period_ms: 1000\n"
        "flush_period_ms: 1000\n"
        "max_file_size_bytes: 268435456\n"
        f"duration_ms: {time_limit_s * 1000}\n"
        'data_sources { config { name: "linux.ftrace" ftrace_config { '
        f'{events} {categories} atrace_apps: "{package}" }} }} }}\n'
        'data_sources { config { name: "linux.process_stats" process_stats_config { '
        "scan_all_processes_on_start: true } } }\n"
        + ('data_sources { config { name: "android.surfaceflinger.frametimeline" } }\n'
           if preset.frame_timeline else ""))


# -- talking to the device ------------------------------------------------------

#: (serial, args, stdin) -> (returncode, stdout, stderr). Raises OSError when adb
#: itself cannot be run, TimeoutError when it does not answer.
Runner = Callable[..., Awaitable[tuple[int, str, str]]]


def adb_runner(adb_path: Callable[[], str | None]) -> Runner:
    async def run(serial: str, *args: str, stdin: bytes | None = None,
                  timeout: float = ADB_TIMEOUT_S) -> tuple[int, str, str]:
        adb = adb_path()
        if not adb:
            raise OSError("adb was not found")
        proc = await asyncio.create_subprocess_exec(
            adb, "-s", serial, *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.shield(proc.wait())
            raise
        return (proc.returncode or 0, _ANSI.sub("", out.decode(errors="replace")),
                _ANSI.sub("", err.decode(errors="replace")))
    return run


#: How often a stop that met a busy session is tried again.
STOP_ATTEMPTS = 4


class Device:
    """One profile's perfetto session on an Android device, through adb.

    **Its calls to perfetto take turns.** `--is_detached` works by attaching
    to the session for a moment, and an `--attach --stop` that lands then is
    refused -- exit 2, "re-attach failed", the same answer as a session that
    is over. Measured: with `--is_detached` polled, the first stop got 2 and
    the next, 0.3 s later, 0. A profile's watch polls it every second, so its
    stop read as "already over" and recorded on to its time limit (live)."""

    def __init__(self, run: Runner, serial: str) -> None:
        self.run = run
        self.serial = serial
        self._turn = asyncio.Lock()

    async def start(self, profile_id: str, package: str, preset: Preset,
                    time_limit_s: int, log: Path) -> None:
        """Start the detached session, and confirm it is running: perfetto's
        exit status is the request, `--is_detached` is the outcome."""
        cfg = config_text(preset, package, time_limit_s).encode()
        rc, out, err = await self.run(
            self.serial, "shell",
            f"perfetto --detach={session_key(profile_id)} --txt -c - "
            f"-o {remote_trace(profile_id)}", stdin=cfg)
        said = (out + err).strip()
        with contextlib.suppress(OSError):
            log.write_text(f"$ perfetto --detach (exit {rc})\n{said}\n")
        if rc != 0:
            last = said.splitlines()[-1] if said else f"exit {rc}"
            raise StartFailed(f"perfetto did not start recording: {last}")
        running = await self.alive(profile_id)
        if running is not True:
            raise StartFailed(
                "perfetto said it started, but no session is running"
                if running is False else
                "perfetto said it started, and whether it is running could not be told")

    async def alive(self, profile_id: str) -> bool | None:
        """True while the session runs, False once over, None when the device
        could not be asked -- not the same answer as over."""
        async with self._turn:
            return await self._alive(profile_id)

    async def _alive(self, profile_id: str) -> bool | None:
        try:
            rc, _out, _err = await self.run(
                self.serial, "shell", f"perfetto --is_detached={session_key(profile_id)}")
        except (OSError, TimeoutError):
            return None
        return True if rc == 0 else False if rc == 2 else None

    async def stop(self, profile_id: str) -> str:
        """"sent", "gone" (already over), or "unknown" (could not ask). An
        exit 2 is checked, not believed: it is also what a session busy with
        another attach answers, so it is "gone" only if the session is."""
        async with self._turn:
            for attempt in range(STOP_ATTEMPTS):
                try:
                    rc, _out, _err = await self.run(
                        self.serial, "shell",
                        f"perfetto --attach={session_key(profile_id)} --stop")
                except (OSError, TimeoutError):
                    return "unknown"
                if rc == 0:
                    return "sent"
                if rc != 2:
                    return "unknown"
                running = await self._alive(profile_id)
                if running is False:
                    return "gone"
                if running is None:
                    return "unknown"
                await asyncio.sleep(0.3 * (attempt + 1))   # busy: someone else attached
            return "unknown"

    async def pull(self, profile_id: str, dest: Path) -> None:
        """Copy the trace off the device, then remove it there. Into a
        temporary name first: `dest` existing means the whole trace arrived,
        so a finish cut off after this (a restart) does not pull again a file
        already removed from the device (review)."""
        remote = remote_trace(profile_id)
        part = dest.with_name(dest.name + ".part")
        rc, out, err = await self.run(self.serial, "pull", remote, str(part),
                                      timeout=PULL_TIMEOUT_S)
        if rc != 0 or not part.is_file() or part.stat().st_size == 0:
            with contextlib.suppress(OSError):
                part.unlink()
            raise PullFailed(f"the trace could not be copied off the device: "
                             f"{(err or out).strip() or f'adb pull exit {rc}'}; it is still "
                             f"on the device at {remote}")
        part.replace(dest)
        await self.remove(profile_id)

    async def remove(self, profile_id: str) -> None:
        """Remove the session's trace from the device; best effort."""
        with contextlib.suppress(OSError, TimeoutError):
            await self.run(self.serial, "shell", f"rm -f {remote_trace(profile_id)}")

    async def snapshot(self, package: str) -> dict:
        """The app's pid, CPU ticks with the device's uptime, and gfxinfo's
        cumulative frame counts -- what a profile without trace_processor is
        measured from. Each part null, with why, when it could not be read."""
        snap: dict = {"pid": None, "ticks": None, "uptime": None, "frames": None,
                      "clock_offset_s": None, "unavailable": {}}
        try:
            # The device's clock against the host's, for the logged hangs,
            # which logcat stamps with the device's time.
            before = time.time()
            rc, out, _err = await self.run(self.serial, "shell", "date +%s.%N")
            with contextlib.suppress(ValueError):
                snap["clock_offset_s"] = round(float(out.strip()) - (before + time.time()) / 2, 3)
            rc, out, err = await self.run(self.serial, "shell", "pidof", package)
            pids = [int(p) for p in out.split() if p.isdigit()]
            if not pids:
                why = ("the app was not running" if rc in (0, 1) and not err.strip()
                       else f"the device could not say whether the app was running: pidof: "
                            f"{(err or out).strip() or f'exit {rc}'}")
                snap["unavailable"] = {"cpu": why, "frames": why}
                return snap
            pid = snap["pid"] = pids[0]
            rc, out, err = await self.run(self.serial, "shell",
                                          f"cat /proc/uptime /proc/{pid}/stat")
            lines = out.splitlines()
            if len(lines) >= 2:
                with contextlib.suppress(ValueError, IndexError):
                    snap["uptime"] = float(lines[0].split()[0])
                snap["ticks"] = performance.parse_proc_stat_ticks(lines[1])
            if snap["ticks"] is None or snap["uptime"] is None:
                snap["unavailable"]["cpu"] = f"/proc/{pid} could not be read: {(err or out)[:200]}"
            rc, out, err = await self.run(self.serial, "shell", "dumpsys", "gfxinfo", package)
            frames = performance.parse_gfxinfo(out, pid)
            if frames is None:
                snap["unavailable"]["frames"] = "gfxinfo reported no frames for the app"
            else:
                snap["frames"] = frames.model_dump()
        except (OSError, TimeoutError) as e:
            why = f"the device could not be asked: {e!r}"
            for part in ("cpu", "frames"):
                snap["unavailable"].setdefault(part, why)
        return snap


class StartFailed(Exception):
    pass


class PullFailed(Exception):
    pass


# -- summarising ----------------------------------------------------------------

def _rel(ns: int | float, start_ns: int | float) -> float:
    return round((ns - start_ns) / 1e9, 3)


def window_matches(track: str, package: str) -> tuple[bool, bool]:
    """Whether a `wq:<id> <window>` counter is one of the app's windows, and
    whether only by a cut name. The name is cut at about 40 characters, so a
    long package can arrive cut -- and then the cut also fits any other
    package that begins the same way (a debug build beside the release one),
    which the caller says rather than hides."""
    window = track.split(" ", 1)[1] if " " in track else ""
    window = window.removesuffix(" (server)")
    if window.startswith(package + "/") or window == package:
        return True, False
    cut = len(track) >= 39 and "/" not in window and len(window) >= 8
    return (cut and package.startswith(window)), cut and package.startswith(window)


#: Lost data is what trace_processor rates `data_loss`, and two it rates only
#: `info` that are data lost all the same: the kernel's ftrace buffer
#: overrunning, and the ring buffer overwriting chunks not yet written out
#: (review).
#:
#: The app's FrameTimeline frames are its process's, or its layer's -- "TX -
#: <package>/<activity>#n" -- for a trace that lost the process names: a
#: Pixel 5's DISCARD trace had 1,355 of the app's frames with no process name.
#:
#: A frame is `Choreographer#doFrame` on Android 10 and `Choreographer#doFrame
#: <vsync id>` on 14 (measured), where its child "Choreographer#doFrame -
#: resynced to ..." is not a frame of its own.
#:
#: The whole summary in one query, so trace_processor loads the trace once:
#: each section was a load of its own, measured 1.7 s for 4.1 MB, so a 100 MB
#: trace would have cost five loads of ~40 s. Each section is JSON, hex-encoded
#: -- trace_processor prints values quoted but does not escape the quotes
#: inside them, so raw JSON does not survive its CSV.
SUMMARY_SQL = """
select
  (select start_ts from trace_bounds) as start_ts,
  (select end_ts from trace_bounds) as end_ts,
  (select count(*) from process where name = '{package}') as procs,
  hex((select json_group_array(json_array(track, ts, value)) from (
     select t.name as track, c.ts as ts, c.value as value
     from counter c join counter_track t on c.track_id = t.id
     where t.name like 'wq:%' order by t.name, c.ts))) as wq,
  hex((select json_group_array(s.dur) from slice s
     join thread_track tt on s.track_id = tt.id join thread t using (utid)
     join process p using (upid)
     where p.name = '{package}' and t.tid = p.pid
       and (s.name = 'Choreographer#doFrame' or s.name glob 'Choreographer#doFrame [0-9]*')))
     as frames,
  hex((select json_group_array(json_array(tid, thread, ns)) from (
     select t.tid as tid, t.name as thread, sum(s.dur) as ns
     from sched s join thread t using (utid) join process p using (upid)
     where p.name = '{package}' group by utid order by ns desc))) as cpu,
  hex((select json_group_array(json_array(name, thread, dur, ts)) from (
     select s.name as name, t.name as thread, s.dur as dur, s.ts as ts
     from slice s join thread_track tt on s.track_id = tt.id
     join thread t using (utid) join process p using (upid)
     where p.name = '{package}' order by s.dur desc limit 10))) as slices,
  hex((select json_group_array(json_array(name, idx, value)) from stats
     where value > 0 and (severity = 'data_loss'
       or name in ('ftrace_cpu_overrun_end', 'traced_buf_chunks_overwritten')))) as loss,
  hex((select json_group_array(json_array(ts, dur, present_type, jank_type, jank_tag)) from (
     select f.ts as ts, f.dur as dur, f.present_type as present_type, f.jank_type as jank_type,
       f.jank_tag as jank_tag
     from actual_frame_timeline_slice f left join process p using (upid)
     where p.name = '{package}' or f.layer_name glob 'TX - {package}/*'
     order by f.ts))) as ft,
  (select count(*) from actual_frame_timeline_slice) as ft_any
"""


def decode_summary_row(row: dict) -> dict:
    """The one row SUMMARY_SQL gives, its hex sections decoded."""
    out = {k: row.get(k) for k in ("start_ts", "end_ts", "procs", "ft_any")}
    for key in ("wq", "frames", "cpu", "slices", "loss", "ft"):
        raw = row.get(key)
        out[key] = json.loads(bytes.fromhex(raw).decode()) if raw else []
    return out


def hangs_from_counters(wq: list, package: str, start_ns: int, end_ns: int) -> list[dict]:
    """Stretches during which one of the app's windows had input waiting,
    `HANG_MS` or longer.

    A stretch runs from the queue leaving 0 to its return to 0, whatever it
    does between: a busy window is sent more events until its oldest has
    waited 500 ms, so its queue can climb 1, 2, 3 during one hang. Judging each
    counter value alone split one hang into pieces too short to count
    (review)."""
    by_track: dict[str, list] = {}
    for track, ts, value in wq:
        by_track.setdefault(str(track), []).append((ts, value))
    out = []
    for track, points in by_track.items():
        mine, cut = window_matches(track, package)
        if not mine:
            continue
        began = None
        for ts, value in sorted(points):
            if value and began is None:
                began = ts
            elif not value and began is not None:
                out.append(_hang(track, began, ts, start_ns, cut, open_=False))
                began = None
        if began is not None:
            out.append(_hang(track, began, end_ns, start_ns, cut, open_=True))
    return sorted((h for h in out if h["duration_ms"] >= HANG_MS),
                  key=lambda h: h["start_s"])


def _hang(track: str, began: int, until: int, start_ns: int, cut: bool, *, open_: bool) -> dict:
    hang = {"start_s": _rel(began, start_ns), "duration_ms": round((until - began) / 1e6, 1),
            "window": track.split(" ", 1)[-1]}
    notes = []
    if open_:
        notes.append("still unanswered when the profile ended")
    if cut:
        notes.append("the window's name was cut short; another app whose package begins "
                     "the same way would match it too")
    if notes:
        hang["note"] = "; ".join(notes)
    return hang


def frames_from_durations(durations_ns: list[int]) -> dict | None:
    if not durations_ns:
        return None
    ms = sorted(d / 1e6 for d in durations_ns)

    def pct(p: float) -> float:
        return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1)
    return {"count": len(ms), "mean_ms": round(statistics.fmean(ms), 2),
            "p50_ms": pct(0.5), "p90_ms": pct(0.9), "p99_ms": pct(0.99),
            "max_ms": round(ms[-1], 1),
            "over_budget": sum(1 for m in ms if m > FRAME_BUDGET_MS),
            "budget_ms": round(FRAME_BUDGET_MS, 1), "source": "trace",
            "measures": "Choreographer#doFrame on the app's main thread"}


#: FrameTimeline's presentations of a frame that reached the display.
PRESENTED = {"On-time Present": "on_time", "Late Present": "late", "Early Present": "early"}
#: Whose jank a frame is, by trace_processor's own reading of it (jank_tag),
#: not by its causes: perfetto counts only App Deadline Missed and App
#: Resynced Jitter as the app's, and a stuffed frame -- one queued behind
#: others -- is smooth with added latency, not jank (review, against the
#: v59.1 stdlib). The first version read Buffer Stuffing as the app's jank,
#: and a Pixel 5's smooth scroll as 56 frames janky by its own fault.
JANKY_TAGS = {"Self Jank": "app", "Other Jank": "system"}
STUFFED_TAGS = ("Buffer Stuffing", "SurfaceFlinger Stuffing")
SMOOTH_TAGS = ("No Jank", "Non-perceivable Jank")
#: A frame that missed: late to the display, or never on it.
MISSED = ("Late Present", "Dropped Frame")


def _causes(jank_type: object) -> list[str]:
    """FrameTimeline writes several causes as one string ("Prediction Error,
    App Deadline Missed", measured), and "None" for a frame on time."""
    return [c.strip() for c in str(jank_type or "").split(",")
            if c.strip() and c.strip() != "None"]


def jank_from_timeline(rows: list, start_ns: int) -> dict | None:
    """The app's frames as SurfaceFlinger judged them: on time, late or
    dropped; which were janky, whose fault, and why; and the worst of them.

    A frame is janky when trace_processor tags it Self (the app's) or Other
    (the system's) Jank, and also when it was late or dropped with no tag it
    knows -- a missed frame without a reason is still missed, so it can
    never read as smooth. A frozen main thread draws nothing, so a hang
    leaves no janky frames behind it: measured, a 12 s hang on a Pixel 5
    left a gap and two late frames after it."""
    if not rows:
        return None
    present = {"on_time": 0, "late": 0, "early": 0}
    dropped = unknown = classified = janky = app_janky = stuffed = unexplained = cut = 0
    by_cause: dict[str, int] = {}
    durations: list[float] = []
    worst: list[dict] = []
    for ts, dur, present_type, jank_type, jank_tag in rows:
        if present_type == "Dropped Frame":
            dropped += 1
        elif present_type in PRESENTED:
            present[PRESENTED[present_type]] += 1
            if dur is not None and dur >= 0:
                durations.append(dur / 1e6)
            else:
                cut += 1                  # still on its way when the trace ended
        else:
            unknown += 1
        known = jank_tag in JANKY_TAGS or jank_tag in STUFFED_TAGS or jank_tag in SMOOTH_TAGS
        classified += known
        if jank_tag in STUFFED_TAGS and present_type != "Dropped Frame":
            stuffed += 1
            continue
        whose = JANKY_TAGS.get(jank_tag)
        if whose is None and (present_type == "Dropped Frame"
                              or (not known and present_type in MISSED)):
            whose = "unexplained"
        if whose is None:
            continue
        janky += 1
        app_janky += whose == "app"
        unexplained += whose == "unexplained"
        causes = _causes(jank_type) or [present_type]
        for c in causes:
            by_cause[c] = by_cause.get(c, 0) + 1
        worst.append({"start_s": _rel(ts, start_ns),
                      "duration_ms": round(dur / 1e6, 1) if dur is not None and dur >= 0 else None,
                      "present": present_type, "whose": whose, "causes": causes})
    presented = sum(present.values())
    ms = sorted(durations)

    def pct(p: float) -> float | None:
        return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1) if ms else None
    worst.sort(key=lambda f: -(f["duration_ms"] or 0))
    return {
        "frames": len(rows), "presented": presented, **present, "dropped": dropped,
        "unknown_present": unknown, "unfinished": cut,
        "janky": janky, "jank_percent": round(janky / len(rows) * 100, 1),
        "app_janky": app_janky, "system_janky": janky - app_janky - unexplained,
        "unexplained_janky": unexplained, "stuffed": stuffed,
        "classified": classified,
        "classification_coverage_percent": round(classified / len(rows) * 100, 1),
        "by_cause": dict(sorted(by_cause.items(), key=lambda kv: -kv[1])),
        "p50_ms": pct(0.5), "p90_ms": pct(0.9), "p99_ms": pct(0.99),
        "max_ms": round(ms[-1], 1) if ms else None,
        "janky_frames": worst[:10], "source": "trace: FrameTimeline",
        "measures": ("each frame the app gave the display, as SurfaceFlinger judged it against "
                     "its deadline and trace_processor tagged it; duration is from the app "
                     "starting the frame to its presentation"),
        "note": ("jank_percent is janky frames of all the app's frames: Self Jank is the app's "
                 "(app_janky), Other Jank the system's, and a late or dropped frame with no "
                 "tag is unexplained_janky; stuffed frames were queued behind others -- added "
                 "latency, not jank; a frozen main thread draws no frames, so a hang is in "
                 "hangs, not here")}


def cpu_from_threads(rows: list, span_ns: int) -> dict | None:
    if not rows:
        return None
    total = sum(ns for _tid, _name, ns in rows)
    return {"cpu_percent": round(total / span_ns * 100, 1) if span_ns else None,
            "cpu_ms": round(total / 1e6, 1), "source": "trace",
            "threads": [{"tid": tid, "thread": name, "cpu_ms": round(ns / 1e6, 1)}
                        for tid, name, ns in rows[:10]]}


def slices_from_rows(rows: list, start_ns: int) -> list[dict] | None:
    return [{"name": name, "thread": thread, "duration_ms": round(dur / 1e6, 1),
             "start_s": _rel(ts, start_ns)} for name, thread, dur, ts in rows] or None


def frames_from_snapshots(first: dict, last: dict) -> tuple[dict | None, str | None]:
    a, b = first.get("frames"), last.get("frames")
    if not a or not b:
        return None, (last.get("unavailable") or {}).get("frames") \
            or (first.get("unavailable") or {}).get("frames") or "gfxinfo was not read"
    if first.get("pid") != last.get("pid"):
        return None, "the app restarted during the profile; gfxinfo's counts started again"
    total = b["total_frames"] - a["total_frames"]
    if total < 0:
        return None, "gfxinfo's frame count went down: its stats were reset during the profile"
    janky = None
    if (a.get("janky_frames") is not None and b.get("janky_frames") is not None
            and a.get("janky_method") == b.get("janky_method")):
        janky = b["janky_frames"] - a["janky_frames"]
    return ({"count": total, "janky": janky,
             "janky_method": b.get("janky_method") if janky is not None else None,
             "source": "gfxinfo"}, None)


def cpu_from_snapshots(first: dict, last: dict) -> tuple[dict | None, str | None]:
    if first.get("ticks") is None or last.get("ticks") is None:
        return None, (last.get("unavailable") or {}).get("cpu") \
            or (first.get("unavailable") or {}).get("cpu") or "/proc was not read"
    if first.get("pid") != last.get("pid"):
        return None, "the app restarted during the profile; its CPU time started again"
    seconds = last["uptime"] - first["uptime"]
    if seconds <= 0:
        return None, "the device's uptime did not advance"
    ticks = last["ticks"] - first["ticks"]
    return ({"cpu_percent": round(ticks / performance.USER_HZ / seconds * 100, 1),
             "cpu_ms": round(ticks / performance.USER_HZ * 1000, 1), "source": "/proc"}, None)


async def summarize(*, preset: str, package: str, trace: Path, first: dict, last: dict,
                    tp, tp_status: dict,
                    logged_hangs: tuple[list[dict] | None, str | None],
                    expected_s: float | None = None) -> dict:
    """The profile, section by section; null with the reason when unread.

    trace_processor failing is not the trace being bad: the summary falls
    back to what the device reported, says why, and the profile is still a
    result with its trace offered (review: it was failed, and the trace
    withheld)."""
    summary: dict = {"preset": preset, "platform": "android", "hangs": None, "frames": None,
                     "frame_timeline": None, "cpu": None, "slowest_slices": None,
                     "trace_processor": tp_status, "unavailable": {}}
    unavailable = summary["unavailable"]
    read = None
    if tp_status.get("state") == "installed":
        try:
            [row] = await tp.query(trace, SUMMARY_SQL.format(package=package))
            read = decode_summary_row(row)
        except Exception as e:  # noqa: BLE001 -- said; the device's readings stand in
            unavailable["trace_processor"] = (
                f"trace_processor could not read the trace ({e!r}); this summary is what "
                "the device reported. The trace is kept: download it, or open it at "
                "ui.perfetto.dev")
    else:
        unavailable["trace_processor"] = _why_not(tp_status)

    if read is not None:
        start_ns, end_ns = read["start_ts"] or 0, read["end_ts"] or 0
        loss = [{"stat": name if idx is None else f"{name}[{idx}]", "value": value}
                 for name, idx, value in read["loss"]]
        span_s = (end_ns - start_ns) / 1e9
        if expected_s and expected_s - span_s > max(TRACE_SHORT_S, expected_s * 0.1):
            # A trace that stops early says nothing in its stats: perfetto
            # stops at max_file_size_bytes, which a busy device reaches in
            # ~130 s (Pixel 5: 48 MB per 25 s), and a 300 s profile is allowed.
            loss.append({"stat": "trace ended early", "value": round(span_s, 1),
                         "detail": f"the trace covers {span_s:.1f} s of a {expected_s:.1f} s "
                                   "profile; perfetto stops writing at its 256 MiB file limit"})
        if loss:
            # The sections below are then read from a trace with holes in it.
            summary["trace_data_loss"] = loss
            summary["trace_note"] = (
                "trace_processor says the trace lost data, so what it holds is incomplete: "
                "counts read from it are lower bounds, and a gap is not proof of nothing "
                "happening")
        if not read["procs"]:
            unavailable["app"] = f"{package} did not run during the profile"
        if preset == "cpu":
            unavailable["hangs"] = "not recorded by the cpu preset (no input tracing)"
        elif not read["wq"]:
            # No window's input queue at all, the app's or any other: the
            # trace did not record them, which is not "no hangs" (CodeRabbit).
            # Measured on Android 10; a later version may name them otherwise.
            unavailable["hangs"] = (
                "the trace has no InputDispatcher wait-queue counters (wq:) for any window, "
                "so hangs could not be read from it; this Android version may not record "
                "them under that name")
        else:
            hangs = hangs_from_counters(read["wq"], package, start_ns, end_ns)
            summary.update(hangs=hangs[:LIST_LIMIT], hangs_count=len(hangs),
                           hangs_source="trace: input the app left unanswered")
        summary["frames"] = frames_from_durations(read["frames"])
        if summary["frames"] is None:
            unavailable["frames"] = "the trace has no frames from the app's main thread"
        if not PRESETS[preset].frame_timeline:
            unavailable["frame_timeline"] = f"not recorded by the {preset} preset"
        else:
            summary["frame_timeline"] = jank_from_timeline(read["ft"], start_ns)
            if summary["frame_timeline"] is None:
                unavailable["frame_timeline"] = (
                    "the app gave the display no frames during the profile (FrameTimeline "
                    "recorded other apps')" if read["ft_any"] else
                    "the trace has no FrameTimeline frames at all: it needs Android 12 or "
                    "later, and a screen that changed")
        summary["cpu"] = cpu_from_threads(read["cpu"], end_ns - start_ns)
        if summary["cpu"] is None:
            unavailable["cpu"] = "the trace has no CPU time for the app"
        summary["slowest_slices"] = slices_from_rows(read["slices"], start_ns)
        if summary["slowest_slices"] is None:
            unavailable["slowest_slices"] = "the trace has no slices from the app"
    else:
        unavailable["slowest_slices"] = "read from the trace, which needs trace_processor"
        unavailable["frame_timeline"] = "read from the trace, which needs trace_processor"

    # What the device said, for whatever the trace did not answer.
    if summary["hangs"] is None:
        hangs, why = logged_hangs
        if hangs is not None:
            unavailable.pop("hangs", None)
            summary.update(hangs=hangs[:LIST_LIMIT], hangs_count=len(hangs),
                           hangs_source=("logcat: InputDispatcher's own reports, from 2 s; "
                                         "shorter hangs need trace_processor"))
            if why:
                summary["hangs_note"] = why
        else:
            unavailable.setdefault("hangs", why or "no source for hangs")
    if summary["frames"] is None:
        frames, why = frames_from_snapshots(first, last)
        if frames is not None:
            unavailable.pop("frames", None)
            summary["frames"] = frames
        else:
            unavailable.setdefault("frames", why)
    if summary["cpu"] is None:
        cpu, why = cpu_from_snapshots(first, last)
        if cpu is not None:
            unavailable.pop("cpu", None)
            summary["cpu"] = cpu
        else:
            unavailable.setdefault("cpu", why)
    return summary


def _why_not(status: dict) -> str:
    state, detail = status.get("state"), status.get("detail")
    return {
        "downloading": f"trace_processor was still downloading ({detail}); this summary is "
                       "what the device reported. The trace is kept: download it, or open it "
                       "at ui.perfetto.dev",
        "failed": f"{detail}; this summary is what the device reported",
        "disabled": "trace_processor is turned off (config.json profiles.trace_processor); "
                    "this summary is what the device reported",
        "unsupported": f"{detail}; this summary is what the device reported",
    }.get(state, f"trace_processor is {state}; this summary is what the device reported")
