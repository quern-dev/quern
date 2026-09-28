"""API routes for crash reports."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request

from server.api.actions import logged_action
from server.device.devicectl import canonical_device_id
from server.models import (
    CrashLatestResponse,
    CrashPullStatus,
    CrashReport,
    DeviceType,
    LogEntry,
    LogQueryParams,
    LogSource,
    OpenCrashDialog,
    UtcDatetime,
)
from server.sources.android_dropbox import DropboxPullError, pull_dropbox
from server.sources.crash import DIAGNOSTIC_REPORTS_DIR

if TYPE_CHECKING:
    from server.device.controller import DeviceController
    from server.sources.crash import CrashAdapter

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

    When ``udid`` is provided, fetches that device's crashes first: an iPhone
    over USB with ``idevicecrashreport``, an Android device or emulator from
    its DropBox. ``pull`` says whether that happened -- pulled, skipped or
    failed, with the reason -- and the list is then that device's crashes,
    plus reports that name no device.
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
    if udid:
        # That device's crashes -- plus reports that name no device (a
        # simulator's crash file does not say which simulator), so nothing that
        # showed before goes missing. Returning every device's crashes for one
        # device's udid put the emulator's crash at the top of a Pixel's list
        # once Android reports carried their device (#316, live test).
        device = canonical_device_id(udid)
        reports = [r for r in reports if not r.device_id or r.device_id == device]

    if since:
        reports = [r for r in reports if r.timestamp >= since]

    # Most recent first
    reports = sorted(reports, key=lambda r: r.timestamp, reverse=True)
    total = len(reports)
    limited = reports[:limit]

    return CrashLatestResponse(crashes=limited, total=total, pull=pull)


async def _pull(request: Request, crash_adapter: CrashAdapter, udid: str) -> CrashPullStatus:
    """Fetch the device's crash reports, and say plainly if that could not happen."""
    controller = request.app.state.device_controller
    if controller is None:
        return CrashPullStatus(
            udid=udid, status="skipped", reason="device controller not available",
        )

    # Warm the type cache first: on a fresh server it is empty, and a cold
    # `_is_android` sent an emulator's serial down the iPhone path to be
    # told it was "not connected over USB" (see `_device_type`, and #305).
    await controller._ensure_device_type_cached(canonical_device_id(udid))
    device = canonical_device_id(udid)      # again: the refresh records aliases
    kind = controller._device_type(device)

    if kind in (DeviceType.ANDROID_DEVICE, DeviceType.ANDROID_EMULATOR):
        return await _pull_android(request, crash_adapter, controller, udid)
    if kind == DeviceType.SIMULATOR:
        # Nothing to fetch: a simulator's crash reports are written on this
        # Mac. Said as a skip with the reason, rather than the USB message
        # every simulator used to get.
        watching = DIAGNOSTIC_REPORTS_DIR in crash_adapter.extra_watch_dirs
        return CrashPullStatus(
            udid=udid, platform="ios", status="skipped",
            reason=(
                "a simulator's crash reports are written on this Mac and read "
                "continuously from ~/Library/Logs/DiagnosticReports; there is nothing to pull"
                if watching else
                "a simulator's crash reports are read from ~/Library/Logs/DiagnosticReports, "
                "and that is off (server started with --no-simulator-crashes)"
            ),
        )
    if kind != DeviceType.DEVICE:
        return CrashPullStatus(
            udid=udid, status="skipped",
            reason="quern does not know this device; list devices to check it is connected",
        )

    lib_udid = await controller.get_libimobiledevice_udid(device)
    if not lib_udid:
        # Was a debug log line, while the response looked like "no new
        # crashes". idevicecrashreport needs the phone on USB.
        return CrashPullStatus(
            udid=udid, platform="ios", status="skipped",
            reason="not connected over USB; idevicecrashreport needs a USB connection",
        )
    # idevicecrashreport writes files that do not say which phone they came
    # from, so the pull says it: these came from the device asked for.
    result = await crash_adapter.pull_from_device(lib_udid, device_id=device)
    if result.error:
        return CrashPullStatus(
            udid=udid, platform="ios", status="failed",
            new_reports=len(result.new), reason=result.error,
        )
    return CrashPullStatus(udid=udid, platform="ios", status="pulled", new_reports=len(result.new))


async def _pull_android(
    request: Request, crash_adapter: CrashAdapter, controller: DeviceController, udid: str,
) -> CrashPullStatus:
    """DropBox, which an unrooted phone serves to the shell user.

    See server/sources/android_dropbox.py. There was no Android pull at all
    before #316, so `get_latest_crash` returned nothing after a crash.
    """
    try:
        pulled = await pull_dropbox(controller.adb.adb_path, udid)
    except DropboxPullError as e:
        return CrashPullStatus(udid=udid, platform="android", status="failed", reason=str(e))
    new = await crash_adapter.add_reports(
        pulled.reports, already_logged=lambda r: _logged_by_logcat(request, r),
    )
    # Records read before a failure are kept, but the pull did not succeed:
    # "pulled" promises the list reflects the device, and it does not.
    problems = list(pulled.errors)
    if pulled.undated:
        problems.append(
            f"{pulled.undated} record(s) skipped: the device's timezone could not be read, "
            "so their times could not be converted"
        )
    return CrashPullStatus(
        udid=udid, platform="android", status="failed" if problems else "pulled",
        new_reports=len(new), reason="; ".join(problems) or None,
        open_dialogs=None if pulled.open_dialogs is None else [
            OpenCrashDialog(process=proc, kind=kind)
            for proc, kind in sorted(pulled.open_dialogs.items())
        ],
    )


def _same_process(entry: LogEntry, report: CrashReport) -> bool:
    """Is logcat's crash entry about the process this report names?

    By pid where both have one. By name otherwise, allowing for a native
    crash, which logcat names by the kernel's 15-character process name --
    the tail of the package (`ndroid.settings` for `com.android.settings`).
    Comparing names alone logged every native crash twice.
    """
    if entry.pid is not None and report.pid is not None:
        return entry.pid == report.pid
    if entry.process == report.process:
        return True
    return len(entry.process) == 15 and report.process.endswith(entry.process)


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
    return any(e.id.startswith("android-crash-") and _same_process(e, report) for e in seen)
