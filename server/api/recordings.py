"""Recordings: a device's actions, flows and logs, written to disk as they arrive (#364).

The routes are thin; `server/recording/recorder.py` holds the recorder and says why it
is shaped the way it is.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from server import visibility
from server.api.actions import logged_action
from server.api.trace import _proxy_is_running
from server.device.ios.devicectl import canonical_device_id
from server.models import DeviceType, RecordingKeyframeRequest, RecordingStartRequest, UtcDatetime
from server.recording import recorder as recording_mod
from server.recording.recorder import (
    KEYFRAME_TRIGGERS,
    KINDS,
    Filters,
    RecordingError,
    RecordingManager,
    RecordingNotFilming,
)

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
    if body.viewer:
        _may_issue_viewer_links()
    if body.viewer and not body.video:
        # The viewer relays the recording's movie; without one there is
        # nothing to show, and a link that never plays is worse than none.
        raise HTTPException(status_code=400, detail="a viewer link needs video: pass video=true")
    if body.video:
        # quern-media captures a simulator's framebuffer; a phone or an
        # emulator would be accepted and give no movie.
        kind = controller._device_type(udid) if controller is not None else None
        if kind != DeviceType.SIMULATOR:
            raise HTTPException(status_code=400,
                                detail=f"video records simulators; {udid} is "
                                       f"{kind.value if kind else 'not a device quern knows'}")
    # Before anything is written: a recording of flows from a simulator that
    # cannot capture them is refused or made able to, not started and empty (#414).
    # What the recording would refuse is refused first: a refusal after the CA
    # check below could follow a CA it had installed for nothing (review).
    try:
        filters = Filters(
            kinds=tuple(body.kinds or KINDS), hosts=body.hosts,
            exclude_hosts=body.exclude_hosts, include_unattributed=body.include_unattributed,
            video=body.video, bodies=body.bodies, max_body_bytes=body.max_body_bytes,
            exclude_content_types=body.exclude_content_types,
            keyframes=tuple(body.keyframes) if body.keyframes is not None
            else KEYFRAME_TRIGGERS,
            gauges=body.gauges, gauge_interval_s=body.gauge_interval_s)
        await manager.check_start(udid, body.output_dir, filters)
    except RecordingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    simulator_tls = None
    if "flows" in filters.kinds:
        from server.proxy import sim_tls
        try:
            check = await sim_tls.ensure_capturable(
                request.app, udid, allow_passthrough=body.allow_passthrough)
        except sim_tls.CaptureNotReady as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail) from e
        simulator_tls = check.entry
        warnings.extend(check.warnings)
        if (simulator_tls is not None and simulator_tls.tls != "decrypted"
                and simulator_tls.reason not in check.warnings):
            warnings.append(f"this simulator's HTTPS is passed through, not recorded: "
                            f"{simulator_tls.reason}")
    try:
        caller = visibility.current_caller()
        rec = await manager.start(udid, body.output_dir, filters,
                                  requested_by=body.requested_by,
                                  principal=caller.principal,
                                  blind=visibility.blind_for(caller))
    except RecordingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if "flows" in rec.filters.kinds and not _proxy_is_running(request):
        warnings.append("the proxy is not running, so no flows will be recorded until it is")
    viewer_url = None
    if body.viewer:
        viewer_url, why = _issue_viewer_link(request, rec.id)
        if why:
            warnings.append(why)
    return {**rec.summary(), "warnings": [*warnings, *rec.warnings],
            "viewer_url": viewer_url if body.viewer else None,
            # What is in effect for this simulator now, not what was asked for.
            "simulator_tls": simulator_tls.model_dump() if simulator_tls else None}


CLIENT_VIEWER_LINKS_OFF = (
    "this server's owner has turned off viewer links for client keys "
    "(remote.client_viewer_links); ask them for one")


def _may_issue_viewer_links() -> None:
    """Refuse a client key a viewer link when the owner has turned them off
    (#559). The owner may always issue one."""
    from server.config import client_viewer_links
    if not visibility.current_caller().is_owner and not client_viewer_links():
        raise HTTPException(status_code=403, detail=CLIENT_VIEWER_LINKS_OFF)


def _viewer_recording(request: Request, recording_id: str, *, issuing: bool = True):
    """The recording a viewer link is asked for, if the caller may have one:
    any for the owner; for a client key, its own (#559). Another's is 403 --
    or 404 to a blind client, to which it does not exist. Revoking is allowed
    with client links turned off: it only takes access away."""
    caller = visibility.current_caller()
    if issuing:
        _may_issue_viewer_links()
    missing = HTTPException(status_code=404, detail=f"no recording {recording_id!r}")
    try:
        rec = _manager(request).get(recording_id)
    except RecordingError as e:
        # The same words as another's recording to a blind client, or the
        # difference says which ids exist (review).
        raise missing from e
    if not caller.is_owner and rec.principal != caller.principal:
        if visibility.blind_for(caller):
            raise missing
        raise HTTPException(status_code=403, detail=(
            f"{recording_id} is not your recording; only its owner or the server's "
            "owner can issue its viewer links"))
    return rec


def _issue_viewer_link(request: Request, recording_id: str) -> tuple[str | None, str | None]:
    """(viewer URL, reason there is none) for a recording (#449)."""
    viewer = getattr(request.app.state, "viewer", None)
    if viewer is None:
        return None, ("no viewer link: the LAN viewer is off. Set viewer.enabled in "
                      "~/.quern/config.json and restart quern")
    if viewer.base_url is None:
        return None, f"no viewer link: the viewer listener is not running ({viewer.error})"
    return viewer.url_for(viewer.grants.issue(recording_id)), None


@router.post("/{recording_id}/viewer")
@logged_action("recording_viewer_link", category="media")
async def recording_viewer_link(request: Request, recording_id: str) -> dict:
    """Issue another viewer link for a recording that is filming (#449) --
    by a client key, for its own recordings (#559)."""
    rec = _viewer_recording(request, recording_id)
    if not rec.filters.video or rec.state != "recording":
        raise HTTPException(status_code=409,
                            detail=f"{recording_id} is not filming, so there is nothing to view")
    url, why = _issue_viewer_link(request, recording_id)
    if url is None:
        raise HTTPException(status_code=503, detail=why)
    return {"recording_id": recording_id, "viewer_url": url}


@router.delete("/{recording_id}/viewer")
@logged_action("recording_viewer_revoke", category="media")
async def recording_viewer_revoke(request: Request, recording_id: str) -> dict:
    """Revoke every viewer link for a recording at once (#449) -- by a client
    key, for its own recordings (#559)."""
    _viewer_recording(request, recording_id, issuing=False)
    viewer = getattr(request.app.state, "viewer", None)
    revoked = viewer.grants.revoke(recording_id) if viewer is not None else 0
    return {"recording_id": recording_id, "revoked": revoked}


def _with_rejections(request: Request, summary: dict) -> dict:
    """A recording whose simulator refused the proxy's certificate says so (#414):
    those requests failed in the app and never became flows, so the recording
    alone reads as a quiet run."""
    from server.proxy import sim_tls
    note = sim_tls.rejection_note(request.app, summary.get("udid"),
                                  since=summary.get("started_at"),
                                  until=summary.get("stopped_at"))
    if note:
        summary = {**summary, "warnings": [*summary.get("warnings", []), note]}
    return summary


def _check_recording_device(request: Request, recording_id: str) -> None:
    """A recording is addressed by its id, so its device never reaches the
    access middleware: stopping, or marking, the recording of a device another
    session holds is checked against that device here (#254 review)."""
    from server import device_access

    try:
        rec = _manager(request).get(recording_id)
    except Exception:  # noqa: BLE001 -- an unknown id is the route's own 404
        return
    device_access.check(getattr(rec, "udid", None))


@router.post("/{recording_id}/stop")
@logged_action("stop_recording", category="proxy")
async def stop_recording(request: Request, recording_id: str) -> dict:
    """Stop a recording, write its last lines and its manifest, and say whether it is complete."""
    _check_recording_device(request, recording_id)
    try:
        rec = await _manager(request).stop(recording_id)
    except RecordingError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _with_rejections(request, rec.summary())


@router.post("/{recording_id}/keyframe")
@logged_action("recording_keyframe", category="proxy")
async def recording_keyframe(request: Request, recording_id: str,
                             body: RecordingKeyframeRequest | None = None) -> dict:
    """Ask the recording's movie for a keyframe now, so this moment is a seek
    point (#415) -- for a driver quern does not see, such as a CI step or a
    test marking the moment it cares about. The moment is written into the
    recording as a `mark`, with the label if one is given."""
    _check_recording_device(request, recording_id)
    try:
        asked = await _manager(request).keyframe(recording_id,
                                                 label=body.label if body else None)
    except RecordingNotFilming as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except RecordingError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"recording_id": recording_id, "requested": asked}


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
        "Comma-separated, any of actions, flows, logs, gauges. Default actions, flows and "
        "logs: gauges, a sample a second, are read when named."))] = None,
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
    if unknown := [k for k in chosen if k not in recording_mod.READ_KINDS]:
        raise HTTPException(status_code=400, detail=f"unknown kinds {unknown}: choose from "
                                                    f"{', '.join(recording_mod.READ_KINDS)}")
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


