"""What quern asks xctrace for, and how it reads the answer back (#245).

**Recording always launches the app** (`--launch -- <bundle id>`). Attaching
to a running app was measured broken on a physical iPhone (iOS 26.7, Xcode
27.2): "Cannot find process" for any name or pid, SpringBoard included, while
an all-processes recording of the same moment saw it. So a profile relaunches
the app, and xctrace terminates it when the recording stops. The response
says so, because an agent mid-flow would otherwise lose its place silently.

**Exports are XML that deduplicates.** A value seen once is written in full
with an `id`; later uses are `<tag ref="id"/>`. Every reader here resolves
refs before looking at a field, or it reads empty values out of a full table.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: The xctrace output that means the recording has begun, and any of these
#: before it means it never will. Measured on Xcode 27.2.
STARTED_MARKER = "Ctrl-C to stop the recording"
FAILURE_MARKERS = (
    "Timed out waiting for device to boot",
    "is not supported on this platform",
    "Cannot find process",
    "Recording failed",
    "Unable to launch",
    "Failed to launch",
)

#: xctrace has stopped recording and is writing the trace, then has written
#: it. The bundle exists from the start of recording, so only this line says
#: a trace is whole. Measured on Xcode 27.2: a time limit prints "Reached
#: specified time limit", a ^C "Stopping recording..." -- seconds before
#: "Saving output file" on an iPhone.
SAVING_MARKERS = ("Reached specified time limit", "Stopping recording", "Saving output file")
SAVED_MARKER = "Output file saved as"

#: How a launch path tells an app's own code from the system's: an app's
#: binary sits in its container on both platforms -- `.../data/Containers/...`
#: on a simulator, `/private/var/containers/...` on a device -- so the match
#: ignores case.
APP_CONTAINER = "/containers/bundle/application/"


@dataclass(frozen=True)
class Preset:
    template: str
    instruments: tuple[str, ...]
    #: Simulators refuse some templates outright ("Hitches is not supported
    #: on this platform", measured), so those are refused before launching.
    on_simulator: bool
    #: What it is for, in the words an agent picks a preset by.
    purpose: str


PRESETS: dict[str, Preset] = {
    # System Call Trace for a hang the thread spent blocked rather than busy:
    # without it, Time Profiler sees nothing (it samples running threads) and
    # triage's system calls read "N/A". Measured: it named the probe's 12 s
    # `sleepForTimeInterval:` down to __semwait_signal, and cost +0.8 MB per
    # 30 s on an iPhone 11 (3.2 to 4.0), +4.1 MB per 40 s on a simulator,
    # with save and triage times unchanged.
    "hangs": Preset("Time Profiler", ("Hangs", "System Call Trace"), True,
                    "main-thread hangs, each with what the thread was doing: its CPU samples "
                    "if busy, the system call it waited in if blocked"),
    "cpu": Preset("Time Profiler", (), True, "where CPU time goes"),
    "hitches": Preset("Animation Hitches", (), False,
                      "dropped or late frames, with Instruments' explanation of each"),
    "launch": Preset("App Launch", (), True, "what the app does while it launches"),
}

#: The tables a summary reads, when the template recorded them.
TABLES = ("potential-hangs", "hitches", "time-profile", "life-cycle-period")


def record_command(xctrace: tuple[str, ...], device: str, preset: Preset, time_limit_s: int,
                   output: Path, bundle_id: str) -> list[str]:
    cmd = [*xctrace, "record", "--device", device, "--template", preset.template]
    for instrument in preset.instruments:
        cmd += ["--instrument", instrument]
    return cmd + ["--time-limit", f"{time_limit_s}s", "--no-prompt", "--output", str(output),
                  "--launch", "--", bundle_id]


def triage_command(xctrace: tuple[str, ...], trace: Path, limit: int) -> list[str]:
    """`xctrace triage --style hangs` (Xcode 27.2): Apple's own reading of each
    hang -- its tier, the main thread's CPU, and the system calls it made."""
    return [*xctrace, "triage", "--input", str(trace), "--style", "hangs", "--limit", str(limit),
            "--format", "text"]


