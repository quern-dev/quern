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
import re
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from server.device import performance

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
#: Pulling the trace: 4.1 MB took 0.14 s; a 300 s profile can be 100+ MB.
PULL_TIMEOUT_S = 300.0

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Preset:
    purpose: str
    atrace: tuple[str, ...]
    ftrace: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    "hangs": Preset(
        "input the app left unanswered (hangs), its frames, its CPU, and its slowest work",
        ("input", "view", "am", "gfx"),
        ("sched/sched_switch", "task/task_newtask", "task/task_rename")),
    "frames": Preset(
        "how long each frame took, and the frames that missed their deadline",
        ("gfx", "view", "input"),
        ("sched/sched_switch", "task/task_newtask", "task/task_rename")),
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
    limit, with the app's atrace so its own slices are recorded."""
    events = " ".join(f'ftrace_events: "{e}"' for e in preset.ftrace)
    categories = " ".join(f'atrace_categories: "{c}"' for c in preset.atrace)
    return (
        "buffers { size_kb: 32768 fill_policy: DISCARD }\n"
        "write_into_file: true\n"
        "file_write_period_ms: 1000\n"
        "max_file_size_bytes: 536870912\n"
        f"duration_ms: {time_limit_s * 1000}\n"
        'data_sources { config { name: "linux.ftrace" ftrace_config { '
        f'{events} {categories} atrace_apps: "{package}" }} }} }}\n'
        'data_sources { config { name: "linux.process_stats" process_stats_config { '
        "scan_all_processes_on_start: true } } }\n")


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
        """Copy the trace off the device, then remove it there."""
        remote = remote_trace(profile_id)
        rc, out, err = await self.run(self.serial, "pull", remote, str(dest),
                                      timeout=PULL_TIMEOUT_S)
        if rc != 0 or not dest.is_file() or dest.stat().st_size == 0:
            raise PullFailed(f"the trace could not be copied off the device: "
                             f"{(err or out).strip() or f'adb pull exit {rc}'}")
        with contextlib.suppress(OSError, TimeoutError):
            await self.run(self.serial, "shell", f"rm -f {remote}")

    async def snapshot(self, package: str) -> dict:
        """The app's pid, CPU ticks with the device's uptime, and gfxinfo's
        cumulative frame counts -- what a profile without trace_processor is
        measured from. Each part null, with why, when it could not be read."""
        snap: dict = {"pid": None, "ticks": None, "uptime": None, "frames": None,
                      "unavailable": {}}
        try:
            rc, out, err = await self.run(self.serial, "shell", "pidof", package)
            pids = [int(p) for p in out.split() if p.isdigit()]
            if not pids:
                why = ("the app was not running" if rc in (0, 1) and not err.strip()
                       else f"pidof failed: {(err or out).strip()}")
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


def window_matches(track: str, package: str) -> bool:
    """Whether a `wq:<id> <window>` counter is one of the app's windows. The
    name is cut at about 40 characters, so a long package can arrive cut."""
    window = track.split(" ", 1)[1] if " " in track else ""
    window = window.removesuffix(" (server)")
    if window.startswith(package + "/") or window == package:
        return True
    cut = len(track) >= 39 and "/" not in window and len(window) >= 8
    return cut and package.startswith(window)


HANGS_SQL = """
select t.name as track, c.ts as ts, c.value as value,
       coalesce(lead(c.ts) over (partition by c.track_id order by c.ts),
                (select end_ts from trace_bounds)) as until,
       lead(c.ts) over (partition by c.track_id order by c.ts) is null as open,
       (select start_ts from trace_bounds) as start_ts
from counter c join counter_track t on c.track_id = t.id
where t.name like 'wq:%'
"""

FRAMES_SQL = """
select s.dur as dur from slice s
join thread_track tt on s.track_id = tt.id join thread t using (utid) join process p using (upid)
where p.name = '{package}' and t.tid = p.pid and s.name = 'Choreographer#doFrame'
"""

CPU_SQL = """
select t.tid as tid, t.name as thread, sum(s.dur) as ns,
       (select end_ts - start_ts from trace_bounds) as span
from sched s join thread t using (utid) join process p using (upid)
where p.name = '{package}' group by utid order by ns desc
"""

SLICES_SQL = """
select s.name as name, t.name as thread, s.dur as dur, s.ts as ts,
       (select start_ts from trace_bounds) as start_ts
from slice s join thread_track tt on s.track_id = tt.id
join thread t using (utid) join process p using (upid)
where p.name = '{package}' order by s.dur desc limit 10
"""