#: The files a recording writes, and so the only names served from its
#: directory: whatever else is there is not the recording's to hand out.
_FILE = re.compile(r"(events\.jsonl|manifest\.json|video-[0-9]{1,4}\.(mp4|log))")


_REC_ID = re.compile(r"rec_[0-9a-f]{12}")


def _from_disk(recording_id: str):
    """A recording this server made before it restarted: found in the default
    directory by id, its principal read from its manifest -- an old one with
    none is the owner's. Only the running and interrupted are kept across a
    restart, so a client's finished recording otherwise could never be
    downloaded again (review)."""
    import json
    from types import SimpleNamespace

    from server.config import CONFIG_DIR

    if not _REC_ID.fullmatch(recording_id):
        return None
    directory = CONFIG_DIR / "recordings" / recording_id
    try:
        manifest = json.loads((directory / recording_mod.MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    if directory.is_symlink() or not isinstance(manifest, dict):
        return None
    who = manifest.get("principal")
    return SimpleNamespace(id=recording_id, dir=directory,
                           principal=who if isinstance(who, str) and who else "owner")


def _own_recording(request: Request, recording_id: str):
    """A recording the caller may download: any for the owner, its own
    principal's for a client key -- another's reads as missing (#254, 3b)."""
    caller = visibility.current_caller()
    try:
        rec = _manager(request).get(recording_id)
    except RecordingError:
        rec = _from_disk(recording_id)
    if rec is None or not (caller.is_owner or rec.principal == caller.principal):
        raise HTTPException(status_code=404, detail=f"no recording {recording_id!r}")
    return rec


def _stat_sized(path: Path):
    """(size, chunks): the file opened without following a symlink, and read
    up to the size it had when opened. A recording still being written grows
    while it is sent, and a response longer than its Content-Length was
    dropped mid-way with a traceback (review)."""
    import os

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    # A file object now, not a bare descriptor: a stream abandoned before it
    # starts never enters the generator, and the file closes when collected
    # (CodeRabbit). The route also closes it when the response ends.
    f = os.fdopen(fd, "rb")
    size = os.fstat(fd).st_size

    def chunks():
        left = size
        with f:
            while left > 0:
                data = f.read(min(left, 1024 * 1024))
                if not data:
                    break
                left -= len(data)
                yield data

    return size, chunks(), f


def _files_of(rec) -> list[Path]:
    try:
        children = sorted(rec.dir.iterdir())
    except OSError:
        return []
    return [p for p in children
            if _FILE.fullmatch(p.name) and p.is_file() and not p.is_symlink()]


@router.get("/{recording_id}/files")
async def recording_files(request: Request, recording_id: str) -> dict:
    """A recording's files, to download from another machine (#254, 3b)."""
    rec = _own_recording(request, recording_id)
    manager = getattr(request.app.state, "recordings", None)
    files = []
    for p in _files_of(rec):
        try:
            files.append({"name": p.name, "bytes": p.stat().st_size})
        except OSError:
            continue        # gone since it was listed: a live recording moves (CodeRabbit)
    return {"recording_id": rec.id, "live": bool(manager and manager.is_live(rec.dir)),
            "files": files}


@router.get("/{recording_id}/files/{name}")
async def recording_file(request: Request, recording_id: str, name: str):
    """One of a recording's files, streamed."""
    rec = _own_recording(request, recording_id)
    match = next((p for p in _files_of(rec) if p.name == name), None)
    if match is None:
        raise HTTPException(status_code=404,
                            detail=f"{name!r} is not one of recording {rec.id}'s files")
    from fastapi.responses import StreamingResponse

    media = "video/mp4" if name.endswith(".mp4") else (
        "application/json" if name.endswith(".json") else "text/plain")
    from starlette.background import BackgroundTask

    try:
        size, chunks, opened = _stat_sized(match)
    except OSError:
        raise HTTPException(status_code=404,
                            detail=f"{name!r} is not one of recording {rec.id}'s files") from None
    return StreamingResponse(chunks, media_type=media, background=BackgroundTask(opened.close),
                             headers={
        "Content-Length": str(size), "Content-Disposition": f'attachment; filename="{name}"'})


@router.get("")
async def list_recordings(
    request: Request,
    requested_by: Annotated[str | None, Query(description=(
        "Only the recordings started with this `requested_by`"))] = None,
) -> dict:
    """The recordings this server is making or has made since it started."""
    # Read as start stores it: " ci" was kept as "ci", and listing by what was
    # passed found nothing; blank is no filter, as the CLI and MCP send it.
    requested_by = (requested_by or "").strip() or None
    # Blind (#254): a client's own principal's recordings only.
    caller = visibility.current_caller()
    blind = visibility.blind_for(caller)
    return {"recordings": [_with_rejections(request, r.summary())
                           for r in _manager(request).list()
                           if (requested_by is None or r.requested_by == requested_by)
                           and (not blind or r.principal == caller.principal)]}
