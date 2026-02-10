"""API routes for log streaming, querying, and source management."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from server.models import LogEntry, LogLevel, LogQueryParams, LogSource, LogStreamParams

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


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
    device_id: str = "default",
) -> EventSourceResponse:
    """Stream log entries in real time via Server-Sent Events."""
    buffer = request.app.state.ring_buffer
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
        queue = buffer.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if matches_filter(entry):
                        yield {
                            "event": "log",
                            "data": entry.model_dump_json(),
                        }
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({
                            "time": datetime.now(timezone.utc).isoformat(),
                            "buffer_size": buffer.size,
                        }),
                    }
        finally:
            buffer.unsubscribe(queue)

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
    device_id: str = "default",
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> LogQueryResponse:
    """Query historical log entries with filters and pagination."""
    buffer = request.app.state.ring_buffer

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

    entries, total = await buffer.query(params)
    return LogQueryResponse(
        entries=entries,
        total=total,
        has_more=(offset + limit) < total,
    )


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

    Note: In Phase 1a, this restarts the adapter with new filter settings.
    """
    # TODO: Implement dynamic filter reconfiguration
    return {"status": "accepted", "note": "Filter reconfiguration not yet implemented"}
