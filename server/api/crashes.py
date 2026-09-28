"""API routes for crash reports."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request

from server.api.actions import logged_action
from server.device.devicectl import canonical_device_id, spellings_of
from server.models import (
    ClearCrashesResponse,
    ClearDeviceCrashesRequest,
    ClearDeviceCrashesResponse,
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
from server.sources import ios_crash
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
    days: int = Query(
        default=ios_crash.DEFAULT_WINDOW_DAYS, ge=1, le=3650,
        description=(
            "iPhone: how far back the pull reaches. Older reports stay on the "
            "phone and are counted in pull.older_on_device."
        ),
    ),
    detail: bool = Query(
        default=False,
        description=(
            "Include each report's full frames and images (UUID, load address): "
            "what symbolicating it needs. Off by default: several kilobytes per crash."
        ),
    ),
    include_raw: bool = Query(
        default=False,
        description=(
            "Include each report's raw_text, the start of the report as written. Off "
            "by default: about a thousand tokens of JSON per crash."
        ),
    ),
) -> CrashLatestResponse:
    """Return recent crash reports.

    When ``udid`` is provided, fetches that device's crashes first: an iPhone
    over USB with pymobiledevice3 (the last ``days``), an Android device or
    emulator from its DropBox. ``pull`` says whether that happened -- pulled, skipped or
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
        pull = await _pull(request, crash_adapter, udid, days)

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
    # Compact by default: where it crashed, why, and the top frames. The full
    # frames and images are a few kilobytes a crash, which at the default limit
    # of ten swamped the answer -- more than the raw text it replaced.
    trimmed: dict = {}
    if not detail:
        trimmed.update(frames=[], images=[])
    if not include_raw:
        trimmed["raw_text"] = ""
    if trimmed:
        limited = [r.model_copy(update=trimmed) for r in limited]

    return CrashLatestResponse(crashes=limited, total=total, pull=pull)


async def _resolve(controller: DeviceController, udid: str) -> tuple[str, DeviceType | None]:
    """The device's canonical id and its kind, warming the type cache first.

    On a fresh server the cache is empty, and a cold `_is_android` sent an
    emulator's serial down the iPhone path to be told it was "not connected
    over USB" (see `_device_type`, and #305).
    """
    await controller._ensure_device_type_cached(canonical_device_id(udid))
    device = canonical_device_id(udid)      # again: the refresh records aliases
    return device, controller._device_type(device)


async def _pull(
    request: Request, crash_adapter: CrashAdapter, udid: str,
    days: int = ios_crash.DEFAULT_WINDOW_DAYS,
) -> CrashPullStatus:
    """Fetch the device's crash reports, and say plainly if that could not happen."""
    controller = request.app.state.device_controller
    if controller is None:
        return CrashPullStatus(
            udid=udid, status="skipped", reason="device controller not available",
        )

    device, kind = await _resolve(controller, udid)

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
        # crashes". The crash service is reached over USB.
        return CrashPullStatus(
            udid=udid, platform="ios", status="skipped",
            reason="not connected over USB; crash reports are pulled over USB",
        )
    # Report files do not say which phone they came from, so the pull says
    # it: these came from the device asked for.
    result = await crash_adapter.pull_from_device(lib_udid, device_id=device, days=days)
    left = dict(
        window_days=result.window_days, older_on_device=result.older_on_device,
        oldest_on_device=result.oldest_on_device, note=_left_behind(result),
    )
    if result.error:
        return CrashPullStatus(
            udid=udid, platform="ios", status="failed",
            new_reports=len(result.new), reason=result.error, **left,
        )
    return CrashPullStatus(
        udid=udid, platform="ios", status="pulled", new_reports=len(result.new), **left,
    )


def _left_behind(result) -> str | None:
    """Say what a pull left on the phone, and what to do about it."""
    if not result.older_on_device:
        return None
    return (
        f"{result.older_on_device} report(s) older than {result.window_days} day(s) "
        f"stay on the phone, the oldest from {result.oldest_on_device}. Pass a "
        "larger `days` to include them. clear_device_crashes deletes every crash "
        "report on the phone, these and the recent ones alike."
    )


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


