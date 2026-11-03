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
import json
import logging
import os
import signal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from server import sandboxes, visibility
from server.api.actions import logged_action
from server.principal import label_of

logger = logging.getLogger(__name__)

router = APIRouter(tags=["git sandbox"])

_SERVICES = {"git-upload-pack", "git-receive-pack"}

#: Seconds between sweeps for revoked keys' and idle repositories.
SWEEP_SECONDS = 60

#: Seconds a push may go without sending a byte before it is dropped: a
#: client that sends half a body and waits must not hold a process forever.
BODY_IDLE_SECONDS = 60

#: One push at a time per key: each is checked against the room left, and two
#: at once would each be given all of it (review).
_push_locks: dict[str, asyncio.Lock] = {}


def _repo(name: str) -> str:
    if not name.endswith(".git"):
        raise HTTPException(404, "a sandbox repository's URL ends in .git")
    try:
        return sandboxes.check_repo(name[:-4])
    except sandboxes.SandboxError as e:
        raise HTTPException(e.status, str(e)) from None


def _key_stamp(request: Request, principal: str) -> str:
    """Which key the caller is: "owner", or a client key's issue time."""
    label = label_of(principal)
    if label is None:
        return "owner"
    clients = getattr(request.app.state, "clients", None)
    record = next((r for r in clients.records() if r.label == label), None) if clients else None
    return record.created_at if record is not None else label


def _caller(request: Request) -> str:
    """The caller's principal, once it may use sandboxes at all, with its
    sandbox bound to its key."""
    principal = visibility.current_caller().principal
    if label_of(principal) is not None:
        from server.config import client_builds
        if not client_builds():
            raise HTTPException(403, (
                "git sandboxes are off for client keys on this server: what is pushed "
                "is there to be built. Its owner can allow it with "
                '"remote": {"client_builds": true} in ~/.quern/config.json'))
    try:
        sandboxes.bind(principal, _key_stamp(request, principal))
    except sandboxes.SandboxError as e:
        raise HTTPException(e.status, str(e)) from None
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
    principal = _caller(request)
    if not (service == "git-receive-pack" and request.method == "POST"):
        return await _run_cgi(request, principal, repo, tail, service)
    # Held from before the room is measured until the push's response has
    # been sent -- the checkout runs inside it -- and released on every path.
    lock = _push_locks.setdefault(principal, asyncio.Lock())
    await lock.acquire()
    try:
        response = await _run_cgi(request, principal, repo, tail, service)
    except BaseException:
        lock.release()
        raise
    inner = response.body_iterator

    async def body():
        try:
            async for chunk in inner:
                yield chunk
        finally:
            lock.release()
    response.body_iterator = body()
    return response