def calltree_command(xctrace: tuple[str, ...], trace: Path, track: str, calltree: str,
                     start_ns: int, duration_ns: int) -> list[str]:
    """`xctrace show` for one call tree over one window, with each frame's
    library, in markdown: the heaviest stack is marked, one row per frame."""
    return [*xctrace, "show", "--input", str(trace), "--track", track, "--calltree", calltree,
            # xctrace refuses a start of 0, in any unit (measured: exit 49).
            "--time-start", f"{max(start_ns, 1)}ns", "--duration", f"{duration_ns}ns",
            "--format", "markdown", "--arrange-columns", "Library Name|..."]


def export_command(xctrace: tuple[str, ...], trace: Path, schema: str | None) -> list[str]:
    if schema is None:
        return [*xctrace, "export", "--input", str(trace), "--toc"]
    return [*xctrace, "export", "--input", str(trace), "--xpath",
            f'/trace-toc/run[@number="1"]/data/table[@schema="{schema}"]']


# -- reading exports ---------------------------------------------------------

class _Refs:
    """Resolve `ref=` elements to the `id=` element they point at."""

    def __init__(self, root: ET.Element) -> None:
        self.ids = {e.attrib["id"]: e for e in root.iter() if "id" in e.attrib}

    def __call__(self, e: ET.Element | None) -> ET.Element | None:
        if e is None:
            return None
        return self.ids.get(e.attrib["ref"], e) if "ref" in e.attrib else e

    def fmt(self, e: ET.Element | None) -> str | None:
        e = self(e)
        if e is None:
            return None
        return e.attrib.get("fmt") or (e.text or "").strip() or None

    def number(self, e: ET.Element | None) -> int | None:
        e = self(e)
        try:
            return int((e.text or "").strip()) if e is not None else None
        except ValueError:
            return None


def parse_toc(text: str) -> dict:
    """The run's facts: what was launched, why it ended, which tables exist."""
    root = ET.fromstring(text)
    run = root.find("run")
    if run is None:
        raise ValueError("the table of contents has no run")
    info = run.find("info")
    target = info.find("target") if info is not None else None
    summary = info.find("summary") if info is not None else None
    device = target.find("device") if target is not None else None
    process = target.find("process") if target is not None else None

    def text_of(parent: ET.Element | None, tag: str) -> str | None:
        e = parent.find(tag) if parent is not None else None
        return (e.text or "").strip() or None if e is not None else None

    duration = text_of(summary, "duration")
    return {
        "template": text_of(summary, "template-name"),
        "end_reason": text_of(summary, "end-reason"),
        "duration_s": float(duration) if duration else None,
        "device": None if device is None else {
            "platform": device.attrib.get("platform"), "model": device.attrib.get("model"),
            "os_version": device.attrib.get("os-version")},
        "process": None if process is None else {
            "name": process.attrib.get("name"),
            "pid": int(process.attrib["pid"]) if process.attrib.get("pid", "").isdigit() else None,
            "exit": process.attrib.get("termination-reason")},
        "tables": sorted({t.attrib["schema"] for t in run.iter("table") if "schema" in t.attrib}),
    }


class _Row(dict):
    """A row's cells keyed by column mnemonic. Cells are tagged by their
    engineering type (`start-time`, `string`), not their column, so a
    `string` could be any of several columns: position is what says which."""

    def find(self, mnemonic: str) -> ET.Element | None:
        return self.get(mnemonic)