PROCESS_SQL = "select count(*) as n from process where name = '{package}'"


def hangs_from_counters(rows: list[dict], package: str) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("value") or not window_matches(str(r.get("track")), package):
            continue
        dur_ms = (r["until"] - r["ts"]) / 1e6
        if dur_ms < HANG_MS:
            continue
        hang = {"start_s": _rel(r["ts"], r["start_ts"]), "duration_ms": round(dur_ms, 1),
                "window": str(r["track"]).split(" ", 1)[-1]}
        if r.get("open"):
            hang["note"] = "still unanswered when the profile ended"
        out.append(hang)
    return sorted(out, key=lambda h: h["start_s"])


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


def cpu_from_threads(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    span = rows[0].get("span") or 0
    total = sum(r["ns"] for r in rows)
    return {"cpu_percent": round(total / span * 100, 1) if span else None,
            "cpu_ms": round(total / 1e6, 1), "source": "trace",
            "threads": [{"tid": r["tid"], "thread": r["thread"],
                         "cpu_ms": round(r["ns"] / 1e6, 1)} for r in rows[:10]]}


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
                    tp, tp_status: dict, logged_hangs: tuple[list[dict] | None, str | None],
                    ) -> dict:
    """The profile, section by section; null with the reason when unread."""
    summary: dict = {"preset": preset, "platform": "android", "hangs": None, "frames": None,
                     "cpu": None, "slowest_slices": None, "trace_processor": tp_status,
                     "unavailable": {}}
    unavailable = summary["unavailable"]
    deep = tp_status.get("state") == "installed"
    if deep:
        try:
            if not (await tp.query(trace, PROCESS_SQL.format(package=package)))[0]["n"]:
                unavailable["app"] = f"{package} did not run during the profile"
        except Exception as e:  # noqa: BLE001 -- the trace itself could not be read
            deep = False
            unavailable["trace"] = f"trace_processor could not read the trace: {e}"
    if not deep and "trace" not in unavailable:
        unavailable["trace_processor"] = _why_not(tp_status)

    if deep:
        hangs = await _section(tp, trace, HANGS_SQL, package, unavailable, "hangs",
                               lambda rows: hangs_from_counters(rows, package))
        if hangs is not None and preset == "cpu":
            hangs = None
            unavailable["hangs"] = "not recorded by the cpu preset (no input tracing)"
        if hangs is not None:
            summary["hangs_count"] = len(hangs)
            summary["hangs"] = hangs[:LIST_LIMIT]
            summary["hangs_source"] = "trace: input the app left unanswered"
        summary["frames"] = await _section(tp, trace, FRAMES_SQL, package, unavailable, "frames",
                                           lambda rows: frames_from_durations(
                                               [r["dur"] for r in rows]))
        summary["cpu"] = await _section(tp, trace, CPU_SQL, package, unavailable, "cpu",
                                        cpu_from_threads)
        summary["slowest_slices"] = await _section(
            tp, trace, SLICES_SQL, package, unavailable, "slowest_slices",
            lambda rows: [{"name": r["name"], "thread": r["thread"],
                           "duration_ms": round(r["dur"] / 1e6, 1),
                           "start_s": _rel(r["ts"], r["start_ts"])} for r in rows] or None)
    else:
        unavailable["slowest_slices"] = "read from the trace, which needs trace_processor"

    # What the device said, for whatever the trace did not answer.
    if summary["hangs"] is None:
        hangs, why = logged_hangs
        if hangs is not None:
            unavailable.pop("hangs", None)
            summary["hangs"] = hangs[:LIST_LIMIT]
            summary["hangs_count"] = len(hangs)
            summary["hangs_source"] = ("logcat: InputDispatcher's own reports, from 2 s; "
                                       "shorter hangs need trace_processor")
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


async def _section(tp, trace: Path, sql: str, package: str, unavailable: dict, name: str,
                   reduce: Callable[[list[dict]], object]) -> object:
    try:
        rows = await tp.query(trace, sql.format(package=package))
    except Exception as e:  # noqa: BLE001 -- one section never sinks the others
        unavailable[name] = f"trace_processor could not read {name}: {e}"
        return None
    value = reduce(rows)
    if value is None:
        unavailable[name] = {"frames": "the trace has no frames from the app's main thread",
                             "cpu": "the trace has no CPU time for the app",
                             "slowest_slices": "the trace has no slices from the app"}.get(
            name, "nothing recorded")
    return value


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
