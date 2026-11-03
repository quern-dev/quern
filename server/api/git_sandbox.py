"""Git's smart-HTTP protocol into the caller's sandbox (#608).

    GET    /git/{repo}.git/info/refs?service=git-receive-pack|git-upload-pack
    POST   /git/{repo}.git/git-receive-pack
    POST   /git/{repo}.git/git-upload-pack
    GET    /api/v1/sandboxes                 the caller's repositories
    DELETE /api/v1/sandboxes/{repo}          one of them, and its checkouts

The git routes are handed to `git http-backend` (a CGI program shipped with
git, and so with Xcode) with the caller's own sandbox as its project root, so
a key reaches only its own repositories. Request and response bodies are
streamed: a pack can be large. After a push the pushed branches are checked
out before the response ends, so a build that follows the push finds them.

A client key may use any of this only when the owner allows client builds
(`remote.client_builds`): a push is data, but its only use is to be built.
A push is refused before it starts when the key's sandboxes are at their cap
(413) or the volume is at its free-space floor (507), and cut off when it
grows past the cap while arriving. See server/sandboxes.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from server import sandboxes, visibility
from server.principal import label_of

logger = logging.getLogger(__name__)

router = APIRouter(tags=["git sandbox"])

_SERVICES = {"git-upload-pack", "git-receive-pack"}

#: Seconds between sweeps for revoked keys' and idle repositories.
SWEEP_SECONDS = 600


def _repo(name: str) -> str:
    if not name.endswith(".git"):
        raise HTTPException(404, "a sandbox repository's URL ends in .git")
    try:
        return sandboxes.check_repo(name[:-4])
    except sandboxes.SandboxError as e:
        raise HTTPException(e.status, str(e)) from None


def _caller() -> str:
    """The caller's principal, once it may use sandboxes at all."""
    principal = visibility.current_caller().principal
    if label_of(principal) is not None:
        from server.config import client_builds
        if not client_builds():
            raise HTTPException(403, (
                "git sandboxes are off for client keys on this server: what is pushed "
                "is there to be built. Its owner can allow it with "
                '"remote": {"client_builds": true} in ~/.quern/config.json'))
    return principal


def _room(principal: str) -> int:
    """Bytes the caller may still push, or HTTPException when it may push none."""
    from server.config import sandbox_settings
    limits = sandbox_settings()
    held = sandboxes.size_of(sandboxes.principal_dir(principal))
    if held >= limits["max_bytes"]:
        raise HTTPException(413, (
            f"this key's git sandboxes hold {held // 2**20} MB, at their cap of "
            f"{limits['max_bytes'] // 2**20} MB (sandboxes.max_mb): delete a repository "
            "(DELETE /api/v1/sandboxes/{repo}) and push again"))
    try:
        free = sandboxes.free_bytes()
    except OSError:
        free = None
    if free is not None and free < limits["min_free_bytes"]:
        raise HTTPException(507, (
            f"the server's disk has {free // 2**20} MB free, under the "
            f"{limits['min_free_bytes'] // 2**20} MB it keeps (uploads.min_free_mb)"))
    return limits["max_bytes"] - held


