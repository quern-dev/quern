"""Recordings: a device's actions, flows and logs, written to disk as they arrive (#364).

The routes are thin; `server/recording.py` holds the recorder and says why it
is shaped the way it is.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from server import recording as recording_mod
from server.api.actions import logged_action
from server.api.trace import _proxy_is_running
from server.device.devicectl import canonical_device_id
from server.models import RecordingStartRequest, UtcDatetime
from server.recording import KINDS, Filters, RecordingError, RecordingManager

router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])


def _manager(request: Request) -> RecordingManager:
    manager = getattr(request.app.state, "recordings", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="recordings are not available: the "
                                                    "server has not finished starting")
    return manager


@router.post("")
@logged_action("start_recording", category="proxy")
async def start_recording(request: Request, body: RecordingStartRequest) -> dict:
    """Start writing one device's actions, flows and logs to disk, until stopped."""
    manager = _manager(request)
    # Canonical, because actions and logs record a physical device under one
    # of its two identifiers: the other spelling would match nothing.
    udid = canonical_device_id(body.udid)
    warnings = []
    controller = getattr(request.app.state, "device_controller", None)
    if controller is not None:
        try:
            await controller._ensure_device_type_cached(udid)
            known = controller._device_type(udid) is not None
        except Exception:  # noqa: BLE001 -- a lookup that fails must not refuse the recording
            known = None
        if known is False:
            # Recorded anyway -- the device may boot after the recording
            # starts -- but said, because a typo would otherwise record
            # nothing for 90 minutes and look like a quiet run.
            warnings.append(f"{udid} is not a device quern knows right now; recording "
                            f"anyway, in case it appears")
    try:
        rec = await manager.start(udid, body.output_dir, Filters(
            kinds=tuple(body.kinds or KINDS), hosts=body.hosts,
            exclude_hosts=body.exclude_hosts, include_unattributed=body.include_unattributed))
    except RecordingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if "flows" in rec.filters.kinds and not _proxy_is_running(request):
        warnings.append("the proxy is not running, so no flows will be recorded until it is")
    return {**rec.summary(), "warnings": [*warnings, *rec.warnings]}


@router.post("/{recording_id}/stop")
@logged_action("stop_recording", category="proxy")
async def stop_recording(request: Request, recording_id: str) -> dict:
    """Stop a recording, write its last lines and its manifest, and say whether it is complete."""
    try:
        rec = await _manager(request).stop(recording_id)
    except RecordingError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return rec.summary()


def recording_dir(request: Request, ref: str) -> Path:
    """A recording by id on this server, or by the directory it was written to."""
    manager = getattr(request.app.state, "recordings", None)
    if manager is not None:
        try:
            return manager.get(ref).dir
        except RecordingError:
            pass
    directory = Path(ref).expanduser()
    if not (directory / recording_mod.EVENTS).is_file():
        raise HTTPException(status_code=404,
                            detail=f"no recording {ref!r}: not an id this server knows, nor a "
                                   f"directory holding {recording_mod.EVENTS}")
    return directory


@router.get("/events")
async def recording_events(
    request: Request,
    recording: Annotated[str, Query(description="The recording's id, or its directory")],
    kinds: Annotated[str | None, Query(description=(
        "Comma-separated, any of actions, flows, logs. Default all."))] = None,
    since: Annotated[UtcDatetime | None, Query()] = None,
    until: Annotated[UtcDatetime | None, Query()] = None,
    cursor: Annotated[int, Query(ge=0, description="next_cursor from the previous page")] = 0,
    limit: Annotated[int, Query(ge=1, le=recording_mod.MAX_PAGE)] = 500,
    detail: Annotated[Literal["full", "summary"], Query()] = "full",
    flow_id: Annotated[str | None, Query(description="Only this flow, in full")] = None,
) -> dict:
    """A recording's events -- any combination of actions, flows and logs -- for a window."""
    directory = recording_dir(request, recording)
    chosen = tuple(k.strip() for k in kinds.split(",") if k.strip()) if kinds else KINDS
    if unknown := [k for k in chosen if k not in KINDS]:
        raise HTTPException(status_code=400, detail=f"unknown kinds {unknown}: choose from "
                                                    f"{', '.join(KINDS)}")
    try:
        page = await asyncio.to_thread(
            recording_mod.read_events, directory, chosen, since=since, until=until,
            cursor=cursor, limit=limit, detail=detail, flow_id=flow_id)
        manager = getattr(request.app.state, "recordings", None)
        live = bool(manager and manager.is_live(directory))
        # Markers only: a page must not rebuild every flow in the recording
        # just to say where its holes are (review).
        loaded_holes = await asyncio.to_thread(recording_mod.load, directory, live=live,
                                               markers_only=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"{directory} could not be read: {e}") from e
    return {
        "directory": str(directory), "kinds": list(chosen), **page,
        # What the file says it does not hold in this window, for these kinds.
        "holes": recording_mod.holes_in(loaded_holes, recording_mod.event_types(chosen),
                                        since, until),
        "stopped": loaded_holes.stopped,
        "warnings": loaded_holes.warnings,
    }


@router.get("")
async def list_recordings(request: Request) -> dict:
    """The recordings this server is making or has made since it started."""
    return {"recordings": [r.summary() for r in _manager(request).list()]}