async def _run_cgi(request: Request, principal: str, repo: str, tail: str,
                   service: str) -> StreamingResponse:
    gitdir = sandboxes.principal_dir(principal) / f"{repo}.git"
    pushing = service == "git-receive-pack"
    room = None
    if pushing and request.method == "POST" and request.headers.get("content-encoding"):
        # git never compresses a push, and an inflated body would pass every
        # size check made on the bytes received (review).
        raise HTTPException(415, "a push's body is not accepted compressed")
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
    if room is not None:
        # git's own limit on the pack it will take, in receive-pack itself:
        # the bytes counted here are the request's, and this is the pack's.
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "receive.maxInputSize",
                    "GIT_CONFIG_VALUE_0": str(max(room, 1))})

    try:
        # Its own process group: http-backend runs receive-pack or
        # upload-pack beneath it, and killing only the parent left them.
        proc = await asyncio.create_subprocess_exec(
            "git", "http-backend", env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
    except OSError as e:
        raise HTTPException(500, f"git http-backend could not start: {e}") from None

    def kill() -> None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    over_cap = asyncio.Event()
    abandoned = asyncio.Event()

    # stderr is read as it comes: unread, a full pipe blocks the backend
    # mid-push, and the push never ends. The tail is kept for the log.
    err_tail = bytearray()

    async def drain_stderr() -> None:
        while chunk := await proc.stderr.read(65536):
            err_tail.extend(chunk)
            del err_tail[:-4096]

    draining = asyncio.create_task(drain_stderr())

    async def feed() -> None:
        sent = 0
        stream = request.stream().__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), BODY_IDLE_SECONDS)
                except StopAsyncIteration:
                    break
                sent += len(chunk)
                if room is not None and request.method == "POST" and sent > room:
                    # Chunked, so no length was declared: cut it off here. An
                    # unfinished push leaves only git's quarantine, which git
                    # removes.
                    over_cap.set()
                    kill()
                    return
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a client gone, a body gone quiet, a pipe closed
            # Starlette's ClientDisconnect is none of OSError's family, and
            # escaping here left the backend running for ever (review).
            abandoned.set()
            kill()
        finally:
            with contextlib.suppress(OSError, ConnectionError):
                proc.stdin.close()

    feeding = asyncio.create_task(feed())

    # The backend's output, read as it comes into a bounded queue. Read so a
    # full pipe never stalls the backend; bounded so a slow client holds back
    # a fetch's pack instead of it piling up here.
    out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)

    async def read_out() -> None:
        try:
            while chunk := await proc.stdout.read(65536):
                await out.put(chunk)
        finally:
            await out.put(None)

    reading = asyncio.create_task(read_out())

    def stop() -> None:
        kill()
        for task in (feeding, reading, draining):
            task.cancel()

    # No answer until the whole request has reached the backend. It writes its
    # CGI headers before it reads the body, and a 200 sent on them while the
    # client is still uploading makes curl stop sending: the push hung, with
    # receive-pack waiting for a pack that never came (found by a 2 MB push).
    try:
        await feeding
    except BaseException:
        stop()
        raise
    if abandoned.is_set():
        stop()
        raise HTTPException(400, "the request's body stopped arriving")
    if over_cap.is_set():
        stop()
        raise HTTPException(413, f"this push grew past the {room // 2**20} MB the key's git "
                                 "sandboxes have left (sandboxes.max_mb)")

    # The CGI's own headers, up to the blank line.
    raw = b""
    while b"\r\n\r\n" not in raw and b"\n\n" not in raw:
        chunk = await out.get()
        if chunk is None:
            break
        raw += chunk
    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    head, _, first = raw.partition(sep)
    if not head:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(draining, 2)
        err = bytes(err_tail).decode(errors="replace").strip()
        stop()
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
            while (chunk := await out.get()) is not None:
                yield chunk
            rc = await proc.wait()
            if pushing and request.method == "POST":
                if rc != 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(draining, 2)
                    err = bytes(err_tail).decode(errors="replace").strip()
                    logger.warning("sandbox push %s/%s: http-backend exited %s: %s",
                                   principal, repo, rc, err)
                    return
                from server.config import sandbox_settings
                limits = sandbox_settings()
                try:
                    done = await sandboxes.check_out(principal, repo, limits["max_bytes"],
                                                     limits["min_free_bytes"])
                except sandboxes.SandboxError as e:
                    done = [{"error": str(e)}]
                sandboxes.record_checkout(principal, repo, done)
                logger.info("sandbox push %s/%s: %s", principal, repo, done)
        finally:
            stop()

    headers.pop("Content-Length", None)    # streamed: let the server frame it
    media = headers.pop("Content-Type", None)
    return StreamingResponse(body(), status_code=status, headers=headers, media_type=media)


@router.get("/git/{repo}/info/refs")
async def info_refs(request: Request, repo: str, service: str | None = None):
    if service not in _SERVICES:
        raise HTTPException(403, "only smart HTTP: ?service=git-upload-pack or git-receive-pack")
    return await _cgi(request, _repo(repo), "info/refs", service)


@router.post("/git/{repo}/git-receive-pack")
@logged_action("push_to_sandbox", category="build")
async def receive_pack(request: Request, repo: str):
    return await _cgi(request, _repo(repo), "git-receive-pack", "git-receive-pack")


@router.post("/git/{repo}/git-upload-pack")
async def upload_pack(request: Request, repo: str):
    return await _cgi(request, _repo(repo), "git-upload-pack", "git-upload-pack")


@router.get("/api/v1/sandboxes")
async def list_sandboxes(request: Request) -> dict:
    """The caller's own repositories, their branches and what they hold."""
    from server.config import sandbox_settings
    principal = _caller(request)
    limits = sandbox_settings()
    repos = await asyncio.to_thread(sandboxes.listing, principal)
    return {"repos": repos, "max_bytes": limits["max_bytes"],
            "max_idle_days": limits["max_idle_days"],
            **({"note": limits["note"]} if limits["note"] else {})}


@router.delete("/api/v1/sandboxes/{repo}")
@logged_action("delete_sandbox", category="build")
async def delete_sandbox(request: Request, repo: str) -> dict:
    principal = _caller(request)
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
    from server.config import sandbox_settings

    def key_of(who: str):
        """The key `who` is now: its issue time; None when revoked; ... when
        clients.json cannot be read, which is no evidence of anything."""
        clients = getattr(app.state, "clients", None)
        label = label_of(who)
        if clients is None or label is None:
            return ...
        try:
            json.loads(clients.path.read_text())
        except FileNotFoundError:
            return None if label else ...
        except (OSError, ValueError):
            return ...
        record = next((r for r in clients.records() if r.label == label), None)
        if record is None or not record.active:
            return None
        return record.created_at

    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            removed = await asyncio.to_thread(
                sandboxes.prune, key_of, sandbox_settings()["max_idle_days"])
            if removed:
                logger.info("Removed git sandbox(es): %s", ", ".join(removed))
        except Exception:  # noqa: BLE001 -- the sweep must outlive one bad pass
            logger.exception("Sandbox sweep failed")
