"""API routes for screen landmarks."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from server import config as config_mod
from server import visibility
from server.api.actions import logged_action
from server.knowledge.landmarks import (
    FileConventions,
    LandmarkRegistry,
    SkippedFile,
    check_conventions,
    conventions_report,
    needs_page_urls,
    resolve_targets,
    scan_knowledge_base,
)
from server.knowledge.navigation import (
    apply_global_edges,
    parse_global_leads_to,
    parse_leads_to,
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
    elif path.name == ".quern" and (path / "knowledge").is_dir():
        path = path / "knowledge"
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"{source} is not a directory")
    return path


async def _load_upload(request: Request, body: LoadLandmarksRequest) -> dict:
    """Load an uploaded knowledge base for the caller's held session (#560).

    The set is the session's alone: for `app`, that session sees only these
    screens; every other caller, and every other app, is as before. It ends
    with the session -- which is why a held session is required: one that is
    never held never ends, and its set would outlive anyone using it."""
    from server.knowledge.session_landmarks import SessionLandmarks
    from server.uploads import UploadError

    if body.source or body.landmarks or body.remember or body.global_leads_to:
        raise HTTPException(status_code=400, detail=(
            "upload_id is used on its own (with app): it is the knowledge base"))
    if not body.app:
        raise HTTPException(status_code=400, detail="app is required with upload_id")
    caller = visibility.current_caller()
    sessions = getattr(request.app.state, "sessions", None)
    held = sessions.get(caller.session) if sessions is not None and caller.session else None
    if held is None or held.principal != caller.principal:
        raise HTTPException(status_code=409, detail=(
            "an uploaded knowledge base is loaded for a held session, and ends with it: "
            "send X-Quern-Session for a session you hold (the quern MCP holds one, as "
            "does `quern session hold NAME`)"))
    landmarks = request.app.state.landmark_registry
    if not isinstance(landmarks, SessionLandmarks):
        raise HTTPException(status_code=503, detail="session knowledge bases are not available")
    store = request.app.state.uploads
    try:
        upload = store.get(body.upload_id, caller.principal)
    except UploadError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None
    if upload.kind != "knowledge":
        raise HTTPException(status_code=400, detail=(
            f"{body.upload_id} is {upload.kind!r}, not a knowledge base: upload one as "
            "<name>.knowledge.tar.gz"))
    # The archive holds the knowledge directory's contents (app.md,
    # screens/); one wrapped in a single top folder is taken as that folder.
    root = upload.path
    if not (root / "screens").is_dir():
        inner = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
        if len(inner) == 1 and (inner[0] / "screens").is_dir():
            root = inner[0]
    if not (root / "screens").is_dir():
        raise HTTPException(status_code=400, detail=(
            "the uploaded knowledge base has no screens/ folder: send the contents of "
            ".quern/knowledge (app.md and screens/*.md)"))
    scan = await asyncio.to_thread(scan_knowledge_base, root)
    if not scan.screens:
        # Refused before anything is replaced: loading nothing over a set the
        # session already has would empty it and still answer "loaded".
        raise HTTPException(status_code=400, detail=(
            "not loaded: the uploaded knowledge base has no screens with landmarks"))
    # Asked again after the scan, which awaited: a session released meanwhile
    # has had its clean-up, and a set loaded now would never be dropped
    # (review).
    now = sessions.get(caller.session)
    if now is None or now.opened_at != held.opened_at:
        raise HTTPException(status_code=409, detail=(
            "not loaded: the session ended while its knowledge base was being read"))
    own = landmarks.own(caller.session, held.opened_at)
    count, skipped = own.load_scan(body.app, scan, f"uploaded ({body.upload_id})")
    store.discard(body.upload_id)
    return {
        "loaded": body.app,
        "source": "upload",
        "scope": "session",
        "session": caller.session,
        "screens": count,
        "skipped": _serialize_skipped(skipped),
        "remembered": False,
        "conventions": conventions_report(own.conventions(body.app)),
        "navigation": own.navigation_report(body.app),
    }


def _app_from_project(knowledge: Path) -> str | None:
    """The bundle id a project's `.quern/config.json` names -- the file
    `init_app_knowledge` writes -- for `.quern/knowledge` or its `screens/`."""
    quern = next((p for p in (knowledge.parent, knowledge.parent.parent)
                  if p.name == ".quern"), None)
    if quern is None:
        return None
    try:
        bundle = json.loads((quern / "config.json").read_text()).get("bundle_id")
    except (OSError, ValueError, AttributeError):
        return None
    return bundle if isinstance(bundle, str) and bundle else None


@router.post("/load")
@logged_action("load_landmarks", category="knowledge")
async def load_landmarks(request: Request, body: LoadLandmarksRequest):
    """Load screen landmarks from a knowledge base path or inline JSON, or --
    for the caller's held session only -- from an uploaded one (#560)."""
    registry = _get_registry(request)

    if body.upload_id:
        return await _load_upload(request, body)

    if body.source:
        # Directory checks, file reads, the scan and the config lock all go
        # to a thread: a slow volume must not stall every other request
        # (CodeRabbit). The registry update stays here, on the loop.
        knowledge = await asyncio.to_thread(_knowledge_dir, body.source)
        app = body.app or await asyncio.to_thread(_app_from_project, knowledge)
        if not app:
            raise HTTPException(
                status_code=400,
                detail=f"app is required: no .quern/config.json with a bundle_id "
                       f"beside {knowledge}",
            )
        scan = await asyncio.to_thread(scan_knowledge_base, knowledge)
        if body.remember and not scan.screens:
            # Refused before anything is replaced: a 400 that had already
            # swapped the loaded set for an empty one read as "nothing
            # happened" while identification went dead (review).
            earlier = (await asyncio.to_thread(config_mod.get_knowledge_bases)).get(app)
            raise HTTPException(
                status_code=400,
                detail=(f"not loaded or remembered: {knowledge} has no screens with landmarks"
                        + (f"; {app} stays remembered at {earlier}" if earlier else "")),
            )
        if not scan.screens and registry.list_sets().get(app):
            # The same trade without remember: loading nothing over a set
            # that identifies screens empties it, and the 200 says "loaded".
            raise HTTPException(
                status_code=400,
                detail=(f"not loaded: {knowledge} has no screens with landmarks, and "
                        f"{app}'s loaded set from {registry.source(app)} was left in place"),
            )
        count, skipped = registry.load_scan(app, scan, str(knowledge))
        remembered = False
        if body.remember:
            try:
                # Absolute, but with its symlinks kept: the path as given is
                # the one meant, and a link repointed later should be followed.
                await asyncio.to_thread(
                    config_mod.remember_knowledge_base, app, str(knowledge.absolute()))
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
            "navigation": registry.navigation_report(app),
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
        edge_issues: list[dict] = []
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
                raw_leads_to = entry.get("leads_to")
            else:
                raw = entry
                raw_scrollable = None
                raw_declared = None
                raw_leads_to = None
            # Anything but a literal bool reads as unset, matching the file
            # parser: a typo must mean "nobody has said" rather than quietly
            # asserting one of the two answers.
            scrollable = raw_scrollable if isinstance(raw_scrollable, bool) else None
            landmarks = _inline_landmarks(screen_name, raw)
            edges, unusable = parse_leads_to(raw_leads_to)
            edge_issues.extend(
                {"file": f"inline:{screen_name}", "screen": screen_name, **u}
                for u in unusable
            )
            screens.append(ScreenLandmarks(
                screen=screen_name, landmarks=landmarks, scrollable=scrollable,
                leads_to=edges,
            ))
            # No file to name, so the screen stands in for one.
            conventions.append(check_conventions(
                f"inline:{screen_name}", screen_name, raw_declared, landmarks,
                unloaded=None if landmarks else "no_landmarks",
            ))
        edge_issues.extend(resolve_targets(screens))
        declared = 0
        if body.global_leads_to is not None:
            globals_, unusable = parse_global_leads_to(body.global_leads_to)
            declared = len(globals_)
            edge_issues.extend({"file": "inline:global_leads_to", **u} for u in unusable)
            edge_issues.extend(
                apply_global_edges(screens, globals_, file="inline:global_leads_to"))
        count = registry.load(body.app, screens, conventions, edge_issues, declared)
        return {
            "loaded": body.app,
            "source": "inline",
            "screens": count,
            "skipped": [],
            "conventions": conventions_report(conventions),
            "navigation": registry.navigation_report(body.app),
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
    # Where each set came from: a path, or "inline". Two checkouts of one app
    # share a server, so "loaded" alone cannot say whose set it is.
    sources = {app: registry.source(app) for app in sets}
    # What loads at every start, and how it went at this one: a remembered
    # knowledge base that did not load is said here, not only in the log.
    at_start = getattr(request.app.state, "landmark_startup", {}) or {}
    if not await asyncio.to_thread(config_mod.knowledge_bases_readable):
        # Never "nothing remembered" on the strength of a file nobody read.
        return {"sets": sets, "sources": sources, "total_screens": total, "remembered": None,
                "remembered_error": f"{config_mod.USER_CONFIG_FILE} cannot be read"}
    remembered = []
    for app, path in sorted((await asyncio.to_thread(config_mod.get_knowledge_bases)).items()):
        loaded_from = registry.source(app)
        remembered.append({
            "app": app, "path": path,
            # This path's set, not just any set for the app: a later manual
            # load from elsewhere is not the remembered one (review).
            "loaded": loaded_from is not None and _same_place(loaded_from, path),
            "loaded_from": loaded_from,
            "screens": sets.get(app),
            "at_start": at_start.get(app),
        })
    return {"sets": sets, "sources": sources, "total_screens": total, "remembered": remembered}


def _same_place(a: str, b: str) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return a == b


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
    """Unload landmarks for a specific app or all apps. The caller's own
    session sets (#560) are unloaded first, when it has any for `app`; a
    client has only those."""
    from server.knowledge.session_landmarks import SessionLandmarks

    registry = _get_registry(request)
    caller = visibility.current_caller()
    mine = (registry.own_apps(caller.session)
            if isinstance(registry, SessionLandmarks) and caller.session else [])
    if mine and (app is None or app in mine):
        if forget:
            raise HTTPException(status_code=400, detail=(
                "forget applies to the server-wide sets; this session's own are never "
                "remembered"))
        if app is None:
            return {"unloaded": registry.drop(caller.session), "scope": "session"}
        registry.own(caller.session).unload(app)
        return {"unloaded": app, "scope": "session"}
    if not caller.is_owner:
        raise HTTPException(status_code=404, detail=(
            f"this session has no knowledge base of its own loaded"
            f"{' for ' + app if app else ''}; the server-wide sets are the owner's"))
    unloaded = registry.unload(app)
    if not forget:
        return {"unloaded": unloaded}
    try:
        forgotten = await asyncio.to_thread(config_mod.forget_knowledge_base, app)
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
        from server.knowledge.landmarks import detect_collisions
        result = detect_collisions(scan.screens)
        if skipped_payload:
            result["skipped"] = skipped_payload
        result["conventions"] = conventions_report(scan.conventions)
        return result

    return registry.validate(app=app)
