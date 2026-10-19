"""Screenshots the server saved, fetched by the session that caused them
(#254, phase 3b). See server/screenshots.py.

    GET /api/v1/screenshots?path=/tmp/quern/screenshots/...png
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from server import screenshots, visibility

router = APIRouter(prefix="/api/v1/screenshots", tags=["screenshots"])


@router.get("")
async def screenshot_file(request: Request, path: str = Query(
        description="A screenshot path from an answer: screenshots.before/after, "
                    "screen_context.screenshot, a timeline entry")) -> Response:
    """The screenshot at `path`, for the session that caused it, or the owner."""
    owner = screenshots.owner_of(path)
    caller = visibility.current_caller()
    # Not recorded, or another's, reads the same: no such screenshot.
    if owner is None or not (caller.is_owner or owner == (caller.principal, caller.session)):
        raise HTTPException(404, f"no screenshot {path!r} for this session")
    try:
        data = screenshots.read(path)
    except FileNotFoundError:
        raise HTTPException(404, f"{path} is gone from the server (its /tmp was cleared)") from None
    except OSError:
        raise HTTPException(404, f"no screenshot {path!r} for this session") from None
    return Response(data, media_type="image/png")
