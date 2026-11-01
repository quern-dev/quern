"""Gauges: one app's CPU, memory and frames, sampled for as long as a
recording runs, and summarised per action in the trace (#245, phase 2).

A recording started with `gauges=<bundle id or package>` samples that app
back to back -- every `gauge_interval_s` -- and writes each sample as a
`gauge` line. The trace then gives every action the peaks of the samples
that overlap it, so "this tap took 2 s" arrives with "and the app's memory
went from 180 to 260 MB, and 14 frames were janky".

**A sample is a window, not an instant.** CPU is a difference of two
readings, so each sample covers `[from, to]`, a second by default, and
lands wherever the sampler's clock happened to be. A 100 ms tap overlaps one
or two samples, and their peak is the peak of the whole second -- which is
why `sample_seconds` is on every summary. It says how coarse the answer is;
leaving it out would let a reader read a second's average as the tap's.

**The window runs past the action, by the trace's own grace.** Most actions
hand work to the app and return (`server.trace.CAUSAL_GRACE` explains the
measurement), and what a tap costs shows after it returns. So an action's
gauges cover `(started_at, finished_at + grace]`, the same window its late
flows are attributed by. Adjacent actions therefore share samples, which is
measurement, not attribution: nothing is claimed by one action at the
other's expense, so there is nothing to mark ambiguous.

**A metric that could not be read is null, with why.** A summary with no
memory figure says whether the app was not running, the platform does not
measure it, the device could not be asked, or no sample covered the action
at all. Zero is only ever a reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from server.trace import CAUSAL_GRACE

#: How often a recording samples unless told otherwise, and the bounds. Below
#: half a second a simulator's two `ps` readings are mostly noise; above ten
#: a whole tap-and-settle is one sample, which says nothing per action.
INTERVAL_DEFAULT_S = 1.0
INTERVAL_MIN_S = 0.5
INTERVAL_MAX_S = 10.0

#: The metrics a summary reports, in the order it reports them.
METRICS = ("cpu_percent", "memory_bytes", "threads", "frames", "display_fps")


@dataclass
class Sample:
    """One gauge reading, as a recording wrote it."""

    start: datetime
    end: datetime
    #: None when the sample failed: the device could not be asked at all.
    running: bool | None
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    threads: int | None = None
    #: Android's cumulative `gfxinfo` counters: total_frames, janky_frames,
    #: janky_method. Cumulative, so a window's frames are a difference.
    frames: dict | None = None
    display_fps: float | None = None
    unavailable: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    # bool is an int; a reading is never one.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def parse_sample(data: object) -> Sample | None:
    """A `gauge` line's data, or None when it is not one: a torn or foreign
    line must cost that sample, never the trace."""
    if not isinstance(data, dict):
        return None
    start, end = _when(data.get("from")), _when(data.get("to"))
    if start is None or end is None or end < start:
        return None
    running = data.get("running")
    frames = data.get("frames")
    unavailable = data.get("unavailable")
    return Sample(
        start=start, end=end,
        running=running if isinstance(running, bool) else None,
        cpu_percent=_number(data.get("cpu_percent")),
        memory_bytes=_number(data.get("memory_bytes")),
        threads=_number(data.get("threads")),
        frames=frames if isinstance(frames, dict) else None,
        display_fps=_number(data.get("display_fps")),
        unavailable={str(k): str(v) for k, v in unavailable.items()}
        if isinstance(unavailable, dict) else {},
        error=str(data["error"]) if data.get("error") else None,
    )


def sample_line(snapshot: dict, start: datetime, end: datetime) -> dict:
    """What a `gauge` line holds for one snapshot (`PerformanceSnapshot` as a
    dict): its readings, and the window they cover."""
    keep = ("running", "pid", "sample_seconds", "cpu_percent", "memory_bytes",
            "memory_kind", "threads", "frames", "display_fps", "unavailable")
    return {"from": start.isoformat(), "to": end.isoformat(),
            **{k: snapshot.get(k) for k in keep}}


def failed_line(why: str, start: datetime, end: datetime) -> dict:
    """A `gauge` line for a sample that could not be taken: the window it
    should have covered, so the gap reads as "could not tell", not "quiet"."""
    return {"from": start.isoformat(), "to": end.isoformat(), "running": None,
            "error": why, "unavailable": {m: why for m in METRICS}}


def _reasons(samples: list[Sample], metric: str) -> str:
    """Why `metric` is null across `samples`, each distinct reason once."""
    seen: list[str] = []
    for s in samples:
        why = s.unavailable.get(metric) or s.error
        if why and why not in seen:
            seen.append(why)
    return "; ".join(seen) or "no sample read it"


def summarise(samples: list[Sample], started: datetime, finished: datetime, *,
              grace: timedelta = CAUSAL_GRACE, uncovered: str | None = None) -> dict:
    """The gauges over one action: the samples overlapping
    `(started, finished + grace]`, reduced to peaks.

    `samples` is every sample of the device, in time order. `uncovered` is
    what to say when none overlaps -- a recording names the hole it fell in.
    """
    until = finished + grace
    covering = [s for s in samples if s.start < until and started < s.end]
    summary: dict = {
        "window": {"from": started.isoformat(), "to": until.isoformat()},
        "samples": len(covering),
        "sample_seconds": round(max((s.seconds for s in covering), default=0.0), 3) or None,
        "covered": ({"from": covering[0].start.isoformat(), "to": covering[-1].end.isoformat()}
                    if covering else None),
        "cpu_percent": None, "memory_bytes": None, "threads_peak": None,
        "frames": None, "display_fps": None,
        "unavailable": {}, "caveats": [],
    }
    unavailable: dict[str, str] = summary["unavailable"]
    if not covering:
        why = uncovered or "no gauge sample overlaps this action"
        for metric in METRICS:
            unavailable[metric] = why
        return summary

    if covering[0].start > started or covering[-1].end < until:
        summary["caveats"].append(
            "the samples do not span the whole window; the peaks are of the part they cover")
    if any(s.running is False for s in covering) and any(s.running for s in covering):
        summary["caveats"].append("the app was not running for part of this window")

    cpu = [s.cpu_percent for s in covering if s.cpu_percent is not None]
    if cpu:
        summary["cpu_percent"] = {"peak": round(max(cpu), 1),
                                  "mean": round(sum(cpu) / len(cpu), 1)}
    else:
        unavailable["cpu_percent"] = _reasons(covering, "cpu_percent")

    memory = [(s, s.memory_bytes) for s in covering if s.memory_bytes is not None]
    if memory:
        summary["memory_bytes"] = {"peak": int(max(m for _, m in memory)),
                                   "first": int(memory[0][1]), "last": int(memory[-1][1])}
    else:
        unavailable["memory_bytes"] = _reasons(covering, "memory_bytes")

    threads = [s.threads for s in covering if s.threads is not None]
    if threads:
        summary["threads_peak"] = int(max(threads))
    else:
        unavailable["threads"] = _reasons(covering, "threads")

    fps = [s.display_fps for s in covering if s.display_fps is not None]
    if fps:
        summary["display_fps"] = {"min": round(min(fps), 1),
                                  "mean": round(sum(fps) / len(fps), 1)}
    else:
        unavailable["display_fps"] = _reasons(covering, "display_fps")

    frames, why = _frames(samples, covering, started)
    if frames is not None:
        summary["frames"] = frames
    else:
        unavailable["frames"] = why
    return summary


def _counter(s: Sample, key: str) -> int | None:
    value = (s.frames or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _frames(samples: list[Sample], covering: list[Sample], started: datetime,
            ) -> tuple[dict | None, str]:
    """Frames drawn over the window, from Android's cumulative counters: the
    last reading in it, less the last before it.

    Without a reading from before the window, counting from the first one in
    it misses whatever was drawn before that reading, so it is said. A count
    that goes down is the app restarting or its stats being reset, and the
    difference then means nothing.
    """
    read = [s for s in covering if _counter(s, "total_frames") is not None]
    if not read:
        return None, _reasons(covering, "frames")
    before = [s for s in samples if s.end <= started and _counter(s, "total_frames") is not None]
    base, last = (before[-1] if before else read[0]), read[-1]
    if base is last:
        return None, "only one frame reading covers this window; frames need two"
    total = _counter(last, "total_frames") - _counter(base, "total_frames")
    if total < 0:
        return None, ("gfxinfo's frame count went down in this window: the app restarted or "
                      "its stats were reset")
    janky = None
    jb, jl = _counter(base, "janky_frames"), _counter(last, "janky_frames")
    method = (last.frames or {}).get("janky_method")
    if jb is not None and jl is not None and jl >= jb \
            and (base.frames or {}).get("janky_method") == method:
        janky = jl - jb
    frames = {"total": total, "janky": janky, "janky_method": method if janky is not None else None,
              "from": base.end.isoformat(), "to": last.end.isoformat()}
    if not before:
        frames["note"] = ("counted from the first reading inside the window; frames drawn "
                          "before it are not included")
    return frames, ""