async def _cgi(request: Request, repo: str, tail: str, service: str) -> StreamingResponse:
    principal = _caller()
    gitdir = sandboxes.principal_dir(principal) / f"{repo}.git"
    pushing = service == "git-receive-pack"
    room = None
    if pushing:
        room = await asyncio.to_thread(_room, principal)
        try:
            await sandboxes.ensure_repo(principal, repo)
        except sandboxes.SandboxError as e:
            raise HTTPException(e.status, str(e)) from None
    elif not (gitdir / "HEAD").exists():
        raise HTTPException(404, f"no sandbox repository {repo!r}")

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_PROJECT_ROOT": str(sandboxes.principal_dir(principal)),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": f"/{repo}.git/{tail}",
        "REQUEST_METHOD": request.method,
        "QUERY_STRING": request.url.query,
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "REMOTE_USER": label_of(principal) or "owner",
        "REMOTE_ADDR": request.client.host if request.client else "",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if (length := request.headers.get("content-length")) is not None:
        if room is not None and request.method == "POST" and length.isdigit() \
                and int(length) > room:
            raise HTTPException(413, f"this push is {int(length) // 2**20} MB and the key's "
                                     f"git sandboxes have {room // 2**20} MB left "
                                     "(sandboxes.max_mb)")
        env["CONTENT_LENGTH"] = length
    if (encoding := request.headers.get("content-encoding")):
        env["HTTP_CONTENT_ENCODING"] = encoding
    if (protocol := request.headers.get("git-protocol")):
        env["GIT_PROTOCOL"] = protocol

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "http-backend", env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise HTTPException(500, f"git http-backend could not start: {e}") from None

    over_cap = asyncio.Event()

    async def feed() -> None:
        sent = 0
        try:
            async for chunk in request.stream():
                sent += len(chunk)
                if room is not None and request.method == "POST" and sent > room:
                    # Chunked, so no length was declared: cut it off here. An
                    # unfinished push leaves only git's quarantine, which git
                    # removes.
                    over_cap.set()
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    return
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError, ConnectionError):
                proc.stdin.close()

    feeding = asyncio.create_task(feed())

    # The CGI's own headers, up to the blank line.
    raw = b""
    while b"\r\n\r\n" not in raw and b"\n\n" not in raw:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        raw += chunk
    if over_cap.is_set():
        feeding.cancel()
        raise HTTPException(413, f"this push grew past the {room // 2**20} MB the key's git "
                                 "sandboxes have left (sandboxes.max_mb)")
    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    head, _, first = raw.partition(sep)
    if not head:
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        feeding.cancel()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise HTTPException(500, f"git http-backend gave no answer: {err or 'nothing'}")
    status, headers = 200, {}
    for line in head.decode("latin-1").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "status":
            try:
                status = int(value.strip().split()[0])
            except (ValueError, IndexError):
                status = 500
        elif key.strip():
            headers[key.strip()] = value.strip()

    async def body():
        try:
            if first:
                yield first
            while chunk := await proc.stdout.read(65536):
                yield chunk
            await feeding
            rc = await proc.wait()
            if pushing and request.method == "POST":
                if rc != 0:
                    err = (await proc.stderr.read()).decode(errors="replace").strip()
                    logger.warning("sandbox push %s/%s: http-backend exited %s: %s",
                                   principal, repo, rc, err)
                    return
                try:
                    done = await sandboxes.check_out(principal, repo)
                    logger.info("sandbox push %s/%s: %s", principal, repo, done)
                except sandboxes.SandboxError as e:
                    logger.warning("sandbox push %s/%s: checkout failed: %s", principal, repo, e)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            feeding.cancel()

    headers.pop("Content-Length", None)    # streamed: let the server frame it
    media = headers.pop("Content-Type", None)
    return StreamingResponse(body(), status_code=status, headers=headers, media_type=media)


@router.get("/git/{repo}/info/refs")
async def info_refs(request: Request, repo: str, service: str | None = None):
    if service not in _SERVICES:
        raise HTTPException(403, "only smart HTTP: ?service=git-upload-pack or git-receive-pack")
    return await _cgi(request, _repo(repo), "info/refs", service)


@router.post("/git/{repo}/git-receive-pack")
async def receive_pack(request: Request, repo: str):
    return await _cgi(request, _repo(repo), "git-receive-pack", "git-receive-pack")


@router.post("/git/{repo}/git-upload-pack")
async def upload_pack(request: Request, repo: str):
    return await _cgi(request, _repo(repo), "git-upload-pack", "git-upload-pack")


@router.get("/api/v1/sandboxes")
async def list_sandboxes() -> dict:
    """The caller's own repositories, their branches and what they hold."""
    from server.config import sandbox_settings
    principal = _caller()
    limits = sandbox_settings()
    repos = await asyncio.to_thread(sandboxes.listing, principal)
    return {"repos": repos, "max_bytes": limits["max_bytes"],
            "max_idle_days": limits["max_idle_days"],
            **({"note": limits["note"]} if limits["note"] else {})}


@router.delete("/api/v1/sandboxes/{repo}")
async def delete_sandbox(repo: str) -> dict:
    principal = _caller()
    try:
        existed = await asyncio.to_thread(sandboxes.remove_repo, principal, repo)
    except sandboxes.SandboxError as e:
        raise HTTPException(e.status, str(e)) from None
    if not existed:
        raise HTTPException(404, f"no sandbox repository {repo!r}")
    return {"deleted": repo}


async def sweep(app) -> None:
    """Revoked keys' sandboxes, and repositories idle too long, removed every
    SWEEP_SECONDS."""
    from server import principal as principal_mod
    from server.config import sandbox_settings

    def still_valid(who: str) -> bool:
        label = principal_mod.label_of(who)
        clients = getattr(app.state, "clients", None)
        return label is None or (clients is not None and clients.is_active(label))

    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            removed = await asyncio.to_thread(
                sandboxes.prune, still_valid, sandbox_settings()["max_idle_days"])
            if removed:
                logger.info("Removed git sandbox(es): %s", ", ".join(removed))
        except Exception:  # noqa: BLE001 -- the sweep must outlive one bad pass
            logger.exception("Sandbox sweep failed")
