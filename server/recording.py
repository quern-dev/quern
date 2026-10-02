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
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server import config as config_mod
from server.models import FlowRecord, LogEntry, LogSource
from server.trace import APP_LOG_SOURCES, Ownership, device_of, owns

logger = logging.getLogger(__name__)

#: 2 adds `request_started` lines (#364, phase 2), and with them an exact
#: answer for flows across a gap: a request that started before it is in the
#: file, finished or not.
FORMAT_VERSION = 2
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
_EVENT_TYPES = {"actions": ("action",), "flows": ("flow", "request_started"),
                "logs": ("log", "crash")}


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


#: How far before a gap a lost flow can have started. A flow is stamped when
#: its request *started* but written when it completed, so one in flight when
#: quern stopped and finished while it was down is missing from a window that
#: ends before the gap. Five minutes covers a request that waits out an app's
#: usual 60s timeout several times over; phase 2's request-start events
#: (#364) make it exact.
FLOW_LOOKBACK = timedelta(minutes=5)


@dataclass
class Recording:
    id: str
    udid: str
    dir: Path
    filters: Filters
    started_at: datetime
    #: recording; interrupted (saved, but could not resume -- tried again at
    #: the next start, and stoppable); stopped; failed.
    state: str = "recording"
    error: str | None = None
    stopped_at: datetime | None = None
    counts: dict[str, int] = field(default_factory=lambda: {
        "action": 0, "flow": 0, "request_started": 0, "log": 0, "crash": 0})
    dropped: dict[str, int] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    #: Said on the start response: things that do not stop the recording but
    #: change what it can be trusted for.
    warnings: list[str] = field(default_factory=list)
    # Runtime only.
    _subs: list = field(default_factory=list, repr=False)
    _pumps: list[asyncio.Task] = field(default_factory=list, repr=False)
    _flusher_task: asyncio.Task | None = field(default=None, repr=False)
    _pending: list[str] = field(default_factory=list, repr=False)
    #: What each subscription had dropped when last noted. Separate from
    #: `dropped`, the recording's totals: a resumed recording's subscriptions
    #: start again from zero while its totals carry on.
    _noted: dict[str, int] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Set to end the flusher: it finishes the write it is in, then returns.
    #: Never cancelled mid-write -- a cancelled `to_thread` keeps writing
    #: after the lock is released, which put lines after `stopped`, and lost
    #: them unseen when that write then failed (review).
    _closing: bool = field(default=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def events(self) -> Path:
        return self.dir / EVENTS

    @property
    def manifest(self) -> Path:
        return self.dir / MANIFEST

    @property
    def complete(self) -> bool | None:
        """True when stopped with nothing dropped and no gap; None while it runs."""
        if self.state in ("recording", "interrupted"):
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
            "filters": self.filters.as_dict(), "warnings": list(self.warnings),
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
    # A temporary name of its own, so two writers never share one and a
    # stale snapshot cannot win the rename.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(body, indent=2, default=str))
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _append(path: Path, lines: list[str], sync: bool = False) -> None:
    """Append whole lines, starting on a line of their own: after a crash
    mid-write the file ends in a torn fragment, and a marker glued onto it
    became one unreadable line -- a `resumed` gap lost that way read the
    downtime as covered (review)."""
    data = ("\n".join(lines) + "\n").encode()
    with open(path, "a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                data = b"\n" + data
        f.write(data)
        if sync:
            f.flush()
            os.fsync(f.fileno())


@dataclass
class _Tally:
    """What a recording's file says it holds, read back on resume."""

    counts: dict[str, int]
    dropped: dict[str, int]
    gaps: list[dict]
    last_at: datetime | None


def _tally(events: Path) -> _Tally:
    """Counts, drops and gaps from the file itself: the record, where the
    state file is only a pointer. Saved counts went stale on a crash and a
    resumed recording then reported 0 flows of 1,000 written (review)."""
    counts = {"action": 0, "flow": 0, "request_started": 0, "log": 0, "crash": 0}
    dropped: dict[str, int] = {}
    gaps: list[dict] = []
    last_at = None
    with open(events, "rb") as f:
        for raw in f:
            try:
                event = json.loads(raw)
                kind = event["type"]
                at = datetime.fromisoformat(event["at"])
            except (ValueError, KeyError, TypeError):
                continue          # a torn line from a crash mid-write
            last_at = at
            if kind in counts:
                counts[kind] += 1
            elif kind == "dropped" and event.get("what"):
                what = event["what"]
                dropped[what] = dropped.get(what, 0) + int(event.get("count") or 0)
            elif kind == "resumed" and isinstance(event.get("gap"), dict):
                gaps.append(event["gap"])
    return _Tally(counts=counts, dropped=dropped, gaps=gaps, last_at=last_at)


class RecordingManager:
    """Every recording this server is making, and the subscriptions feeding them."""

    def __init__(self, *, server_buffer, ring_buffer, crash_buffer, flow_store,
                 ip_map: Callable[[], dict[str, tuple[str, bool]]] | None = None) -> None:
        self._server_buffer = server_buffer
        self._ring_buffer = ring_buffer
        self._crash_buffer = crash_buffer
        self._flow_store = flow_store
        #: Raises when the mapping cannot be read, unlike the trace's copy:
        #: a recording cannot be asked again later, so a failure is said in it.
        self._ip_map_source = ip_map or (lambda: {})
        self._ip_map: dict[str, tuple[str, bool]] = {}
        self._ip_map_at = -IP_MAP_REFRESH
        self._ip_map_failing = False
        self._recordings: dict[str, Recording] = {}
        self._save_lock = asyncio.Lock()
        listeners = getattr(flow_store, "pending_dropped_listeners", None)
        if listeners is not None:
            listeners.append(self._note_proxy_stopped)

    def _note_proxy_stopped(self, in_flight: int) -> None:
        """The proxy stopped: requests in flight then will not finish, and a
        recording that says so does not read them back as hung."""
        for rec in self._recordings.values():
            if rec.state == "recording" and "flows" in rec.filters.kinds:
                rec._pending.append(_line("proxy_stopped", in_flight=in_flight))

    # ── selection: the trace's own rules ────────────────────────────────────

    def _ip(self) -> dict[str, tuple[str, bool]]:
        if time.monotonic() - self._ip_map_at >= IP_MAP_REFRESH:
            self._ip_map_at = time.monotonic()
            try:
                self._ip_map = self._ip_map_source()
                self._ip_map_failing = False
            except Exception as e:  # noqa: BLE001 -- said in the recordings, not raised
                if not self._ip_map_failing:
                    # Once per failure, into every recording taking flows: a
                    # physical device's flows are matched by this mapping, and
                    # without it they go unrecorded, which the file must say.
                    self._ip_map_failing = True
                    for rec in self._recordings.values():
                        if rec.state == "recording" and "flows" in rec.filters.kinds:
                            rec._pending.append(_line(
                                "warning", message=f"the recorded device addresses could not "
                                f"be read ({e}); a physical device's flows may be missing "
                                f"until they can"))
        return self._ip_map

    @staticmethod
    def _wants_action(rec: Recording, e: LogEntry) -> bool:
        return e.source == LogSource.SERVER and bool(e.action) and e.udid == rec.udid

    @staticmethod
    def _wants_log(rec: Recording, e: LogEntry) -> bool:
        if e.source not in APP_LOG_SOURCES:
            return False
        return e.device_id == rec.udid or (not e.device_id and rec.filters.include_unattributed)

    @staticmethod
    def _wants_crash(rec: Recording, e: LogEntry) -> bool:
        # A crash report with no device is kept, as the live trace keeps it:
        # host crash reports often carry none, and a crash is the one entry a
        # run most needs. They come a handful at a time, unlike log lines.
        return e.device_id == rec.udid or not e.device_id

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

    def is_live(self, directory: Path) -> bool:
        """Whether this server is still writing into `directory`."""
        return any(r.state == "recording" and r.dir == directory
                   for r in self._recordings.values())

    async def start(self, udid: str, output_dir: str | None, filters: Filters) -> Recording:
        rec_id = f"rec_{uuid.uuid4().hex[:12]}"
        out = Path(os.path.expanduser(output_dir)) if output_dir else (
            config_mod.CONFIG_DIR / "recordings" / rec_id)
        if not out.is_absolute():
            raise RecordingError(f"output_dir must be an absolute path, not {output_dir!r}")
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RecordingError(f"{out} could not be created: {e}") from e
        rec = Recording(id=rec_id, udid=udid, dir=out, filters=filters, started_at=_now())
        try:
            await asyncio.to_thread(self._begin_files, rec)
        except FileExistsError as e:
            # Created exclusively, which is what decides two starts racing
            # into one directory: a check before it would let both through.
            raise RecordingError(f"{rec.events} already exists: pass a new output_dir, so "
                                 f"one recording never appends to another's") from e
        except OSError as e:
            raise RecordingError(f"{out} could not be written: {e}") from e
        self._attach(rec)
        self._recordings[rec.id] = rec
        if not await self._save():
            rec.warnings.append(f"the list of running recordings could not be saved in "
                                f"{_state_file()}: this recording will not resume if quern "
                                f"restarts")
        logger.info("Recording %s started: %s into %s", rec.id, udid, out)
        return rec

    def _begin_files(self, rec: Recording) -> None:
        with open(rec.events, "x") as f:
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
                        ("crash", self._crash_buffer, lambda e: self._wants_crash(rec, e))]
        if "flows" in rec.filters.kinds and self._flow_store is not None:
            sources.append(("flow", self._flow_store, lambda f: self._wants_flow(rec, f)))
            # Each request as it starts, by the same rule: one that never
            # finishes is then in the file as started and never answered.
            sources.append(("request_started", self._flow_store.starts,
                            lambda f: self._wants_flow(rec, f)))
        rec._closing = False
        rec._wake = asyncio.Event()
        for kind, source, accept in sources:
            queue = source.subscribe(accept)
            rec._subs.append((kind, source, queue))
            rec._pumps.append(asyncio.create_task(self._pump(rec, kind, queue)))
        rec._flusher_task = asyncio.create_task(self._flusher(rec))

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
        while not rec._closing:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(rec._wake.wait(), FLUSH_INTERVAL)
            if rec._closing:
                return
            # A failed write ends it too: `_fail` sets `_closing`.
            await self._flush(rec)

    async def _flush(self, rec: Recording, *, sync: bool = False) -> bool:
        """Write what is pending; False if the write failed (and the
        recording with it)."""
        async with rec._lock:
            self._note_drops(rec)
            if not rec._pending:
                return True
            lines, rec._pending = rec._pending, []
            try:
                # Shielded: if this task is cancelled the write still runs to
                # the end, and its outcome is still the one reported.
                await asyncio.shield(asyncio.to_thread(_append, rec.events, lines, sync))
            except OSError as e:
                # A disk that fills mid-run: the recording ends, and says so
                # in every place a reader looks. It never takes the server
                # down with it.
                await self._fail(rec, f"writing {rec.events} failed: {e}")
                return False
            return True

    async def _close_writer(self, rec: Recording) -> None:
        """End the flusher -- letting it finish any write it is in -- stop the
        pumps, and take what the subscriptions still hold."""
        rec._closing = True
        rec._wake.set()
        flusher, rec._flusher_task = rec._flusher_task, None
        if flusher is not None and flusher is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await flusher
        for task in rec._pumps:
            task.cancel()
        for task in rec._pumps:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        rec._pumps = []
        self._drain(rec)
        self._note_drops(rec)
        for _, source, queue in rec._subs:
            source.unsubscribe(queue)
        rec._subs = []

    def _drain(self, rec: Recording) -> None:
        """What the subscriptions hold but the pumps have not taken yet."""
        for kind, _, queue in rec._subs:
            while not queue.empty():
                item = queue.get_nowait()
                rec._pending.append(_line(kind, item.model_dump(mode="json")))
                rec.counts[kind] = rec.counts.get(kind, 0) + 1

    async def _fail(self, rec: Recording, why: str) -> None:
        """The recording cannot go on. Called from inside a flush, holding the
        lock, so it ends the writer without waiting on it."""
        logger.error("Recording %s failed: %s", rec.id, why)
        rec.state, rec.error, rec.stopped_at = "failed", why, _now()
        rec._closing = True
        rec._wake.set()
        for task in rec._pumps:
            task.cancel()
        rec._pumps = []
        for _, source, queue in rec._subs:
            source.unsubscribe(queue)
        rec._subs = []
        # Said in the file too, if the file will still take a line.
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_append, rec.events, [_line("failed", error=why)], True)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
        await self._save()

    async def stop(self, recording_id: str) -> Recording:
        rec = self.get(recording_id)
        if rec.state == "interrupted":
            # Saved but never resumed: no subscriptions to end. Stopping says
            # so in the file if it can, and drops it from the list to resume.
            #
            # And it is never complete: nothing was recorded from its last
            # line to now. Without a gap for that, it read `complete: true`
            # with zero counts once its directory came back (review), and the
            # file had no hole for the downtime -- a false all-clear.
            last_at = None
            if rec.events.is_file():
                with contextlib.suppress(OSError):
                    tally = await asyncio.to_thread(_tally, rec.events)
                    rec.counts, rec.dropped, rec.gaps = tally.counts, tally.dropped, tally.gaps
                    last_at = tally.last_at
            gap = {"from": last_at.isoformat() if last_at else None, "to": _now().isoformat(),
                   "reason": f"not recording: {rec.error}"}
            rec.gaps.append(gap)
            rec.state, rec.stopped_at = "stopped", _now()
            try:
                await asyncio.to_thread(_append, rec.events, [_line(
                    "stopped", counts=dict(rec.counts), dropped=dict(rec.dropped),
                    gaps=len(rec.gaps), complete=rec.complete, after=rec.error,
                    gap=gap)], True)
                await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
            except OSError as e:
                rec.state, rec.error = "failed", f"{rec.error}; stopping it failed too: {e}"
            await self._save()
            return rec
        if rec.state != "recording":
            return rec
        await self._close_writer(rec)
        if rec.state != "recording":          # the last write failed
            return rec
        rec.state, rec.stopped_at = "stopped", _now()
        rec._pending.append(_line("stopped", counts=dict(rec.counts), dropped=dict(rec.dropped),
                                  gaps=len(rec.gaps), complete=rec.complete))
        if await self._flush(rec, sync=True):
            try:
                await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
            except OSError as e:
                rec.state, rec.error = "failed", f"writing {rec.manifest} failed: {e}"
        await self._save()
        logger.info("Recording %s stopped: %s", rec.id, rec.counts)
        return rec

    # ── surviving a restart ─────────────────────────────────────────────────

    def _snapshot(self) -> dict:
        """Taken on the event loop, never in a worker thread: iterating the
        recordings there raced a start changing them (review)."""
        return {"recordings": [
            {"id": r.id, "udid": r.udid, "output_dir": str(r.dir),
             "filters": r.filters.as_dict(), "started_at": r.started_at.isoformat()}
            for r in self._recordings.values() if r.state in ("recording", "interrupted")]}

    async def _save(self) -> bool:
        """Save which recordings to resume; False if that could not be done.
        One at a time, each with a snapshot taken when its turn comes, so an
        older snapshot can never land after a newer one."""
        async with self._save_lock:
            snapshot = self._snapshot()
            path = _state_file()

            def write() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(path, snapshot)
            try:
                await asyncio.to_thread(write)
                return True
            except OSError:
                logger.exception("Could not save the running recordings in %s; a restart "
                                 "will not resume them", path)
                return False

    async def shutdown(self) -> None:
        """Quern is stopping: say so in each recording, and keep it to resume."""
        for rec in list(self._recordings.values()):
            if rec.state != "recording":
                continue
            await self._close_writer(rec)
            if rec.state != "recording":
                continue
            rec._pending.append(_line("paused", reason="quern stopped"))
            await self._flush(rec, sync=True)
        await self._save()

    async def resume_all(self) -> list[str]:
        """Pick up the recordings a previous run of quern was making, each
        with a `resumed` line naming the gap. What happened in the gap is
        not in the file, and the file says so.

        One that cannot resume -- its directory missing, say an artifacts
        volume not yet mounted -- is kept as `interrupted`: listed, stoppable,
        and tried again at the next start. Dropping it from the saved list,
        as this once did, forgot it for good on the first failure (review).
        """
        try:
            saved = json.loads(_state_file().read_text()).get("recordings", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError) as e:
            logger.error("Could not read %s, so running recordings were not resumed: %s",
                         _state_file(), e)
            return []
        resumed = []
        for s in saved:
            try:
                rec = Recording(id=s["id"], udid=s["udid"], dir=Path(s["output_dir"]),
                                filters=Filters.from_dict(s.get("filters") or {}),
                                started_at=datetime.fromisoformat(s["started_at"]))
            except (KeyError, TypeError, ValueError, RecordingError) as e:
                logger.error("Skipping a saved recording that cannot be read: %s (%r)", e, s)
                continue
            self._recordings[rec.id] = rec
            if not rec.events.is_file():
                rec.state = "interrupted"
                rec.error = (f"{rec.dir} is gone: it will be tried again the next time quern "
                             f"starts")
                logger.error("Recording %s cannot resume: %s is gone", rec.id, rec.dir)
                continue
            try:
                tally = await asyncio.to_thread(_tally, rec.events)
                rec.counts, rec.dropped, rec.gaps = tally.counts, tally.dropped, tally.gaps
                gap = {"from": tally.last_at.isoformat() if tally.last_at else None,
                       "to": _now().isoformat(), "reason": "quern was not running"}
                await asyncio.to_thread(_append, rec.events, [
                    _line("resumed", gap=gap, clock_anchor=_anchor())], True)
            except OSError as e:
                rec.state = "interrupted"
                rec.error = f"{rec.events} could not be resumed ({e}): tried again next start"
                logger.error("Recording %s cannot resume: %s", rec.id, e)
                continue
            rec.gaps.append(gap)
            try:
                await asyncio.to_thread(_write_json_atomic, rec.manifest, rec.manifest_body())
            except OSError as e:
                # The events file is the record and it is taking lines; the
                # manifest is rewritten at stop. Said, not fatal.
                rec.warnings.append(f"{rec.manifest} could not be updated: {e}")
            self._attach(rec)
            resumed.append(rec.id)
            logger.info("Recording %s resumed after a gap from %s", rec.id, gap["from"])
        await self._save()
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
    #: `None` edge is unknown and reaches as far as it must.
    holes: list[tuple[datetime | None, datetime | None, frozenset[str], str]]
    stopped: bool
    unreadable_lines: int
    #: The (wall, monotonic) anchors written at each start and resume, in
    #: order. A recording that spans a reboot has more than one monotonic
    #: base: join each stretch against video with its own anchor.
    clock_anchors: list[dict] = field(default_factory=list)
    #: How many times `monotonic` went backwards: a reboot during the run.
    monotonic_resets: int = 0
    warnings: list[str] = field(default_factory=list)
    #: Requests the file has a start for and no flow: hung, cut off by a gap
    #: or the end, or lost with dropped flows. Each carries why in `error`.
    unfinished: list[FlowRecord] = field(default_factory=list)


