"""Profiling an app with Instruments: start, stop, read, download (#245)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from server import device_access, visibility
from server.api.actions import logged_action
from server.api.device import _get_controller, _handle_device_error
from server.config import profile_settings
from server.device.ios.devicectl import spellings_of
from server.logging_ext import current_action
from server.models import DeviceError, DeviceType, StartProfileRequest
from server.profiling import xctrace as xt
from server.profiling.manager import ID, ProfileError, ProfileManager

router = APIRouter(prefix="/api/v1/profiles", tags=["profiles"])

#: What a profile serves. The trace is a bundle (a directory), so it is
#: zipped on request rather than kept twice.
FILES = ("summary.json", "xctrace.log", "profile.trace.zip")
_ID = ID

RELAUNCH_NOTE = (
    "xctrace launched {app} for this profile and terminates it when the profile stops: "
    "anything the agent had on screen in it is gone. Relaunch it to carry on.")
#: Measured: taps right after start found nothing, and on an iPhone the
#: first one answered after 12.7 s; the app launches slowly under Instruments.
LAUNCHING_NOTE = (
    "{app} is still launching, slowly under Instruments: wait for its first screen "
    "(wait_for_element) before driving it.")


def _manager(request: Request) -> ProfileManager:
    manager = getattr(request.app.state, "profiles", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="profiling is not available")
    return manager


def _error(e: ProfileError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def _own(request: Request, profile_id: str):
    """A profile the caller may see: any for the owner, its own for a client
    key. Another's reads as missing, so ids do not leak who profiled what."""
    if not _ID.fullmatch(profile_id):
        raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
    caller = visibility.current_caller()
    try:
        profile = _manager(request).get(profile_id)
    except ProfileError as e:
        raise _error(e) from e
    if not (caller.is_owner or profile.principal == caller.principal):
        raise HTTPException(status_code=404, detail=f"no profile {profile_id!r}")
    return profile


def _hardware_udid(udid: str) -> str:
    """The spelling xctrace's --device takes for a physical device: its
    hardware UDID (00008030-...), not quern's CoreDevice UUID."""
    others = [s for s in spellings_of(udid) if s != udid]
    return others[0] if others else udid


@router.post("")
@logged_action("start_profile", category="device.action")
async def start_profile(request: Request, body: StartProfileRequest) -> dict:
    """Record an app with Instruments. xctrace launches the app."""
    controller = _get_controller(request)
    manager = _manager(request)
    try:
        # Resolving checks the caller may change this device, and puts the
        # device on the action, so the profile shows in that device's trace.
        udid = await controller.resolve_udid(body.udid)
        await controller._ensure_device_type_cached(udid)
    except DeviceError as e:
        raise _handle_device_error(e) from e
    kind = controller._device_type(udid)
    if kind == DeviceType.SIMULATOR:
        platform, device = "simulator", udid
    elif kind == DeviceType.DEVICE:
        platform, device = "device", _hardware_udid(udid)
    elif kind in (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE):
        raise HTTPException(status_code=400, detail=(
            "profiling Android is not available yet (#245 phase 4); get_performance reads "
            "an Android app's CPU, memory and frames now"))
    else:
        raise HTTPException(status_code=400, detail=f"could not tell what kind of device {udid} is")

    async def warm_up(target: str) -> None:
        # One devicectl call brings CoreDevice's tunnel up, and xctrace then
        # holds it for the recording (measured 3/3 from cold).
        await controller.devicectl._run_devicectl("device", "info", "details", "--device", target)

    caller = visibility.current_caller()
    try:
        profile, room = await manager.start(
            udid=udid, platform=platform, device=device, bundle_id=body.bundle_id,
            preset=body.preset, time_limit_s=body.time_limit_s, principal=caller.principal,
            warm_up=warm_up)
    except ProfileError as e:
        raise _error(e) from e
    view = await asyncio.to_thread(profile.view)
    notes = [RELAUNCH_NOTE.format(app=body.bundle_id), LAUNCHING_NOTE.format(app=body.bundle_id)]
    # The budget is the server's, so a start can remove anyone's oldest. Only
    # the caller's own are named: another principal's ids are not its to see.
    mine = [e["id"] for e in room["evicted"] if e["principal"] == caller.principal]
    others = len(room["evicted"]) - len(mine)
    if room["evicted"]:
        removed = ", ".join(mine + ([f"{others} of other keys'"] if others else []))
        notes.append(f"removed older profiles to stay within the "
                     f"{room['budget_bytes'] // 2**20} MB kept for profiles: {removed}")
    if note := profile_settings()["note"]:
        notes.append(note)
    answer = {**view, "purpose": xt.PRESETS[body.preset].purpose,
              "stops_at": f"after {body.time_limit_s}s, or on stop_profile", "notes": notes}
    if caller.is_owner:
        answer["disk"] = {"used_bytes": room["used_bytes"], "budget_bytes": room["budget_bytes"]}
    return answer


@router.post("/{profile_id}/stop")
@logged_action("stop_profile", category="device.action")
async def stop_profile(request: Request, profile_id: str) -> dict:
    """Stop early, wait for xctrace to finish the trace, and summarise it."""
    profile = _own(request, profile_id)
    # An id-addressed route names no device for the middleware to check.
    device_access.check(profile.udid)
    current_action().udid = profile.udid
    try:
        profile = await _manager(request).stop(profile_id)
    except ProfileError as e:
        raise _error(e) from e
    return await asyncio.to_thread(profile.view)


@router.get("")
async def list_profiles(request: Request) -> dict:
    """Every kept profile the caller may see, oldest first, without summaries."""
    caller = visibility.current_caller()
    mine = [p for p in _manager(request).list()
            if caller.is_owner or p.principal == caller.principal]
    views = await asyncio.to_thread(lambda: [p.view() for p in mine])
    for view in views:
        view.pop("summary", None)
    return {"profiles": views}


@router.get("/{profile_id}")
async def get_profile(request: Request, profile_id: str) -> dict:
    """One profile: its state and, once finished, its summary."""
    return await asyncio.to_thread(_own(request, profile_id).view)


@router.get("/{profile_id}/files/{name}")
async def get_profile_file(request: Request, profile_id: str, name: str):
    """summary.json, xctrace.log, or the trace bundle as profile.trace.zip."""
    profile = _own(request, profile_id)
    if name not in FILES:
        raise HTTPException(status_code=404, detail=f"{profile_id} has no file {name!r}")
    if name == "profile.trace.zip":
        try:
            path = await _manager(request).zip_trace(profile)
        except ProfileError as e:
            raise _error(e) from e
        return FileResponse(path, media_type="application/zip", filename=name,
                            background=BackgroundTask(_remove, path))
    path = profile.dir / name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"{profile_id} has no {name} yet")
    return FileResponse(path, filename=name, media_type=(
        "application/json" if name.endswith(".json") else "text/plain"))


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
