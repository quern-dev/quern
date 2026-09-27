"""API routes for log streaming, querying, and source management."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from server.api.actions import logged_action
from server.models import (
    Completeness,
    LogEntry,
    LogErrorsResponse,
    LogLevel,
    LogQueryParams,
    LogSource,
    LogStreamParams,
    LogSummaryResponse,
    StartOslogRequest,
    UtcDatetime,
)
from server.processing.summarizer import (
    WINDOW_DURATIONS,
    generate_summary,
)
from server.storage.arrival import (
    ArrivalCursor,
    TimestampCursor,
    make_arrival_cursor,
    parse_any_cursor,
)
from server.storage.fanout import DropNotice, Missed
from server.storage.ring_buffer import RingBuffer

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


def _get_buffers(request: Request, source: LogSource | None) -> list[RingBuffer]:
    """Return the buffer(s) to query based on source filter.

    Server logs and crash reports each live in a dedicated buffer, so device
    syslog can evict neither.
    """
    if source == LogSource.SERVER:
        return [request.app.state.server_buffer]
    if source == LogSource.CRASH:
        return [request.app.state.crash_buffer]
    if source is not None:
        return [request.app.state.ring_buffer]
    # No source filter — merge them all
    return [
        request.app.state.ring_buffer,
        request.app.state.server_buffer,
        request.app.state.crash_buffer,
    ]


def _completeness(
    buffers: list[RingBuffer],
    since: datetime | None,
    *,
    source: LogSource | None = None,
    min_level: LogLevel | None = None,
) -> dict[str, Any]:
    """`truncated` and `complete_after` for an answer drawn from `buffers`.

    Narrowed by the same source and level the query used, so a search for
    errors is not reported incomplete because debug lines were shed -- a flag
    that is always on tells the reader nothing.
    """
    sources = [source] if source is not None else None
    truncated = not all(
        b.is_complete_since(since, sources, min_level) for b in buffers
    )
    stamps = [
        at for b in buffers
        if (at := b.evicted_through(sources, min_level)) is not None
    ]
    return {"truncated": truncated, "complete_after": max(stamps) if stamps else None}


class LogQueryResponse(Completeness):
    entries: list[LogEntry]
    total: int
    has_more: bool


class SourcesResponse(BaseModel):
    sources: list[dict[str, Any]]
    #: Per buffer: capacity, size, intake, evictions by source, and the span it
    #: still holds. `entries_captured` on a source is intake; this is what
    #: survived it. A source can report 870,000 captured while its buffer holds
    #: the last 3.5 seconds, and only this says so.
    buffers: dict[str, dict[str, Any]] = {}


class FilterRequest(BaseModel):
    source: str | None = None
    device_id: str | None = None
    process: str | None = None
    processes: list[str] | None = None
    subsystems: list[str] | None = None
    exclude_processes: list[str] | None = None
    exclude_subsystems: list[str] | None = None
    exclude_messages: list[str] | None = None
    min_level: str | None = None
    preset: str | None = None
    flush: bool = True


# ---------------------------------------------------------------------------
# SSE Streaming
# ---------------------------------------------------------------------------


@router.get("/stream")
async def stream_logs(
    request: Request,
    level: LogLevel | None = None,
    process: str | None = None,
    subsystem: str | None = None,
    category: str | None = None,
    source: LogSource | None = None,
    match: str | None = None,
    exclude: str | None = None,
    device_id: str | None = None,
) -> EventSourceResponse:
    """Stream log entries in real time via Server-Sent Events."""
    buffers = _get_buffers(request, source)
    params = LogStreamParams(
        level=level,
        process=process,
        subsystem=subsystem,
        category=category,
        source=source,
        match=match,
        exclude=exclude,
        device_id=device_id,
    )

    min_levels: set[LogLevel] | None = None
    if params.level is not None:
        min_levels = set(LogLevel.at_least(params.level))

    def matches_filter(entry: LogEntry) -> bool:
        if params.device_id and entry.device_id != params.device_id:
            return False
        if min_levels and entry.level not in min_levels:
            return False
        if params.process and entry.process != params.process:
            return False
        if params.subsystem and entry.subsystem != params.subsystem:
            return False
        if params.category and entry.category != params.category:
            return False
        if params.source and entry.source != params.source:
            return False
        if params.match and params.match.lower() not in entry.message.lower():
            return False
        if params.exclude and params.exclude.lower() in entry.message.lower():
            return False
        return True

    async def event_generator():
        # Subscribe to all relevant buffers and merge into one queue
        merged: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=1000)
        # Subscribed with the client's filter, so entries it did not ask for
        # never take a slot and are never counted as missed.
        subscriptions = [(buf, buf.subscribe(matches_filter)) for buf in buffers]
        merge_missed = Missed()

        async def forward(queue: asyncio.Queue[LogEntry]) -> None:
            while True:
                entry = await queue.get()
                try:
                    merged.put_nowait(entry)
                except asyncio.QueueFull:
                    merge_missed.add(entry)

        def missed() -> list[Missed]:
            return [merge_missed, *(buf.missed(q) for buf, q in subscriptions)]

        notice = DropNotice()
        tasks = [asyncio.create_task(forward(q)) for _, q in subscriptions]
        try:
            while True:
                if await request.is_disconnected():
                    break
                # Said in the stream, as it happens. A client that falls behind
                # loses entries at two points -- the buffer's queue for it and
                # the merge queue here -- and both dropped silently; in practice
                # it was the merge queue, which the forwarder above fills as fast
                # as the buffer does. A gap the client is told about is one it
                # can fill with `query_logs`; one it is not reads as quiet (#255).
                if (due := notice.due(*missed())) is not None:
                    yield {"event": "dropped", "data": json.dumps(due)}
                try:
                    entry = await asyncio.wait_for(merged.get(), timeout=15.0)
                    if matches_filter(entry):
                        yield {
                            "event": "log",
                            "data": entry.model_dump_json(),
                        }
                except TimeoutError:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({
                            "time": datetime.now(UTC).isoformat(),
                            "buffer_size": buffers[0].size,
                            "total_dropped": sum(m.count for m in missed()),
                        }),
                    }
        finally:
            for task in tasks:
                task.cancel()
            for buf, queue in subscriptions:
                buf.unsubscribe(queue)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Historical Query
# ---------------------------------------------------------------------------


@router.get("/query", response_model=LogQueryResponse)
async def query_logs(
    request: Request,
    since: UtcDatetime | None = None,
    until: UtcDatetime | None = None,
    level: LogLevel | None = None,
    process: str | None = None,
    category: str | None = Query(
        default=None,
        description=(
            "What quern was doing, e.g. 'device.action'. Distinct from "
            "`source`, which is who produced the entry -- both a 'proxy' "
            "category and a LogSource.PROXY exist and they mean different "
            "things. See server/logging_ext.CATEGORIES."
        ),
    ),
    source: LogSource | None = None,
    search: str | None = None,
    device_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    tail: bool = Query(default=False, description="If true, return the last N matching entries"),
) -> LogQueryResponse:
    """Query historical log entries with filters and pagination."""
    params = LogQueryParams(
        since=since,
        until=until,
        level=level,
        process=process,
        category=category,
        source=source,
        search=search,
        device_id=device_id,
        limit=limit,
        offset=offset,
        tail=tail,
    )

    buffers = _get_buffers(request, source)
    if len(buffers) == 1:
        entries, total = await buffers[0].query(params)
    else:
        # Merge results from multiple buffers, sorted by timestamp
        all_entries: list[LogEntry] = []
        for buf in buffers:
            buf_entries = await buf.filter_entries(params)
            all_entries.extend(buf_entries)
        all_entries.sort(key=lambda e: e.timestamp)
        total = len(all_entries)
        if tail:
            entries = all_entries[-limit:]
        else:
            entries = all_entries[offset : offset + limit]

    entries.reverse()

    completeness = _completeness(buffers, since, source=source, min_level=level)
    # A tail asks for the newest N, not for a window, so older entries being
    # gone does not make it incomplete. Without this, `tail_logs` on any busy
    # server would say "truncated" on every call, and a flag that is always
    # on is ignored. Two cases, because "newest" means two different things:
    #
    # - From one buffer, a tail is ranked by *arrival*, and a buffer evicts in
    #   arrival order, so a tail that got its N is always whole. Timestamps do
    #   not enter into it -- which matters, because a physical iPhone's lines
    #   arrive out of timestamp order, and comparing timestamps here reported
    #   a full tail as truncated. Measured on an iPhone 12 before this rule.
    # - Merged across buffers, the result is ranked by *timestamp*, so it is
    #   whole only if everything returned is newer than anything evicted.
    through = completeness["complete_after"]
    if tail and completeness["truncated"] and len(entries) == limit and (
        len(buffers) == 1
        or (through is not None and min(e.timestamp for e in entries) > through)
    ):
        completeness["truncated"] = False

    return LogQueryResponse(
        entries=entries,
        total=total,
        has_more=(offset + limit) < total,
        **completeness,
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=LogSummaryResponse)
async def get_summary(
    request: Request,
    window: str = Query(default="5m", pattern=r"^(30s|1m|5m|15m|1h)$"),
    process: str | None = None,
    since_cursor: str | None = None,
) -> LogSummaryResponse:
    """Get an LLM-optimized summary of recent log activity.

    The response includes a `cursor` field. Pass it back as `since_cursor`
    on the next call to get only new entries since the last summary.
    """
    # Summary always reads from every buffer (no source filter)
    buffers = _get_buffers(request, None)
    clock = buffers[0].clock
    # Snapshot before reading, and read only up to it. Defensive today: no
    # buffer holds its lock across an await, so an append cannot land between
    # two buffers' reads. If that changes, this is what keeps the delta exact
    # -- an entry arriving mid-read goes to the next delta, not to both.
    upto = clock.now

    all_entries: list[LogEntry] = []
    cursor = parse_any_cursor(since_cursor) if since_cursor else None
    # Unhonourable: not a cursor, a cursor from another run, or one ahead of
    # anything this server has numbered. That last can only be mangled or
    # invented, and read as-is it answered "nothing new" with every flag clean.
    cursor_reset = bool(since_cursor) and (
        cursor is None
        or (isinstance(cursor, ArrivalCursor) and (
            cursor.boot != clock.boot or cursor.seq > upto
        ))
    )
    # What the answer covers, for the completeness check.
    covers_since: datetime | None = None
    arrival_after: int | None = None
    if isinstance(cursor, ArrivalCursor) and not cursor_reset:
        # Everything that *arrived* since the last summary, whatever its
        # timestamp. The timestamp cursor this replaces skipped any entry that
        # arrived late with an earlier stamp -- a device clock ahead of the
        # host, a crash report stamped when the crash happened (#317).
        arrival_after = cursor.seq
        for buf in buffers:
            all_entries.extend(await buf.entries_between(cursor.seq, upto))
    elif isinstance(cursor, TimestampCursor):
        # An old cursor, from a client that has not taken a new one yet. Read
        # the way it always was; the response carries an arrival cursor, so
        # the next call is exact.
        covers_since = cursor.at
        for buf in buffers:
            all_entries.extend(await buf.get_after(cursor.at, upto))
    else:
        duration = WINDOW_DURATIONS[window]
        covers_since = datetime.now(UTC) - duration
        for buf in buffers:
            all_entries.extend(await buf.get_since(covers_since, upto))

    all_entries.sort(key=lambda e: e.timestamp)
    summary = generate_summary(all_entries, window=window, process=process)
    summary.cursor = make_arrival_cursor(clock, upto)
    summary.cursor_reset = cursor_reset
    completeness = _completeness(buffers, covers_since)
    if arrival_after is not None:
        # Eviction is in arrival order, so this is exact: something that
        # arrived after the cursor is gone iff the latest eviction did.
        completeness["truncated"] = any(b.last_evicted_seq > arrival_after for b in buffers)
    summary.truncated = completeness["truncated"]
    summary.complete_after = completeness["complete_after"]
    if summary.truncated:
        # In the prose as well as the field. The prose is what a reader takes
        # in first, and counts presented as whole when they are not are the
        # exact misreading this exists to stop.
        summary.summary = (
            "Entries that arrived since the last summary were evicted before "
            "this summary, so the counts below may be low. "
            if arrival_after is not None else
            "Entries in this window were evicted before this summary, so the "
            "counts below may be low. "
        ) + summary.summary
    return summary


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@router.get("/errors", response_model=LogErrorsResponse)
async def get_errors(
    request: Request,
    since: UtcDatetime | None = None,
    limit: int = Query(default=50, ge=1, le=1000),
    include_crashes: bool = True,
) -> LogErrorsResponse:
    """Get error-level entries and crash reports."""
    # Errors endpoint reads from both buffers (server errors are important!)
    buffers = _get_buffers(request, None)
    error_levels = set(LogLevel.at_least(LogLevel.ERROR))

    candidates: list[LogEntry] = []
    for buf in buffers:
        if since:
            candidates.extend(await buf.get_since(since))
        else:
            candidates.extend(await buf.get_recent(buf.max_size))

    candidates.sort(key=lambda e: e.timestamp)
    all_entries = [e for e in candidates if e.level in error_levels]

    if not include_crashes:
        all_entries = [e for e in all_entries if e.source != LogSource.CRASH]

    total = len(all_entries)
    # The newest, newest first -- as `query_logs` and `tail_logs` return them.
    # This sliced from the front of an oldest-first list, so with more errors
    # than `limit` it kept the stale ones and cut the error that just
    # happened: the one a caller asking "what is going wrong" came for.
    limited = all_entries[-limit:][::-1]

    return LogErrorsResponse(
        entries=limited,
        total=total,
        # Error-level evictions only: a busy buffer sheds debug lines all the
        # time, and that loses no errors.
        **_completeness(buffers, since, min_level=LogLevel.ERROR),
    )


# ---------------------------------------------------------------------------
# Source Management
# ---------------------------------------------------------------------------


@router.get("/sources")
async def list_sources(request: Request) -> SourcesResponse:
    """List all active log source adapters and their status."""
    adapters = request.app.state.source_adapters
    return SourcesResponse(
        sources=[adapter.status().model_dump() for adapter in adapters.values()],
        buffers={
            "logs": request.app.state.ring_buffer.stats(),
            "server": request.app.state.server_buffer.stats(),
            "crashes": request.app.state.crash_buffer.stats(),
        },
    )


@router.post("/filter")
async def set_filter(request: Request, filter_req: FilterRequest) -> dict:
    """Configure the ingestion filter to drop noisy entries before the ring buffer.

    Supports presets (e.g. "device-quiet") and per-field overrides.
    Filters can be scoped globally, per-source, or per-device.
    """
    from fastapi import HTTPException

    from server.processing.ingestion_filter import PRESETS, build_config

    ingestion_filter = request.app.state.ingestion_filter
    if ingestion_filter is None:
        raise HTTPException(status_code=503, detail="Server not fully started")

    # Validate preset
    if filter_req.preset and filter_req.preset not in PRESETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset: {filter_req.preset!r}. Available: {sorted(PRESETS)}",
        )

    # Validate source
    source = None
    if filter_req.source:
        try:
            source = LogSource(filter_req.source)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown source: {filter_req.source!r}. "
                    f"Available: {[s.value for s in LogSource]}"
                ),
            )

    # Validate min_level
    min_level = None
    if filter_req.min_level:
        try:
            min_level = LogLevel(filter_req.min_level)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown level: {filter_req.min_level!r}. "
                    f"Available: {[lv.value for lv in LogLevel]}"
                ),
            )

    # Build config from preset + overrides
    overrides = {}
    if filter_req.process is not None:
        overrides["process"] = filter_req.process
    if filter_req.processes is not None:
        overrides["processes"] = filter_req.processes
    if filter_req.subsystems is not None:
        overrides["subsystems"] = filter_req.subsystems
    if filter_req.exclude_processes is not None:
        overrides["exclude_processes"] = filter_req.exclude_processes
    if filter_req.exclude_subsystems is not None:
        overrides["exclude_subsystems"] = filter_req.exclude_subsystems
    if filter_req.exclude_messages is not None:
        overrides["exclude_messages"] = filter_req.exclude_messages
    if min_level is not None:
        overrides["min_level"] = min_level

    config = build_config(preset=filter_req.preset, **overrides)
    ingestion_filter.update_filter(config, source=source, device_id=filter_req.device_id)

    # Purge pre-filter entries from the buffer so tail_logs sees clean results
    purged = 0
    if filter_req.flush:
        buffer: RingBuffer = request.app.state.ring_buffer
        purged = await buffer.purge(lambda e: ingestion_filter.should_admit(e))

    # Restart adapters with subprocess-level filters when a process include is set
    adapter_restarted = False
    if config.process and source in (LogSource.DEVICE, LogSource.SIMULATOR, None):
        if source in (LogSource.DEVICE, None):
            for adapter in request.app.state.device_log_adapters.values():
                if adapter.is_running:
                    await adapter.reconfigure(process_filter=config.process)
                    adapter_restarted = True
        if source in (LogSource.SIMULATOR, None):
            for adapter in request.app.state.sim_log_adapters.values():
                if adapter.is_running:
                    await adapter.reconfigure(process_filter=config.process)
                    adapter_restarted = True

    return {
        "status": "applied",
        "filter": config.to_dict(),
        "scope": (
            f"device:{filter_req.device_id}" if filter_req.device_id
            else f"source:{source.value}" if source
            else "global"
        ),
        "purged": purged,
        "adapter_restarted": adapter_restarted,
    }


@router.get("/filter")
async def get_filter(request: Request) -> dict:
    """Return the current ingestion filter configuration at all scopes."""
    from fastapi import HTTPException

    ingestion_filter = request.app.state.ingestion_filter
    if ingestion_filter is None:
        raise HTTPException(status_code=503, detail="Server not fully started")

    return ingestion_filter.get_all_configs()


# ---------------------------------------------------------------------------
# Host OSLog streaming (on-demand)
# ---------------------------------------------------------------------------


@router.post("/oslog/start")
@logged_action("start_oslog_streaming", category="logs")
async def start_oslog_streaming(request: Request, body: StartOslogRequest):
    """Start streaming logs from the host Mac's unified logging system.

    Creates an on-demand oslog adapter filtered by subsystem and/or process.
    Logs appear in tail_logs/query_logs with source="oslog".
    """
    from fastapi import HTTPException

    from server.sources.oslog import OslogAdapter

    # Check if already running
    existing = getattr(request.app.state, "oslog_adapter", None)
    if existing is not None and existing.is_running:
        return {
            "status": "already_running",
            "adapter_id": existing.adapter_id,
        }

    dedup = request.app.state.deduplicator

    adapter = OslogAdapter(
        on_entry=dedup.process,
        subsystem_filter=body.subsystem,
        process_filter=body.process,
    )

    await adapter.start()

    if adapter._error:
        raise HTTPException(status_code=500, detail=adapter._error)

    # Register so it appears in list_log_sources
    request.app.state.oslog_adapter = adapter
    request.app.state.source_adapters[adapter.adapter_id] = adapter

    return {
        "status": "started",
        "adapter_id": adapter.adapter_id,
    }


@router.post("/oslog/stop")
@logged_action("stop_oslog_streaming", category="logs")
async def stop_oslog_streaming(request: Request):
    """Stop the on-demand host oslog streaming adapter."""
    from fastapi import HTTPException

    adapter = getattr(request.app.state, "oslog_adapter", None)
    if adapter is None or not adapter.is_running:
        raise HTTPException(status_code=404, detail="No oslog streaming active")

    await adapter.stop()

    # Remove from registries
    request.app.state.source_adapters.pop(adapter.adapter_id, None)
    request.app.state.oslog_adapter = None

    return {"status": "stopped"}
