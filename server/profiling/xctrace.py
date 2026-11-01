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
    "hangs": Preset("Time Profiler", ("Hangs",), True,
                    "main-thread hangs, with CPU samples to show what ran"),
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
