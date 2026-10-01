"""Recording a device's actions, flows and logs to disk for as long as a run lasts (#364).

The buffers behind `/trace` are bounded and shared: one 5,000-flow store for
every device on the machine, which a single CI simulator filled a third of in
35 minutes. A run of 90 minutes therefore loses its start before anyone looks,
and a capture session -- a start time and a filter, read back from that same
buffer when stopped -- loses it too.

A recording is the trace's inputs, saved as they arrive: quern's action
entries for the device, its flows in full, and its app logs and crash reports,
each a line of `events.jsonl` in the shape `/trace` already consumes. So the
trace over a recording is `build_trace` run unchanged on what was written --
no second format, and no second set of attribution rules to drift from the
first. Which work belongs to the device is decided by `server.trace.owns`,
the same rule.

Every line carries `monotonic`, on `time.monotonic()`: the clock video frames
are stamped with (mach absolute time, #290), so a recording joins a saved
video with no conversion. A `(wall, monotonic)` anchor is written at each
start and resume, for rendering labels -- never for the join.

What the file holds is said in the file, not left to be inferred:
- a `dropped` line where the writer fell behind, with the count and span;
- a `paused` line when quern stops, and a `resumed` line with the gap when it
  starts again -- a recording outlives a restart, because one update during a
  90-minute build would otherwise end it without a word;
- a `stopped` line, and `complete` in the manifest: true only with nothing
  dropped and no gap. A recording that never stopped cleanly has no `stopped`
  line, and that alone says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from server import config as config_mod
from server.models import FlowRecord, LogEntry, LogSource
from server.trace import APP_LOG_SOURCES, Ownership, device_of, owns

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
EVENTS = "events.jsonl"
MANIFEST = "manifest.json"
#: How often what has arrived is written. A crash loses at most this much.
FLUSH_INTERVAL = 1.0  # s
#: Physical devices are matched by the address their recorded proxy config
#: names; that mapping changes rarely and costs a file read.
IP_MAP_REFRESH = 30.0  # s


class RecordingError(ValueError):
    """A recording that cannot be started or found, in words for the caller."""


def _state_file() -> Path:
    # Read at call time: QUERN_STATE_DIR redirects CONFIG_DIR, and a path
    # computed at import would outlive the redirection a test sets up.
    return config_mod.CONFIG_DIR / "recordings.json"


def _now() -> datetime:
    return datetime.now(UTC)


def host_matches(host: str, patterns: list[str]) -> bool:
    """`api.example.com` matches `example.com` and `api.example.com`; never
    `badexample.com`."""
    host = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower().strip().lstrip(".").rstrip(".")
        if p and (host == p or host.endswith("." + p)):
            return True
    return False


#: What a recording can collect. `logs` is the device's app logs and its crash
#: reports together, as the trace takes them.
KINDS = ("actions", "flows", "logs")
#: The event types each kind writes.
_EVENT_TYPES = {"actions": ("action",), "flows": ("flow",), "logs": ("log", "crash")}


def event_types(kinds) -> set[str]:
    return {t for k in kinds for t in _EVENT_TYPES[k]}


@dataclass
class Filters:
    #: Which of KINDS to collect; all of them unless narrowed.
    kinds: tuple[str, ...] = KINDS
    hosts: list[str] | None = None
    exclude_hosts: list[str] | None = None
    #: Work quern cannot tie to any device: a flow with no simulator, serial
    #: or known address, a log line with no device. The live trace keeps it,
    #: attributed by time with a caveat; a recording does not unless asked,
    #: because on a shared machine it is other processes' traffic, and it
    #: would land in every device's recording.
    include_unattributed: bool = False

    def __post_init__(self) -> None:
        unknown = [k for k in self.kinds if k not in KINDS]
        if unknown or not self.kinds:
            raise RecordingError(f"kinds must be some of {', '.join(KINDS)}, not "
                                 f"{list(self.kinds)}")
        self.kinds = tuple(k for k in KINDS if k in self.kinds)

    def as_dict(self) -> dict:
        return {"kinds": list(self.kinds), "hosts": self.hosts,
                "exclude_hosts": self.exclude_hosts,
                "include_unattributed": self.include_unattributed}

    @classmethod
    def from_dict(cls, d: dict) -> Filters:
        return cls(kinds=tuple(d.get("kinds") or KINDS), hosts=d.get("hosts"),
                   exclude_hosts=d.get("exclude_hosts"),
                   include_unattributed=bool(d.get("include_unattributed", False)))


def _anchor() -> dict:
    return {"wall": _now().isoformat(), "monotonic": time.monotonic()}


@dataclass
class Recording:
    id: str
    udid: str
    dir: Path
    filters: Filters
    started_at: datetime
    state: str = "recording"            # recording | stopped | failed
    error: str | None = None
    stopped_at: datetime | None = None
    counts: dict[str, int] = field(default_factory=lambda: {
        "action": 0, "flow": 0, "log": 0, "crash": 0})
    dropped: dict[str, int] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    # Runtime only.
    _subs: list = field(default_factory=list, repr=False)
    _tasks: list[asyncio.Task] = field(default_factory=list, repr=False)
    _pending: list[str] = field(default_factory=list, repr=False)
    #: What each subscription had dropped when last noted. Separate from
    #: `dropped`, the recording's totals: a resumed recording's subscriptions
    #: start again from zero while its totals carry on.
    _noted: dict[str, int] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def events(self) -> Path:
        return self.dir / EVENTS

    @property
    def manifest(self) -> Path:
        return self.dir / MANIFEST

    @property
    def complete(self) -> bool | None:
        """True when stopped with nothing dropped and no gap; None while it runs."""
        if self.state == "recording":
            return None
        return self.state == "stopped" and not any(self.dropped.values()) and not self.gaps

    def summary(self) -> dict:
        return {
            "id": self.id, "udid": self.udid, "output_dir": str(self.dir),
            "events": str(self.events), "manifest": str(self.manifest),
            "state": self.state, "error": self.error,
            "started_at": self.started_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "counts": dict(self.counts), "dropped": dict(self.dropped),
            "gaps": list(self.gaps), "complete": self.complete,
            "filters": self.filters.as_dict(),
        }

    def manifest_body(self) -> dict:
        return {"format_version": FORMAT_VERSION, **self.summary(),
                # Phase 3 (#364) fills this in; null says "no video", not
                # "video not yet recorded".
                "video": None}


def _line(kind: str, data: dict | None = None, **extra) -> str:
    """One event: what it is, when quern received it on both clocks, and the
    record itself in the shape `/trace` reads."""
    body = {"type": kind, "at": _now().isoformat(), "monotonic": time.monotonic(), **extra}
    if data is not None:
        body["data"] = data
    return json.dumps(body, separators=(",", ":"), default=str)


def _write_json_atomic(path: Path, body: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, default=str))
    os.replace(tmp, path)


def _last_event_time(events: Path) -> datetime | None:
    """When the last whole line was written: where a gap after a crash starts."""
    try:
        with open(events, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 1_048_576))
            lines = f.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            return datetime.fromisoformat(json.loads(raw)["at"])
        except (ValueError, KeyError, TypeError):
            continue          # a torn last line from a crash mid-write
    return None


class RecordingManager:
    """Every recording this server is making, and the subscriptions feeding them."""

    def __init__(self, *, server_buffer, ring_buffer, crash_buffer, flow_store,
                 ip_map: Callable[[], dict[str, tuple[str, bool]]] | None = None) -> None:
        self._server_buffer = server_buffer
        self._ring_buffer = ring_buffer
        self._crash_buffer = crash_buffer
        self._flow_store = flow_store
        self._ip_map_source = ip_map or (lambda: {})
        self._ip_map: dict[str, tuple[str, bool]] = {}
        self._ip_map_at = -IP_MAP_REFRESH
        self._recordings: dict[str, Recording] = {}

    # ── selection: the trace's own rules ────────────────────────────────────

    def _ip(self) -> dict[str, tuple[str, bool]]:
        if time.monotonic() - self._ip_map_at >= IP_MAP_REFRESH:
            try:
                self._ip_map = self._ip_map_source()
            except Exception:  # noqa: BLE001 -- best effort, as in /trace
                logger.debug("Could not read the ip map for a recording", exc_info=True)
            self._ip_map_at = time.monotonic()
        return self._ip_map

    @staticmethod
    def _wants_action(rec: Recording, e: LogEntry) -> bool:
        return e.source == LogSource.SERVER and bool(e.action) and e.udid == rec.udid

    @staticmethod
    def _wants_log(rec: Recording, e: LogEntry) -> bool:
        if e.source not in APP_LOG_SOURCES:
            return False
        return e.device_id == rec.udid or (not e.device_id and rec.filters.include_unattributed)

    def _wants_flow(self, rec: Recording, f: FlowRecord) -> bool:
        work, _ = device_of(f, self._ip())
        ownership = owns(rec.udid, work)
        if ownership is Ownership.FOREIGN:
            return False
        if ownership is Ownership.UNKNOWN_WORK and not rec.filters.include_unattributed:
            return False
        host = f.request.host or ""
        if rec.filters.hosts and not host_matches(host, rec.filters.hosts):
            return False
        return not (rec.filters.exclude_hosts and host_matches(host, rec.filters.exclude_hosts))

    # ── lifecycle ───────────────────────────────────────────────────────────

    def list(self) -> list[Recording]:
        return list(self._recordings.values())

    def get(self, recording_id: str) -> Recording:
        rec = self._recordings.get(recording_id)
        if rec is None:
            raise RecordingError(f"no recording {recording_id!r} on this server")
        return rec

    async def start(self, udid: str, output_dir: str | None, filters: Filters) -> Recording:
        rec_id = f"rec_{uuid.uuid4().hex[:12]}"
        out = Path(os.path.expanduser(output_dir)) if output_dir else (
            config_mod.CONFIG_DIR / "recordings" / rec_id)
        if not out.is_absolute():
            raise RecordingError(f"output_dir must be an absolute path, not {output_dir!r}")
        for other in self._recordings.values():
            if other.state == "recording" and other.dir.resolve() == out.resolve():
                raise RecordingError(f"{out} is already being recorded into by {other.id}")
        if (out / EVENTS).exists():
            raise RecordingError(f"{out / EVENTS} already exists: pass a new output_dir, so "
                                 f"one recording never appends to another's")
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RecordingError(f"{out} could not be created: {e}") from e
        rec = Recording(id=rec_id, udid=udid, dir=out, filters=filters, started_at=_now())
        try:
            await asyncio.to_thread(self._begin_files, rec)
        except OSError as e:
            raise RecordingError(f"{out} could not be written: {e}") from e
        self._attach(rec)
        self._recordings[rec.id] = rec
        await asyncio.to_thread(self._persist)
        logger.info("Recording %s started: %s into %s", rec.id, udid, out)
        return rec

    def _begin_files(self, rec: Recording) -> None:
        with open(rec.events, "a") as f:
            f.write(_line("started", recording=rec.id, udid=rec.udid,
                          filters=rec.filters.as_dict(), clock_anchor=_anchor(),
                          format_version=FORMAT_VERSION) + "\n")
        _write_json_atomic(rec.manifest, rec.manifest_body())

    def _attach(self, rec: Recording) -> None:
        """Subscribe to every source this recording draws on, and start its
        writer. Filtered at the subscription, so another device's traffic
        never takes a slot in this recording's queue and is never counted as
        dropped from it."""
        sources = []
        if "actions" in rec.filters.kinds:
            sources.append(("action", self._server_buffer,
                            lambda e: self._wants_action(rec, e)))
        if "logs" in rec.filters.kinds:
            sources += [("log", self._ring_buffer, lambda e: self._wants_log(rec, e)),
                        ("crash", self._crash_buffer, lambda e: self._wants_log(rec, e))]
        if "flows" in rec.filters.kinds and self._flow_store is not None:
            sources.append(("flow", self._flow_store, lambda f: self._wants_flow(rec, f)))
        for kind, source, accept in sources:
            queue = source.subscribe(accept)
            rec._subs.append((kind, source, queue))
            rec._tasks.append(asyncio.create_task(self._pump(rec, kind, queue)))
        rec._tasks.append(asyncio.create_task(self._flusher(rec)))

    async def _pump(self, rec: Recording, kind: str, queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            rec._pending.append(_line(kind, item.model_dump(mode="json")))
            rec.counts[kind] = rec.counts.get(kind, 0) + 1

    def _note_drops(self, rec: Recording) -> None:
        """A `dropped` line for whatever each subscription lost since the last."""
        for kind, source, queue in rec._subs:
            missed = source.missed(queue)
            new = missed.count - rec._noted.get(kind, 0)
            if new > 0:
                first, last = missed.take_pending()
                rec._noted[kind] = missed.count
                rec.dropped[kind] = rec.dropped.get(kind, 0) + new
                rec._pending.append(_line(
                    "dropped", what=kind, count=new, total=rec.dropped[kind],
                    first=first.isoformat() if first else None,
                    last=last.isoformat() if last else None))

    async def _flusher(self, rec: Recording) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self._flush(rec)

    async def _flush(self, rec: Recording, *, sync: bool = False) -> None:
        async with rec._lock:
            self._note_drops(rec)
            if not rec._pending:
                return
            lines, rec._pending = rec._pending, []
            try:
                await asyncio.to_thread(self._append, rec.events, lines, sync)
            except OSError as e:
                # A disk that fills mid-run: the recording ends, and says so
                # in every place a reader looks. It never takes the server
                # down with it.
                await self._fail(rec, f"writing {rec.events} failed: {e}")

    @staticmethod
    def _append(path: Path, lines: list[str], sync: bool) -> None:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
            if sync:
                f.flush()
                os.fsync(f.fileno())

    async def _fail(self, rec: Recording, why: str) -> None:
        logger.error("Recording %s failed: %s", rec.id, why)
        rec.state, rec.error, rec.stopped_at = "failed", why, _now()
        self._detach(rec, cancel_current=False)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
        await asyncio.to_thread(self._persist)

    def _detach(self, rec: Recording, *, cancel_current: bool = True) -> None:
        for _, source, queue in rec._subs:
            source.unsubscribe(queue)
        current = asyncio.current_task()
        for task in rec._tasks:
            if task is not current or cancel_current:
                task.cancel()
        rec._tasks = []

    def _drain(self, rec: Recording) -> None:
        """What the subscriptions hold but the pumps have not taken yet."""
        for kind, _, queue in rec._subs:
            while not queue.empty():
                item = queue.get_nowait()
                rec._pending.append(_line(kind, item.model_dump(mode="json")))
                rec.counts[kind] = rec.counts.get(kind, 0) + 1

    async def stop(self, recording_id: str) -> Recording:
        rec = self.get(recording_id)
        if rec.state != "recording":
            return rec
        tasks = list(rec._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        rec._tasks = []
        self._drain(rec)
        self._note_drops(rec)
        for _, source, queue in rec._subs:
            source.unsubscribe(queue)
        rec.state, rec.stopped_at = "stopped", _now()
        rec._pending.append(_line("stopped", counts=dict(rec.counts), dropped=dict(rec.dropped),
                                  gaps=len(rec.gaps), complete=rec.complete))
        await self._flush(rec, sync=True)
        if rec.state == "stopped":
            try:
                await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
            except OSError as e:
                rec.state, rec.error = "failed", f"writing {rec.manifest} failed: {e}"
        await asyncio.to_thread(self._persist)
        logger.info("Recording %s stopped: %s", rec.id, rec.counts)
        return rec

    # ── surviving a restart ─────────────────────────────────────────────────

    def _persist(self) -> None:
        """The recordings to resume if quern restarts: those still running."""
        live = [{"id": r.id, "udid": r.udid, "output_dir": str(r.dir),
                 "filters": r.filters.as_dict(), "started_at": r.started_at.isoformat(),
                 "counts": r.counts, "dropped": r.dropped, "gaps": r.gaps}
                for r in self._recordings.values() if r.state == "recording"]
        path = _state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(path, {"recordings": live})
        except OSError:
            logger.exception("Could not save the running recordings; a restart will not "
                             "resume them")

    async def shutdown(self) -> None:
        """Quern is stopping: say so in each recording, and keep it to resume."""
        for rec in self._recordings.values():
            if rec.state != "recording":
                continue
            for task in list(rec._tasks):
                task.cancel()
            rec._tasks = []
            self._drain(rec)
            self._note_drops(rec)
            for _, source, queue in rec._subs:
                source.unsubscribe(queue)
            rec._subs = []
            rec._pending.append(_line("paused", reason="quern stopped"))
            await self._flush(rec, sync=True)
        await asyncio.to_thread(self._persist)

    async def resume_all(self) -> list[str]:
        """Pick up the recordings a previous run of quern was making, each
        with a `resumed` line naming the gap. What happened in the gap is
        not in the file, and the file says so."""
        try:
            saved = json.loads(_state_file().read_text()).get("recordings", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            logger.error("Could not read %s, so running recordings were not resumed: %s",
                         _state_file(), e)
            return []
        resumed = []
        for s in saved:
            try:
                rec = Recording(id=s["id"], udid=s["udid"], dir=Path(s["output_dir"]),
                                filters=Filters.from_dict(s.get("filters") or {}),
                                started_at=datetime.fromisoformat(s["started_at"]),
                                counts=s.get("counts") or {}, dropped=s.get("dropped") or {},
                                gaps=s.get("gaps") or [])
            except (KeyError, TypeError, ValueError, RecordingError) as e:
                logger.error("Skipping a saved recording that cannot be read: %s (%r)", e, s)
                continue
            if not rec.dir.is_dir():
                logger.error("Recording %s cannot resume: %s is gone", rec.id, rec.dir)
                continue
            since = await asyncio.to_thread(_last_event_time, rec.events)
            gap = {"from": since.isoformat() if since else None, "to": _now().isoformat(),
                   "reason": "quern was not running"}
            rec.gaps.append(gap)
            try:
                await asyncio.to_thread(self._append, rec.events, [
                    _line("resumed", gap=gap, clock_anchor=_anchor())], True)
                await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
            except OSError as e:
                logger.error("Recording %s cannot resume: %s", rec.id, e)
                continue
            self._attach(rec)
            self._recordings[rec.id] = rec
            resumed.append(rec.id)
            logger.info("Recording %s resumed after a gap from %s", rec.id, gap["from"])
        await asyncio.to_thread(self._persist)
        return resumed


# ── reading a recording back ─────────────────────────────────────────────────

@dataclass
class Loaded:
    """A recording's events, rebuilt as the models `build_trace` takes."""

    actions: list[LogEntry]
    flows: list[FlowRecord]
    logs: list[LogEntry]
    udid: str | None
    #: Spans the file says it does not cover: (start, end, kinds, why). A
    #: `None` end is "until now"; kinds name what is missing there.
    holes: list[tuple[datetime | None, datetime | None, frozenset[str], str]]
    stopped: bool
    unreadable_lines: int


