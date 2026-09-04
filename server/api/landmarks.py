"""API routes for screen landmarks."""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Query, Request

from server.device.landmarks import (
    LandmarkRegistry,
    SkippedFile,
    needs_page_urls,
    scan_knowledge_base,
)
from server.models import (
    DeviceError,
    IdentifyRequest,
    Landmark,
    LoadLandmarksRequest,
    ScreenLandmarks,
)


def _serialize_skipped(skipped: list[SkippedFile]) -> list[dict]:
    """Convert SkippedFile dataclasses to dicts, dropping null fields."""
    return [
        {k: v for k, v in asdict(s).items() if v is not None}
        for s in skipped
    ]

router = APIRouter(prefix="/api/v1/landmarks", tags=["landmarks"])
logger = logging.getLogger("quern-debug-server.api")


def _get_registry(request: Request) -> LandmarkRegistry:
    return request.app.state.landmark_registry


def _get_controller(request: Request):
    return request.app.state.device_controller


# ---------------------------------------------------------------------------
# POST /load
# ---------------------------------------------------------------------------


@router.post("/load")
async def load_landmarks(request: Request, body: LoadLandmarksRequest):
    """Load screen landmarks from a knowledge base path or inline JSON."""
    registry = _get_registry(request)

    if body.source:
        count, skipped = registry.load_from_path(body.app, body.source)
        return {
            "loaded": body.app,
            "source": body.source,
            "screens": count,
            "skipped": _serialize_skipped(skipped),
        }

    if body.landmarks:
        screens: list[ScreenLandmarks] = []
        for screen_name, lm_list in body.landmarks.items():
            landmarks = [Landmark(**lm) for lm in lm_list]
            screens.append(ScreenLandmarks(screen=screen_name, landmarks=landmarks))
        count = registry.load(body.app, screens)
        return {
            "loaded": body.app,
            "source": "inline",
            "screens": count,
            "skipped": [],
        }

    return {"error": "Provide either 'source' path or 'landmarks' inline data"}


# ---------------------------------------------------------------------------
# POST /identify
# ---------------------------------------------------------------------------


@router.post("/identify")
async def identify_screen(request: Request, body: IdentifyRequest):
    """Identify the current screen against loaded landmarks."""
    registry = _get_registry(request)
    controller = _get_controller(request)

    try:
        elements, resolved = await controller.get_ui_elements(
            body.udid,
            snapshot_depth=body.snapshot_depth,
            source_timeout=body.source_timeout,
            mode=body.mode,
        )
    except DeviceError as e:
        from server.api.device import _handle_device_error
        raise _handle_device_error(e)

    page_urls = (
        await controller.web_page_urls(resolved)
        if needs_page_urls(registry.all_screens(body.app)) else None
    )
    return registry.identify(elements, app=body.app, page_urls=page_urls)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


@router.get("/")
async def list_landmarks(request: Request):
    """List loaded landmark sets."""
    registry = _get_registry(request)
    sets = registry.list_sets()
    total = sum(sets.values())
    return {"sets": sets, "total_screens": total}


# ---------------------------------------------------------------------------
# DELETE /
# ---------------------------------------------------------------------------


@router.delete("/")
async def unload_landmarks(
    request: Request,
    app: str | None = Query(default=None, description="App to unload (omit = all)"),
):
    """Unload landmarks for a specific app or all apps."""
    registry = _get_registry(request)
    unloaded = registry.unload(app)
    return {"unloaded": unloaded}


# ---------------------------------------------------------------------------
# POST /validate
# ---------------------------------------------------------------------------


@router.post("/validate")
async def validate_landmarks(
    request: Request,
    source: str | None = None,
    app: str | None = None,
):
    """Check for landmark collisions.

    If source is provided, validates that path without loading into the registry.
    Otherwise validates currently loaded landmarks.
    """
    registry = _get_registry(request)

    if source:
        from pathlib import Path
        scan = scan_knowledge_base(Path(source))
        skipped_payload = _serialize_skipped(scan.skipped)
        if not scan.screens:
            return {
                "collisions": [],
                "no_landmarks": [],
                "total_screens": 0,
                "skipped": skipped_payload,
                "error": "no_screens_found",
            }
        from server.device.landmarks import detect_collisions
        result = detect_collisions(scan.screens)
        if skipped_payload:
            result["skipped"] = skipped_payload
        return result

    return registry.validate(app=app)
