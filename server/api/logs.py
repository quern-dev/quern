"""API routes for log streaming, querying, and source management."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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
    source: str
    process: str | None = None
    exclude_patterns: list[str] | None = None


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
                except asyncio.TimeoutError:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({
                            "time": datetime.now(timezone.utc).isoformat(),
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
        entries = all_entries[offset : offset + limit]

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
        cutoff = datetime.now(timezone.utc) - duration
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
async def set_filter(request: Request, filter_req: FilterRequest) -> dict[str, str]:
    """Reconfigure capture filters for a source adapter.

    Spec (phase1-architecture.md): Let agents dynamically narrow log capture
    without restarting the server — e.g., filter to a single process or exclude
    noisy subsystems. Request body: {"source": "syslog", "process": "MyApp",
    "exclude_patterns": ["noise_keyword"]}.

    Current limitation: BaseSourceAdapter only accepts filter args at construction
    (process_filter, subsystem_filter, etc.). There is no reconfigure() method.
    Implementation path: stop the adapter, rebuild it with new filter args, restart
    it, and re-wire the on_entry callback. The exclude_patterns field would need
    new support in the adapters or the processing pipeline.
    """
    # TODO: Implement dynamic filter reconfiguration (see docstring for plan)
    return {"status": "accepted", "note": "Filter reconfiguration not yet implemented"}
