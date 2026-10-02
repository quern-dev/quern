"""API routes for screen landmarks."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from server import config as config_mod
from server.api.actions import logged_action
from server.device.landmarks import (
    FileConventions,
    LandmarkRegistry,
    SkippedFile,
    check_conventions,
    conventions_report,
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

def _inline_landmarks(screen: str, raw: object) -> list[Landmark]:
    """Parse one inline screen's landmarks, or refuse with the reason.

    An invalid entry used to escape as a bare 500, which tells the caller
    nothing about which screen or which field. The MCP schema hid it while it
    required `element` on every landmark; a URL landmark needs it optional, so
    the server has to say what is wrong itself.
    """
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail=f"screen {screen!r}: landmarks must be a list of landmark objects",
        )
    landmarks: list[Landmark] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400,
                detail=f"screen {screen!r}, landmark {index}: must be an object",
            )
        try:
            landmarks.append(Landmark(**entry))
        except ValidationError as e:
            reason = "; ".join(err["msg"] for err in e.errors())
            raise HTTPException(
                status_code=400,
                detail=f"screen {screen!r}, landmark {index}: {reason}",
            ) from e
    return landmarks


router = APIRouter(prefix="/api/v1/landmarks", tags=["landmarks"])
logger = logging.getLogger(__name__)


def _get_registry(request: Request) -> LandmarkRegistry:
    return request.app.state.landmark_registry


def _get_controller(request: Request):
    return request.app.state.device_controller


# ---------------------------------------------------------------------------
# POST /load
# ---------------------------------------------------------------------------


def _knowledge_dir(source: str) -> Path:
    """The directory to load: `source`, or the `.quern/knowledge` inside it
    when `source` is a project root. A path that is not a directory is a
    400: it used to load zero screens and say nothing, which is how a
    moved checkout would look loaded-but-empty at every start."""
    path = Path(source).expanduser()
    if (path / ".quern" / "knowledge").is_dir():
        path = path / ".quern" / "knowledge"
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"{source} is not a directory")
    return path


def _app_from_project(knowledge: Path) -> str | None:
    """The bundle id a project's `.quern/config.json` names, beside its
    `knowledge/` -- the file `init_app_knowledge` writes."""
    if knowledge.name != "knowledge" or knowledge.parent.name != ".quern":
        return None
    try:
        bundle = json.loads((knowledge.parent / "config.json").read_text()).get("bundle_id")
    except (OSError, ValueError, AttributeError):
        return None
    return bundle if isinstance(bundle, str) and bundle else None


@router.post("/load")
@logged_action("load_landmarks", category="knowledge")
async def load_landmarks(request: Request, body: LoadLandmarksRequest):
    """Load screen landmarks from a knowledge base path or inline JSON."""
    registry = _get_registry(request)

    if body.source:
        knowledge = _knowledge_dir(body.source)
        app = body.app or _app_from_project(knowledge)
        if not app:
            raise HTTPException(
                status_code=400,
                detail=f"app is required: no .quern/config.json with a bundle_id "
                       f"beside {knowledge}",
            )
        count, skipped = registry.load_from_path(app, str(knowledge))
        remembered = False
        if body.remember:
            if count == 0:
                # Loaded or not, an empty one is not worth loading at every
                # start: it is far likelier a wrong path than a choice.
                raise HTTPException(
                    status_code=400,
                    detail=f"not remembered: {knowledge} has no screens with landmarks",
                )
            try:
                # Absolute, but with its symlinks kept: the path as given is
                # the one meant, and a link repointed later should be followed.
                config_mod.remember_knowledge_base(app, str(knowledge.absolute()))
            except OSError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"loaded {count} screens, but could not remember them: {e}",
                ) from e
            remembered = True
        return {
            "loaded": app,
            "source": str(knowledge),
            "screens": count,
            "skipped": _serialize_skipped(skipped),
            "remembered": remembered,
            "conventions": conventions_report(registry.conventions(app)),
        }

    if body.remember:
        raise HTTPException(
            status_code=400,
            detail="remember needs a source path: inline landmarks have nothing to load again",
        )
    if not body.app:
        raise HTTPException(status_code=400, detail="app is required for inline landmarks")

    if body.landmarks:
        screens: list[ScreenLandmarks] = []
        conventions: list[FileConventions] = []
        for screen_name, entry in body.landmarks.items():
            if isinstance(entry, dict):
                # Missing is refused rather than read as []: the object form
                # exists to say more about a screen, and one that names no
                # landmarks loaded as an empty screen counted in `screens`.
                if "landmarks" not in entry:
                    raise HTTPException(
                        status_code=400,
                        detail=f"screen {screen_name!r}: the object form needs a landmarks list",
                    )
                raw = entry["landmarks"]
                raw_scrollable = entry.get("scrollable")
                raw_declared = entry.get("landmark_conventions")
            else:
                raw = entry
                raw_scrollable = None
                raw_declared = None
            # Anything but a literal bool reads as unset, matching the file
            # parser: a typo must mean "nobody has said" rather than quietly
            # asserting one of the two answers.
            scrollable = raw_scrollable if isinstance(raw_scrollable, bool) else None
            landmarks = _inline_landmarks(screen_name, raw)
            screens.append(ScreenLandmarks(
                screen=screen_name, landmarks=landmarks, scrollable=scrollable,
            ))
            # No file to name, so the screen stands in for one.
            conventions.append(check_conventions(
                f"inline:{screen_name}", screen_name, raw_declared, landmarks,
                unloaded=None if landmarks else "no_landmarks",
            ))
        count = registry.load(body.app, screens, conventions)
        return {
            "loaded": body.app,
            "source": "inline",
            "screens": count,
            "skipped": [],
            "conventions": conventions_report(conventions),
        }

    return {"error": "Provide either 'source' path or 'landmarks' inline data"}


# ---------------------------------------------------------------------------
# POST /identify
# ---------------------------------------------------------------------------


@router.post("/identify")
@logged_action("identify_screen", category="knowledge")
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
    # What loads at every start, and how it went at this one: a remembered
    # knowledge base that did not load is said here, not only in the log.
    at_start = getattr(request.app.state, "landmark_startup", {}) or {}
    remembered = [
        {"app": app, "path": path, "loaded": app in sets, "screens": sets.get(app),
         "at_start": at_start.get(app)}
        for app, path in sorted(config_mod.get_knowledge_bases().items())
    ]
    return {"sets": sets, "total_screens": total, "remembered": remembered}


# ---------------------------------------------------------------------------
# DELETE /
# ---------------------------------------------------------------------------


@router.delete("/")
@logged_action("unload_landmarks", category="knowledge")
async def unload_landmarks(
    request: Request,
    app: str | None = Query(default=None, description="App to unload (omit = all)"),
    forget: bool = Query(default=False, description="Also stop loading it at every start"),
):
    """Unload landmarks for a specific app or all apps."""
    registry = _get_registry(request)
    unloaded = registry.unload(app)
    if not forget:
        return {"unloaded": unloaded}
    try:
        forgotten = config_mod.forget_knowledge_base(app)
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"unloaded, but could not forget it: {e}",
        ) from e
    return {"unloaded": unloaded, "forgotten": forgotten}


# ---------------------------------------------------------------------------
# POST /validate
# ---------------------------------------------------------------------------


@router.post("/validate")
@logged_action("validate_landmarks", category="knowledge")
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
            result = {
                "collisions": [],
                "no_landmarks": [],
                "total_screens": 0,
                "skipped": skipped_payload,
                "error": "no_screens_found",
            }
            if scan.conventions:
                result["conventions"] = conventions_report(scan.conventions)
            return result
        from server.device.landmarks import detect_collisions
        result = detect_collisions(scan.screens)
        if skipped_payload:
            result["skipped"] = skipped_payload
        result["conventions"] = conventions_report(scan.conventions)
        return result

    return registry.validate(app=app)
