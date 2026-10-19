"""Apps sent to the server to install (#254, phase 3b).

    POST   /api/v1/uploads?name=MyApp.ipa   the app as the raw request body
    GET    /api/v1/uploads                  the caller's own
    DELETE /api/v1/uploads/{upload_id}      one of the caller's own

Then `POST /api/v1/device/app/install {"upload_id": ...}`. The body is the
file itself, streamed to disk, not multipart and never held in memory: an
.ipa or .apk as it is, a .app bundle as a gzipped tar named `*.app.tar.gz`.
See server/uploads.py for what is kept, and for how long.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

from fastapi import APIRouter, HTTPException, Query, Request

from server import visibility
from server.api.actions import logged_action
from server.uploads import UploadError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])

#: Seconds between sweeps for expired uploads and revoked keys'.
SWEEP_SECONDS = 30


def _store(request: Request):
    return request.app.state.uploads


@router.post("")
@logged_action("upload_app", category="device.lifecycle")
async def upload(request: Request, name: str = Query(
        description="The app's file name: MyApp.ipa, MyApp.apk, or MyApp.app.tar.gz")) -> dict:
    """Send an app to install with `install_app {"upload_id": ...}`."""
    store = _store(request)
    declared = request.headers.get("content-length")
    try:
        received = await store.receive(
            visibility.current_caller().principal, name, request.stream(),
            int(declared) if declared and declared.isdigit() else None)
    except UploadError as e:
        raise HTTPException(e.status, str(e)) from None
    import time

    return received.summary(time.time())


@router.get("")
async def list_uploads(request: Request) -> dict:
    import time

    now = time.time()
    store = _store(request)
    mine = store.list(visibility.current_caller().principal)
    return {"uploads": [u.summary(now) for u in mine], "max_bytes": store.max_bytes}


@router.delete("/{upload_id}")
@logged_action("delete_upload", category="device.lifecycle")
async def delete_upload(request: Request, upload_id: str) -> dict:
    store = _store(request)
    try:
        store.get(upload_id, visibility.current_caller().principal)
    except UploadError as e:
        raise HTTPException(e.status, str(e)) from None
    if store.busy(upload_id):
        raise HTTPException(409, f"{upload_id} is being installed; delete it after")
    store.discard(upload_id)
    return {"deleted": upload_id}


async def sweep(app) -> None:
    """Expired uploads, and a revoked key's, deleted every SWEEP_SECONDS."""
    from server import principal

    def still_valid(who: str) -> bool:
        label = principal.label_of(who)
        clients = getattr(app.state, "clients", None)
        return label is None or (clients is not None and clients.is_active(label))

    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            # Chosen on the loop; only the deleting is done in a thread.
            dirs = app.state.uploads.expire(still_valid)
            for d in dirs:
                await asyncio.to_thread(shutil.rmtree, d, True)
            if dirs:
                logger.info("Deleted %d upload(s) not installed: %s", len(dirs),
                            ", ".join(d.name for d in dirs))
        except Exception:  # noqa: BLE001 -- the sweep must outlive one bad pass
            logger.exception("Upload sweep failed")