def load(directory: Path) -> Loaded:
    """Everything in `<directory>/events.jsonl`. A flow written twice -- the
    store updates one when its response arrives -- is kept once, as last
    written. A torn line from a crash mid-write is counted, never fatal."""
    actions: list[LogEntry] = []
    flows: dict[str, FlowRecord] = {}
    logs: list[LogEntry] = []
    holes: list[tuple[datetime | None, datetime | None, frozenset[str], str]] = []
    everything = frozenset({"action", "flow", "log", "crash"})
    udid, stopped, bad = None, False, 0
    with open(directory / EVENTS) as f:
        for raw in f:
            try:
                event = json.loads(raw)
                kind = event["type"]
                if kind == "action":
                    actions.append(LogEntry.model_validate(event["data"]))
                elif kind == "flow":
                    flow = FlowRecord.model_validate(event["data"])
                    flows.pop(flow.id, None)
                    flows[flow.id] = flow
                elif kind in ("log", "crash"):
                    logs.append(LogEntry.model_validate(event["data"]))
                elif kind == "started":
                    udid = udid or event.get("udid")
                elif kind == "dropped":
                    holes.append((_dt(event.get("first")), _dt(event.get("last")),
                                  frozenset({event.get("what")}),
                                  f"{event.get('count')} {event.get('what')} dropped"))
                elif kind == "resumed":
                    gap = event.get("gap") or {}
                    holes.append((_dt(gap.get("from")), _dt(gap.get("to")), everything,
                                  gap.get("reason") or "gap"))
                elif kind == "stopped":
                    stopped = True
            except (ValueError, KeyError, TypeError):
                bad += 1
    return Loaded(actions=actions, flows=list(flows.values()), logs=logs, udid=udid,
                  holes=holes, stopped=stopped, unreadable_lines=bad)


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def holes_in(loaded: Loaded, kinds: set[str], since: datetime | None,
             until: datetime | None) -> list[str]:
    """The holes overlapping [since, until] for these kinds, as sentences.

    A hole with an unknown edge is taken to reach that far, so an unknown
    is never read as "covered".
    """
    out = []
    for start, end, what, why in loaded.holes:
        if not (what & kinds):
            continue
        if until is not None and start is not None and start > until:
            continue
        if since is not None and end is not None and end < since:
            continue
        out.append(f"{why} ({start.isoformat() if start else '?'} to "
                   f"{end.isoformat() if end else '?'})")
    return out