@router.delete("", response_model=ClearCrashesResponse)
@logged_action("clear_crashes", category="logs")
async def clear_crashes(
    request: Request,
    udid: str | None = Query(
        default=None, description="Clear only this device's reports; omit for all.",
    ),
) -> ClearCrashesResponse:
    """Delete the crash reports quern stored on the Mac: one device's, or all.

    Clears quern's copies only. The device keeps its own, and a later pull
    lists again whatever it still holds within the pull's window -- without
    logging those as new crashes. Use clear_device_crashes to clear an iPhone.
    """
    crash_adapter = request.app.state.crash_adapter
    if crash_adapter is None:
        raise HTTPException(
            status_code=409, detail="crash capture is disabled (server started with --no-crash)",
        )
    if udid is not None and not udid.strip():
        # An empty udid read as "omitted" and cleared every device.
        raise HTTPException(status_code=400, detail="udid is empty; omit it to clear all devices")
    device = None
    if udid:
        controller = request.app.state.device_controller
        known = controller is not None and (await _resolve(controller, udid))[1] is not None
        device = canonical_device_id(udid)
        stored = any(r.device_id == device for r in crash_adapter.crash_reports)
        if not known and not stored:
            # An id quern has never seen is not "cleared" (#182): answering
            # success would tell teardown code it worked.
            raise HTTPException(
                status_code=404, detail=f"quern knows no device and no crash reports for {udid}",
            )
    result = await crash_adapter.clear(device)
    return ClearCrashesResponse(
        udid=udid, files_removed=result.files_removed,
        reports_removed=result.reports_removed, errors=result.errors,
    )


@router.post("/device/clear", response_model=ClearDeviceCrashesResponse)
@logged_action("clear_device_crashes", category="device.action")
async def clear_device_crashes(
    request: Request, body: ClearDeviceCrashesRequest,
) -> ClearDeviceCrashesResponse:
    """Permanently delete the crash reports on an iPhone.

    A deliberate action, never a side effect of reading: the reports are gone
    for Xcode, Finder and anything else that reads them too. Only the reports
    (`.ips`, `.crash` at the top of the crash directory), each by name --
    pymobiledevice3's own `crash clear` also removes DiagnosticLogs, where
    sysdiagnose archives live, and everything else there. quern's copies on
    the Mac stay, subject to the usual retention.
    """
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="device controller not available")
    device, kind = await _resolve(controller, body.udid)
    if kind in (DeviceType.ANDROID_DEVICE, DeviceType.ANDROID_EMULATOR):
        raise HTTPException(status_code=400, detail=(
            "clearing crash reports is not supported on Android: an unrooted "
            "device's DropBox can only be read, not cleared (dumpsys dropbox "
            "prints, and cmd dropbox only sets a rate limit)"
        ))
    if kind == DeviceType.SIMULATOR:
        raise HTTPException(status_code=400, detail=(
            "a simulator's crash reports are files on this Mac, in "
            "~/Library/Logs/DiagnosticReports; there is nothing on the device to clear"
        ))
    if kind != DeviceType.DEVICE:
        raise HTTPException(
            status_code=404,
            detail=f"quern does not know {body.udid}; list devices to check it is connected",
        )
    lib_udid = await controller.get_libimobiledevice_udid(device)
    if not lib_udid:
        raise HTTPException(status_code=409, detail=(
            "not connected over USB; crash reports are cleared over USB"
        ))
    if lib_udid not in spellings_of(device):
        # Matched by name (the fallback #323 is about), not by the phone's own
        # hardware UDID. Good enough to read from; not to delete from.
        raise HTTPException(status_code=409, detail=(
            "this phone's USB connection was matched by name, not by its hardware "
            "UDID, so quern will not delete from it; see #323"
        ))
    cmd = ios_crash.command()
    if not cmd:
        raise HTTPException(status_code=502, detail="pymobiledevice3 not found")
    try:
        names = await ios_crash.list_reports(cmd, lib_udid)
    except ios_crash.IosCrashError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    try:
        removed, failed = await ios_crash.remove_reports(lib_udid, names)
    except ios_crash.IosCrashError as e:
        # Reports go one at a time, so a timeout or crash partway may have
        # deleted some -- permanently. An error that says only why reads as
        # "nothing happened"; say what the phone holds now (CodeRabbit, #325).
        try:
            left = len(await ios_crash.list_reports(cmd, lib_udid))
            state = (f"{max(len(names) - left, 0)} of {len(names)} report(s) may already "
                     f"be deleted; {left} remain")
        except ios_crash.IosCrashError:
            state = "some reports may already be deleted; list again to check"
        raise HTTPException(status_code=502, detail=f"{e}; {state}") from e
    try:
        remaining = len(await ios_crash.list_reports(cmd, lib_udid))
    except ios_crash.IosCrashError as e:
        raise HTTPException(status_code=502, detail=(
            f"deleted {len(removed)} report(s), but could not list what remains: {e}"
        )) from e
    return ClearDeviceCrashesResponse(
        udid=body.udid, removed=len(removed), remaining=remaining, failed=failed,
    )
