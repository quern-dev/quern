"""API routes for log streaming, querying, and source management."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from server.models import (
    LogEntry,
    LogErrorsResponse,
    LogLevel,
    LogQueryParams,
    LogSource,
    LogStreamParams,
    LogSummaryResponse,
)
from server.processing.summarizer import (
    WINDOW_DURATIONS,
    generate_summary,
    parse_cursor,
)
from server.storage.ring_buffer import RingBuffer

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


def _get_buffers(request: Request, source: LogSource | None) -> list[RingBuffer]:
    """Return the buffer(s) to query based on source filter.

    Server logs live in a dedicated buffer so device syslog can't evict them.
    """
    if source == LogSource.SERVER:
        return [request.app.state.server_buffer]
    if source is not None:
        return [request.app.state.ring_buffer]
    # No source filter — merge both
    return [request.app.state.ring_buffer, request.app.state.server_buffer]


class LogQueryResponse(BaseModel):
    entries: list[LogEntry]
    total: int
    has_more: bool


class SourcesResponse(BaseModel):
    sources: list[dict[str, Any]]


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
        subscriptions = [(buf, buf.subscribe()) for buf in buffers]

        async def forward(queue: asyncio.Queue[LogEntry]) -> None:
            while True:
                entry = await queue.get()
                try:
                    merged.put_nowait(entry)
                except asyncio.QueueFull:
                    pass  # Drop if merged queue is full

        tasks = [asyncio.create_task(forward(q)) for _, q in subscriptions]
        try:
            while True:
                if await request.is_disconnected():
                    break
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
    since: datetime | None = None,
    until: datetime | None = None,
    level: LogLevel | None = None,
    process: str | None = None,
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

    return LogQueryResponse(
        entries=entries,
        total=total,
        has_more=(offset + limit) < total,
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
    # Summary always reads from both buffers (no source filter)
    buffers = _get_buffers(request, None)

    all_entries: list[LogEntry] = []
    if since_cursor:
        cursor_ts = parse_cursor(since_cursor)
        for buf in buffers:
            if cursor_ts:
                all_entries.extend(await buf.get_after(cursor_ts))
            else:
                all_entries.extend(await buf.get_recent(buf.max_size))
    else:
        duration = WINDOW_DURATIONS[window]
        cutoff = datetime.now(UTC) - duration
        for buf in buffers:
            all_entries.extend(await buf.get_since(cutoff))

    all_entries.sort(key=lambda e: e.timestamp)
    return generate_summary(all_entries, window=window, process=process)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@router.get("/errors", response_model=LogErrorsResponse)
async def get_errors(
    request: Request,
    since: datetime | None = None,
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
    limited = all_entries[:limit]

    return LogErrorsResponse(entries=limited, total=total)


# ---------------------------------------------------------------------------
# Source Management
# ---------------------------------------------------------------------------


@router.get("/sources")
async def list_sources(request: Request) -> SourcesResponse:
    """List all active log source adapters and their status."""
    adapters = request.app.state.source_adapters
    return SourcesResponse(
        sources=[adapter.status().model_dump() for adapter in adapters.values()]
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
                detail=f"Unknown source: {filter_req.source!r}. Available: {[s.value for s in LogSource]}",
            )

    # Validate min_level
    min_level = None
    if filter_req.min_level:
        try:
            min_level = LogLevel(filter_req.min_level)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown level: {filter_req.min_level!r}. Available: {[lv.value for lv in LogLevel]}",
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