def _rows(text: str) -> tuple[list[_Row], _Refs]:
    root = ET.fromstring(text)
    schema = root.find(".//schema")
    if schema is None:
        # An export that matched no table prints an empty result. Read as
        # rows, that is "none found", which it does not say.
        raise ValueError("the export returned no table")
    columns = [c.findtext("mnemonic") for c in schema.findall("col")]
    rows = [_Row(zip(columns, list(row), strict=False)) for row in root.iter("row")]
    return rows, _Refs(root)


def _seconds(refs: _Refs, e: ET.Element | None) -> float | None:
    ns = refs.number(e)
    return round(ns / 1e9, 3) if ns is not None else None


def _ms(refs: _Refs, e: ET.Element | None) -> float | None:
    ns = refs.number(e)
    return round(ns / 1e6, 1) if ns is not None else None


def parse_hangs(text: str) -> list[dict]:
    """`potential-hangs`: each hang's start (seconds into the recording),
    duration, Instruments' kind ("Severe Hang", "Hang", "Microhang") and thread."""
    rows, refs = _rows(text)
    hangs = []
    for row in rows:
        hangs.append({
            "start_s": _seconds(refs, row.find("start")),
            "duration_ms": _ms(refs, row.find("duration")),
            "kind": refs.fmt(row.find("hang-type")),
            "thread": refs.fmt(row.find("thread")),
        })
    return hangs


def parse_hitches(text: str) -> list[dict]:
    """`hitches`: each hitch's start, duration, process and Instruments' own
    explanation of it ("Potentially expensive render, 24 offscreen passes")."""
    rows, refs = _rows(text)
    hitches = []
    for row in rows:
        hitches.append({
            "start_s": _seconds(refs, row.find("start")),
            "duration_ms": _ms(refs, row.find("duration")),
            "process": refs.fmt(row.find("process")),
            "explanation": refs.fmt(row.find("narrative-description")),
        })
    return hitches


def parse_launch_phases(text: str) -> dict:
    """`life-cycle-period`: the app's launch, phase by phase, with
    Instruments' explanation of each ("The system frameworks took 52.07 ms to
    initialize..."). `first_frame_ms` is when "Initial Frame Rendering" ended,
    counted from the first phase: the launch as the user sees it. Measured:
    a physical device records these, a simulator records none."""
    rows, refs = _rows(text)
    phases = []
    for row in rows:
        phases.append({
            "start_s": _seconds(refs, row.find("start")),
            "duration_ms": _ms(refs, row.find("duration")),
            "phase": refs.fmt(row.find("period")),
            "explanation": refs.fmt(row.find("narrative")),
        })
    first_frame_ms = None
    known = [p for p in phases if p["start_s"] is not None]
    for p in known:
        if p["phase"] and "Initial Frame Rendering" in p["phase"] and p["duration_ms"] is not None:
            since_start = (p["start_s"] - known[0]["start_s"]) * 1000
            first_frame_ms = round(since_start + p["duration_ms"], 1)
    return {"phases": phases, "first_frame_ms": first_frame_ms}


def parse_time_profile(text: str, process: str | None, top: int = 10) -> dict:
    """`time-profile`: the functions most often on CPU.

    `top_functions` counts the innermost frame of every sample, which is
    where time is spent. `top_app_functions` counts the app's own innermost
    frame in each sample, the app code nearest that time: a hot system call
    made from the app's loop shows the loop. (Counting every app frame in a
    stack ranks `main` first at 99%, which says nothing.) Only samples from
    `process` count, when it is given.
    """
    rows, refs = _rows(text)
    leaves: Counter = Counter()
    app: Counter = Counter()
    samples = 0
    for row in rows:
        if process is not None:
            proc = refs.fmt(row.find("process")) or ""
            if not proc.startswith(f"{process} ("):
                continue
        backtrace = refs(row.find("stack"))
        if backtrace is None:
            continue
        frames = [refs(f) for f in backtrace.findall("frame")]
        if not frames:
            continue
        samples += 1
        leaf = frames[0]
        leaves[(leaf.attrib.get("name", "?"), _binary(refs, leaf))] += 1
        for frame in frames:              # innermost first
            binary = refs(frame.find("binary"))
            if binary is not None and APP_CONTAINER in binary.attrib.get("path", "").lower():
                app[(frame.attrib.get("name", "?"), binary.attrib.get("name"))] += 1
                break

    def ranked(counter: Counter) -> list[dict]:
        return [{"function": f, "binary": b, "samples": n,
                 "percent": round(n / samples * 100, 1)} for (f, b), n in counter.most_common(top)]

    return {"samples": samples, "top_functions": ranked(leaves),
            "top_app_functions": ranked(app)}


