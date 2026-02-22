"""API routes for crash reports."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query, Request

from server.models import CrashLatestResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/crashes", tags=["crashes"])


@router.get("/latest", response_model=CrashLatestResponse)
async def get_latest_crashes(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    since: datetime | None = None,
    udid: str | None = Query(default=None, description="Device UDID to pull crashes from before returning"),
) -> CrashLatestResponse:
    """Return recent crash reports.

    When ``udid`` is provided, pulls fresh crashes from the device via
    ``idevicecrashreport`` before returning results.  If the device is
    network-only (no USB connection), the pull is silently skipped.
    """
    crash_adapter = request.app.state.crash_adapter
    if crash_adapter is None:
        return CrashLatestResponse(crashes=[], total=0)

    # On-demand pull from a specific device
    if udid:
        device_controller = request.app.state.device_controller
        if device_controller:
            lib_udid = await device_controller.get_libimobiledevice_udid(udid)
            if lib_udid:
                await crash_adapter.pull_from_device(lib_udid)
            else:
                logger.debug(
                    "No libimobiledevice UDID for %s (network-only?), skipping pull",
                    udid[:8],
                )

    reports = crash_adapter.crash_reports

    if since:
        reports = [r for r in reports if r.timestamp >= since]

    # Most recent first
    reports = sorted(reports, key=lambda r: r.timestamp, reverse=True)
    total = len(reports)
    limited = reports[:limit]

    return CrashLatestResponse(crashes=limited, total=total)
