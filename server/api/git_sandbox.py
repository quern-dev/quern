"""Git's smart-HTTP protocol into the caller's sandbox (#605, prototype).

    GET  /git/{repo}.git/info/refs?service=git-receive-pack|git-upload-pack
    POST /git/{repo}.git/git-receive-pack
    POST /git/{repo}.git/git-upload-pack

Each is handed to `git http-backend` (a CGI program shipped with git, and so
with Xcode) with the caller's own sandbox as its project root, so a key reaches
only its own repositories. Request and response bodies are streamed: a pack
can be large. After a push the pushed branches are checked out before the
response ends, so a build that follows the push finds them.

A client key may push only when the owner allows client builds
(`remote.client_builds`): a push is data, but its only use is to be built.
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


def _repo(name: str) -> str:
    if not name.endswith(".git"):
        raise HTTPException(404, "a sandbox repository's URL ends in .git")
    try:
        return sandboxes.check_repo(name[:-4])
    except sandboxes.SandboxError as e:
        raise HTTPException(e.status, str(e)) from None


def _allowed(principal: str) -> None:
    if label_of(principal) is None:
        return
    from server.config import client_builds
    if not client_builds():
        raise HTTPException(403, (
            "pushing to a sandbox is off for client keys on this server: what is pushed "
            "is there to be built. Its owner can allow it with "
            '"remote": {"client_builds": true} in ~/.quern/config.json'))


async def _cgi(request: Request, repo: str, tail: str, service: str) -> StreamingResponse:
    principal = visibility.current_caller().principal
    _allowed(principal)
    gitdir = sandboxes.principal_dir(principal) / f"{repo}.git"
    if service == "git-receive-pack":
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

    async def feed() -> None:
        try:
            async for chunk in request.stream():
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
    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    head, _, first = raw.partition(sep)
    status, headers = 200, {}
    for line in head.decode("latin-1").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "status":
            status = int(value.strip().split()[0])
        elif key.strip():
            headers[key.strip()] = value.strip()
    if not head:
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        feeding.cancel()
        raise HTTPException(500, f"git http-backend gave no answer: {err or 'nothing'}")

    async def body():
        try:
            if first:
                yield first
            while chunk := await proc.stdout.read(65536):
                yield chunk
            await feeding
            rc = await proc.wait()
            if service == "git-receive-pack" and request.method == "POST":
                if rc != 0:
                    err = (await proc.stderr.read()).decode(errors="replace").strip()
                    logger.warning("sandbox push %s/%s: http-backend exited %s: %s",
                                   principal, repo, rc, err)
                    return
                try:
                    done = await sandboxes.check_out(principal, repo)
                    logger.info("sandbox push %s/%s: checked out %s", principal, repo,
                                [d["branch"] for d in done])
                except sandboxes.SandboxError as e:
                    logger.warning("sandbox push %s/%s: checkout failed: %s", principal, repo, e)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            feeding.cancel()

    headers.pop("Content-Length", None)    # streamed: let the server frame it
    return StreamingResponse(body(), status_code=status, headers=headers,
                             media_type=headers.pop("Content-Type", None))


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