def _binary(refs: _Refs, frame: ET.Element) -> str | None:
    binary = refs(frame.find("binary"))
    return binary.attrib.get("name") if binary is not None else None


# -- triage (xctrace 27.2) ----------------------------------------------------

#: A hang's main thread averaging less CPU than this was mostly waiting
#: (blocked); at or above BUSY_PERCENT, mostly working (busy). Between, both.
BLOCKED_PERCENT = 20.0
BUSY_PERCENT = 50.0
#: A heaviest stack keeps this many frames from the app's end and the leaf's.
STACK_HEAD = 6
STACK_TAIL = 6

_UNITS_MS = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0, "min": 60000.0}
_DURATION = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(ns|µs|us|ms|s|min)$")
_SHOW = re.compile(r'--track "(?P<track>[^"]+)" --summary "Summary: CPU Usage" '
                   r'--time-start (?P<start>[0-9]+)ns --duration (?P<duration>[0-9]+)ns')
_ROW = re.compile(r"^(?P<branch>[├└]─ )(?P<name>\S(?:.*?\S)?)\s{2,}(?P<count>[0-9]+)\s+"
                  r"(?P<duration>[0-9.]+ (?:ns|µs|us|ms|s|min))\b")


def duration_ms(text: str) -> float | None:
    """"12.00 s", "500.02 ms", "292 ns" in milliseconds; None when not one."""
    m = _DURATION.match(text.strip())
    return round(float(m.group(1)) * _UNITS_MS[m.group(2)], 6) if m else None


def _clock_s(text: str) -> float | None:
    """triage's "Start Time: 00:22.916.005" -- [hh:]mm:ss.mmm.uuu -- in seconds."""
    parts = text.strip().split(":")
    try:
        head = [int(p) for p in parts[:-1]]
        sec, ms, us = (int(x) for x in parts[-1].split("."))
    except ValueError:
        return None
    minutes = 0
    for h in head:
        minutes = minutes * 60 + h
    return round(minutes * 60 + sec + ms / 1000 + us / 1e6, 6)


def parse_triage(text: str) -> dict:
    """`xctrace triage --style hangs` output: the tiers it counted, and per
    hang its start, duration, tier, main-thread CPU, system calls and where
    to look closer. Sections it printed "N/A" or "No Data" for are None, so
    "none recorded" stays apart from "made none"."""
    tiers: dict[str, int] = {}
    hangs: list[dict] = []
    blocks = re.split(r"^=+Hang #\d+=+$", text, flags=re.M)
    summary, per_hang = blocks[0], blocks[1:]
    for line in summary.splitlines():
        m = re.match(r"^[├└]─ (.+?)\s{2,}([0-9]+)\s", line)
        if m:
            tiers[m.group(1)] = int(m.group(2))
    for block in per_hang:
        hang: dict = {"start_s": None, "duration_ms": None, "tier": None,
                      "main_thread_cpu": None, "system_calls": None, "window": None}
        for line in block.splitlines():
            if line.startswith("Start Time:"):
                hang["start_s"] = _clock_s(line.split(":", 1)[1])
            elif line.startswith("Duration:"):
                hang["duration_ms"] = duration_ms(line.split(":", 1)[1])
            elif line.startswith("Hang Type:"):
                hang["tier"] = line.split(":", 1)[1].strip()
        cpu = re.search(r"^CPU Usage of Main Thread.*\n.*\n\* All \*\s+([0-9.]+)%\s+([0-9.]+)%"
                        r"\s+([0-9.]+)%", block, flags=re.M)
        if cpu:
            hang["main_thread_cpu"] = {"avg_percent": float(cpu.group(1)),
                                       "max_percent": float(cpu.group(3))}
        show = _SHOW.search(block)
        if show:
            hang["window"] = {"track": show.group("track"), "start_ns": int(show.group("start")),
                              "duration_ns": int(show.group("duration"))}
        calls = re.search(r"^Summary of System Calls(?P<head>.*)$(?P<body>(?:\n(?!Run `|$).*)*)",
                          block, flags=re.M)
        if calls and "N/A" not in calls.group("head"):
            rows = []
            for line in calls.group("body").splitlines():
                m = _ROW.match(line)
                if m:
                    rows.append({"call": m.group("name").strip(), "count": int(m.group("count")),
                                 "duration_ms": duration_ms(m.group("duration"))})
            hang["system_calls"] = rows
        hangs.append(hang)
    return {"tiers": tiers, "hangs": hangs}