#: One page of events is at most this many: a 90-minute run held 1,384 flows
#: in 35 minutes of it, and every one carries its bodies.
MAX_PAGE = 2000


def _summary(kind: str, data: dict) -> dict:
    """An event without its bulk: enough to choose which to read in full."""
    if kind == "flow":
        req, resp = data.get("request") or {}, data.get("response") or {}
        return {"id": data.get("id"), "timestamp": data.get("timestamp"),
                "method": req.get("method"), "url": req.get("url"),
                "status": resp.get("status_code") if resp else None,
                "error": data.get("error"),
                "total_ms": (data.get("timing") or {}).get("total_ms")}
    if kind == "action":
        return {"timestamp": data.get("timestamp"), "action": data.get("action"),
                "outcome": data.get("outcome"), "duration_ms": data.get("duration_ms"),
                "started_monotonic": data.get("started_monotonic"),
                "message": data.get("message")}
    return {"timestamp": data.get("timestamp"), "level": data.get("level"),
            "process": data.get("process"), "message": data.get("message")}


def read_events(directory: Path, kinds, *, since: datetime | None = None,
                until: datetime | None = None, cursor: int = 0, limit: int = 500,
                detail: str = "full", flow_id: str | None = None) -> dict:
    """The recording's events of these kinds in [since, until], in the order
    they were written, a page at a time.

    `cursor` is a line number from a previous page's `next_cursor`. Markers
    (`dropped`, `paused`, `resumed`, `stopped`) are always included: they say
    what the page around them does not hold. A flow written twice -- once
    when the store updated it -- appears twice; read the last.
    """
    wanted = event_types(kinds)
    markers = {"started", "dropped", "paused", "resumed", "stopped"}
    events, next_cursor, bad = [], None, 0
    with open(directory / EVENTS) as f:
        for number, raw in enumerate(f):
            if number < cursor:
                continue
            try:
                event = json.loads(raw)
                kind = event["type"]
            except (ValueError, KeyError, TypeError):
                bad += 1
                continue
            if kind not in wanted and kind not in markers:
                continue
            data = event.get("data")
            if kind in wanted and data is not None:
                if flow_id is not None and (kind != "flow" or data.get("id") != flow_id):
                    continue
                at = _dt(data.get("timestamp"))
                if at is not None and ((since and at < since) or (until and at > until)):
                    continue
                if detail == "summary":
                    event = {**event, "data": _summary(kind, data)}
            elif flow_id is not None:
                continue
            if len(events) >= limit:
                next_cursor = number
                break
            events.append(event)
    return {"events": events, "next_cursor": next_cursor, "unreadable_lines": bad}
