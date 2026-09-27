"""API routes for crash reports."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Query, Request

from server.api.actions import logged_action
from server.models import (
    CrashLatestResponse,
    CrashPullStatus,
    CrashReport,
    LogQueryParams,
    LogSource,
    UtcDatetime,
)
from server.sources.android_dropbox import DropboxPullError, pull_dropbox

#: How far apart logcat's crash entry and DropBox's record of the same crash
#: may be stamped and still be the same crash.
LOGCAT_MATCH_WINDOW_S = 10

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/crashes", tags=["crashes"])


@router.get("/latest", response_model=CrashLatestResponse)
@logged_action("get_latest_crashes", category="logs")
async def get_latest_crashes(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    since: UtcDatetime | None = None,
    udid: str | None = Query(
        default=None,
        description="Device UDID to pull crashes from before returning",
    ),
) -> CrashLatestResponse:
    """Return recent crash reports.

    When ``udid`` is provided, pulls fresh crashes from the device via
    ``idevicecrashreport`` before returning results.  If the device is
    network-only (no USB connection), the pull is silently skipped.
    """
    crash_adapter = request.app.state.crash_adapter
    if crash_adapter is None:
        return CrashLatestResponse(
            crashes=[], total=0,
            pull=CrashPullStatus(
                udid=udid, status="skipped",
                reason="crash capture is disabled (server started with --no-crash)",
            ) if udid else None,
        )

    pull: CrashPullStatus | None = None
    if udid:
        pull = await _pull(request, crash_adapter, udid)

    reports = crash_adapter.crash_reports

    if since:
        reports = [r for r in reports if r.timestamp >= since]

    # Most recent first
    reports = sorted(reports, key=lambda r: r.timestamp, reverse=True)
    total = len(reports)
    limited = reports[:limit]

    return CrashLatestResponse(crashes=limited, total=total, pull=pull)


async def _pull(request: Request, crash_adapter, udid: str) -> CrashPullStatus:
    """Fetch the device's crash reports, and say plainly if that could not happen."""
    controller = request.app.state.device_controller
    if controller is None:
        return CrashPullStatus(
            udid=udid, status="skipped", reason="device controller not available",
        )

    if controller._is_android(udid):
        # DropBox, which an unrooted phone serves to the shell user; see
        # server/sources/android_dropbox.py. There was no Android pull at all
        # before #316, so `get_latest_crash` returned nothing after a crash.
        try:
            reports = await pull_dropbox(controller.adb.adb_path, udid)
        except DropboxPullError as e:
            return CrashPullStatus(udid=udid, platform="android", status="failed", reason=str(e))
        new = await crash_adapter.add_reports(
            reports, already_logged=lambda r: _logged_by_logcat(request, r),
        )
        return CrashPullStatus(
            udid=udid, platform="android", status="pulled", new_reports=len(new),
        )

    lib_udid = await controller.get_libimobiledevice_udid(udid)
    if not lib_udid:
        # Was a debug log line, while the response looked like "no new
        # crashes". idevicecrashreport needs the phone on USB.
        return CrashPullStatus(
            udid=udid, platform="ios", status="skipped",
            reason="not connected over USB; idevicecrashreport needs a USB connection",
        )
    result = await crash_adapter.pull_from_device(lib_udid)
    if result.error:
        return CrashPullStatus(
            udid=udid, platform="ios", status="failed",
            new_reports=len(result.new), reason=result.error,
        )
    return CrashPullStatus(udid=udid, platform="ios", status="pulled", new_reports=len(result.new))


async def _logged_by_logcat(request: Request, report: CrashReport) -> bool:
    """Is this crash already on the timeline, recognised by logcat as it happened?

    The logcat adapter emits a crash entry when capture is running (#255), so
    a DropBox pull of the same crash would put it there twice. Matched by
    device and process within a few seconds: logcat stamps the first line of
    the crash, DropBox the moment it was filed.
    """
    crash_buffer = getattr(request.app.state, "crash_buffer", None)
    if crash_buffer is None:
        return False
    window = timedelta(seconds=LOGCAT_MATCH_WINDOW_S)
    seen = await crash_buffer.filter_entries(LogQueryParams(
        source=LogSource.CRASH, device_id=report.device_id,
        since=report.timestamp - window, until=report.timestamp + window,
    ))
    return any(e.process == report.process and e.id.startswith("android-crash-") for e in seen)