def nature(cpu: dict | None) -> str | None:
    """"blocked", "busy" or "mixed", by the main thread's average CPU."""
    if not cpu:
        return None
    avg = cpu["avg_percent"]
    return "blocked" if avg < BLOCKED_PERCENT else "busy" if avg >= BUSY_PERCENT else "mixed"


def parse_heaviest(markdown: str, app: str | None) -> list[str] | None:
    """The heaviest stack of an `xctrace show --format markdown` call tree,
    from the app's own innermost frames down: "symbol (library)" each. The
    frames above the app's -- the run loop, UIKit's dispatch -- are the same
    for every hang and say nothing. None when the tree had no rows."""
    lines = [ln for ln in markdown.splitlines() if ln.startswith("|")]
    if len(lines) < 3:
        return None
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    try:
        lib_i, sym_i = header.index("Library Name"), header.index("Symbol Names")
    except ValueError:
        return None
    frames = []
    for line in lines[2:]:
        # Symbol Names is the last column and can hold a "|" (`operator|`), so
        # it takes the rest of the row; strip("|") would also eat the empty
        # first cell of every row that is not on the heaviest path.
        cells = line.removeprefix("|").removesuffix("|").split("|", len(header) - 1)
        if len(cells) == len(header) and cells[0].strip() == "*":
            frames.append((cells[sym_i].strip(), cells[lib_i].strip()))
    if not frames:
        return None
    mine = [i for i, (_sym, lib) in enumerate(frames) if app and lib == app]
    if mine:
        start = mine[-1]
        while start - 1 in mine:
            start -= 1
    else:
        start = max(0, len(frames) - STACK_TAIL)
    stack = [_frame(sym, lib) for sym, lib in frames[start:]]
    if len(stack) > STACK_HEAD + STACK_TAIL + 1:
        # Measured: a busy main thread's heaviest path ran 50 frames below the
        # app's, most of them unsymbolicated system addresses. Whose code it
        # was and what the CPU was doing at the leaf are the two ends.
        hidden = len(stack) - STACK_HEAD - STACK_TAIL
        stack = [*stack[:STACK_HEAD], f"... {hidden} frames ...", *stack[-STACK_TAIL:]]
    return stack


def _frame(sym: str, lib: str) -> str:
    """"symbol (library)". An unsymbolicated frame comes as
    "0x185028f24 (UIKitCore +0x228f87) <B884823E-...>": it names its library
    already, and the image UUID is for a symbolicator, not a reader."""
    if sym.startswith("0x"):
        sym = re.sub(r" <[0-9A-F-]+>$", "", sym)
        if "(" in sym:
            return sym
    return f"{sym} ({lib})" if lib else sym