_EVERYTHING = frozenset({"action", "flow", "log", "crash"})
_NOT_FLOWS = frozenset({"action", "log", "crash"})


def _gap_holes(start, end, why, *, exact_flows: bool = False) -> list[tuple]:
    """A gap is a hole for everything, reaching back further for flows: one
    in flight when quern stopped is stamped with when it started.

    `exact_flows`: the file records request starts (format 2), so a request
    in flight at the gap is in it as started -- reported as unfinished -- and
    the flow hole is the gap itself.
    """
    if exact_flows:
        return [(start, end, _EVERYTHING, why)]
    flow_start = start - FLOW_LOOKBACK if start is not None else None
    return [(start, end, _NOT_FLOWS, why),
            (flow_start, end, frozenset({"flow"}),
             f"{why}; a request already in flight then, started up to "
             f"{int(FLOW_LOOKBACK.total_seconds() // 60)} minutes earlier, would be missing")]


#: How a record line begins, as `_line` writes it: type first, compact.
_RECORD_PREFIXES = tuple(f'{{"type":"{k}",' for k in ("action", "flow", "log", "crash"))
_AT = re.compile(r'"at":"([^"]+)"')


def load(directory: Path, *, live: bool = False, markers_only: bool = False) -> Loaded:
    """Everything in `<directory>/events.jsonl`. A flow written twice -- the
    store updates one when its response arrives -- is kept once, as last
    written. A torn line from a crash mid-write is counted, never fatal.

    A file with no `stopped` line holds nothing after its last line, and
    says so as a hole: `live` (this server is still writing it) words it as
    not yet written rather than lost. `markers_only` reads what the file
    says about itself -- holes, markers, whether it stopped -- without
    rebuilding any record, for a reader that wants only that.
    """
    actions: list[LogEntry] = []
    flows: dict[str, FlowRecord] = {}
    starts: dict[str, FlowRecord] = {}
    version = 1
    # What can explain a start with no flow, other than a hang.
    cut: list[tuple[datetime | None, datetime | None, str]] = []
    logs: list[LogEntry] = []
    holes: list[tuple] = []
    anchors: list[dict] = []
    warnings: list[str] = []
    udid, stopped, bad, resets = None, False, 0, 0
    last_at, last_mono, failed = None, None, None
    with open(directory / EVENTS) as f:
        for raw in f:
            if markers_only and raw.startswith(_RECORD_PREFIXES):
                # Only the holes are wanted: a record line's body -- a flow
                # can carry 200KB of bodies -- is never parsed, only its time,
                # which the trailing hole starts from (review).
                if m := _AT.search(raw, 0, 200):
                    last_at = _dt(m.group(1)) or last_at
                continue
            try:
                event = json.loads(raw)
                kind = event["type"]
            except (ValueError, KeyError, TypeError):
                bad += 1
                continue
            try:
                at, mono = _dt(event.get("at")), event.get("monotonic")
                if isinstance(mono, (int, float)):
                    if last_mono is not None and mono < last_mono:
                        resets += 1
                    last_mono = mono
                last_at = at or last_at
                if kind == "action":
                    actions.append(LogEntry.model_validate(event["data"]))
                elif kind == "flow":
                    flow = FlowRecord.model_validate(event["data"])
                    flows.pop(flow.id, None)
                    flows[flow.id] = flow
                    starts.pop(flow.id, None)
                elif kind == "request_started":
                    flow = FlowRecord.model_validate(event["data"])
                    if flow.id not in flows:
                        starts[flow.id] = flow
                elif kind in ("log", "crash"):
                    logs.append(LogEntry.model_validate(event["data"]))
                elif kind in ("started", "resumed"):
                    if kind == "started":
                        udid = udid or event.get("udid")
                        if isinstance(event.get("format_version"), int):
                            version = event["format_version"]
                    if isinstance(event.get("clock_anchor"), dict):
                        anchors.append({**event["clock_anchor"], "segment": kind})
                    if kind == "resumed":
                        gap = event.get("gap") or {}
                        holes += _gap_holes(_dt(gap.get("from")), _dt(gap.get("to")),
                                            gap.get("reason") or "gap",
                                            exact_flows=version >= 2)
                        cut.append((None, _dt(gap.get("to")),
                                    f"{gap.get('reason') or 'a gap'} from {gap.get('from')} to "
                                    f"{gap.get('to')}; it may have been answered then"))
                elif kind == "dropped":
                    holes.append((_dt(event.get("first")), _dt(event.get("last")),
                                  frozenset({event.get("what")}),
                                  f"{event.get('count')} {event.get('what')} dropped"))
                    if event.get("what") == "flow":
                        # Spanned by the dropped flows' own timestamps -- their
                        # starts -- so a request inside it may be among them.
                        cut.append((_dt(event.get("first")), _dt(event.get("last")),
                                    f"its flow may be among {event.get('count')} the "
                                    f"recording dropped"))
                elif kind == "proxy_stopped":
                    cut.append((None, at, f"the proxy stopped at {event.get('at')} while it "
                                          f"was in flight"))
                elif kind == "warning":
                    warnings.append(str(event.get("message")))
                elif kind == "failed":
                    failed = str(event.get("error"))
                elif kind == "stopped":
                    stopped = True
                    # A recording stopped without ever resuming carries the
                    # time it was not recording here.
                    if isinstance(event.get("gap"), dict):
                        gap = event["gap"]
                        holes += _gap_holes(_dt(gap.get("from")), _dt(gap.get("to")),
                                            gap.get("reason") or "gap")
            except (ValueError, KeyError, TypeError):
                bad += 1
    if not stopped:
        why = ("still recording: what arrived in the last second may not be written yet"
               if live else
               f"the recording failed ({failed})" if failed else
               "the recording did not stop cleanly: nothing after its last line is in it")
        holes += _gap_holes(last_at, None, why, exact_flows=version >= 2)
    return Loaded(actions=actions, flows=list(flows.values()), logs=logs, udid=udid,
                  holes=holes, stopped=stopped, unreadable_lines=bad,
                  clock_anchors=anchors, monotonic_resets=resets, warnings=warnings,
                  unfinished=[_unfinished(f, cut, stopped=stopped, live=live)
                              for f in starts.values()])


def _unfinished(flow: FlowRecord, cut: list[tuple], *, stopped: bool,
                live: bool) -> FlowRecord:
    """A request with a start and no flow, with why in its `error`.

    Anything that could have taken its response is named first, live or not:
    a gap after it started (quern not running), the proxy stopping after it
    started, or dropped flows whose span holds its timestamp -- a dropped
    flow is stamped with its request's start, so the test is containment,
    not "after". Only with none of those is it unfinished, and that is said
    as what is known: it had not finished, not that it never would.
    """
    t = flow.timestamp
    for start, end, why in cut:
        if (start is None or start <= t) and (end is None or end >= t):
            return flow.model_copy(update={"error": f"no response recorded: {why}"})
    if live and not stopped:
        reason = "in flight: no response yet"
    elif stopped:
        reason = "no response: it had not finished when the recording stopped"
    else:
        reason = "no response: it had not finished when the recording ended"
    return flow.model_copy(update={"error": reason})


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
    if kind in ("flow", "request_started"):
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
                if flow_id is not None and (kind not in ("flow", "request_started")
                                            or data.get("id") != flow_id):
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
