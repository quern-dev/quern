"""API routes for crash reports."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query, Request

from server.models import CrashLatestResponse

router = APIRouter(prefix="/api/v1/crashes", tags=["crashes"])


@router.get("/latest", response_model=CrashLatestResponse)
async def get_latest_crashes(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    since: datetime | None = None,
) -> CrashLatestResponse:
    """Return recent crash reports."""
    crash_adapter = request.app.state.crash_adapter
    if crash_adapter is None:
        return CrashLatestResponse(crashes=[], total=0)

    reports = crash_adapter.crash_reports

    if since:
        reports = [r for r in reports if r.timestamp >= since]

    # Most recent first
    reports = sorted(reports, key=lambda r: r.timestamp, reverse=True)
    total = len(reports)
    limited = reports[:limit]

    return CrashLatestResponse(crashes=limited, total=total)
