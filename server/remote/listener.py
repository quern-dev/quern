"""The remote listener: the same API over TLS, to client keys (#254, phase 3).

A second uvicorn server inside the process, as the viewer is
(server/viewer/listener.py), serving the main app wrapped in `RemoteGate`:

- TLS, with quern's own certificate (server/remote/cert.py);
- a Host allowlist -- this Mac's LAN address, a specific bind address and the
  configured `hosts` -- answered 421 otherwise, as the viewer does;
- `scope["quern.remote"]`, which tells APIKeyMiddleware to refuse the owner's
  key here, and the pairing routes that they may answer.

Everything else -- client keys, scopes, blind sharing -- is the main app's,
unchanged. The main API stays on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import uvicorn

from server.remote.cert import RemoteCert
from server.remote.pairing import PairingCodes
from server.viewer.listener import LanAddress, allowed_hosts, raise_fd_limit
from server.viewer.protocol import GuardedH11Protocol

logger = logging.getLogger(__name__)

#: Set on every scope the remote listener passes on; auth reads it too.
from server.auth import REMOTE  # noqa: E402

_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


class RemoteH11Protocol(GuardedH11Protocol):
    """The viewer's guarded protocol, which also lets a WebSocket live.

    The guard closes a connection that has not sent a request within a few
    seconds of its last response. An upgraded connection never completes a
    request in its eyes, so the timer left armed at the upgrade closed the
    held-session socket five seconds after it opened."""

    def handle_websocket_upgrade(self, event) -> None:  # type: ignore[override]
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        super().handle_websocket_upgrade(event)


def _host_of(scope) -> str:
    raw = next((v for k, v in scope.get("headers") or () if k == b"host"), b"")
    host = raw.decode("latin-1").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class RemoteGate:
    """Marks a scope as the remote listener's, after checking its Host."""

    def __init__(self, app, allowed: Callable[[], set[str]]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = _host_of(scope)
        if host not in self.allowed() and host not in _LOCAL_NAMES:
            # A name this listener was not given: a DNS-rebinding page, or a
            # mistyped address. As the viewer answers it.
            if scope["type"] == "websocket":
                message = await receive()
                if message.get("type") == "websocket.connect":
                    await send({"type": "websocket.close", "code": 4421})
                return
            body = json.dumps({"detail": f"this listener does not answer to {host!r}"}).encode()
            await send({"type": "http.response.start", "status": 421,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app({**scope, REMOTE: True}, receive, send)


@dataclass
class RemoteListener:
    """What the API needs from a running remote listener."""

    cert: RemoteCert | None
    port: int | None
    codes: PairingCodes = field(default_factory=PairingCodes)
    error: str | None = None
    lan: Callable[[], str] | None = field(default=None, repr=False)
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def url(self) -> str | None:
        """Where clients connect now; None when the listener isn't listening."""
        if self.port is None or self.lan is None:
            return None
        host = self.lan()
        return f"https://{f'[{host}]' if ':' in host else host}:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, 5)


async def start_remote(app, settings: dict) -> RemoteListener | None:
    """The remote listener, when `remote.enabled`; None when it is off.

    A certificate that cannot be made or read, or a port that cannot be
    bound, is reported -- the listener comes back with `error` and no URL --
    and never stops the server. The port is not moved: clients were given it.
    """
    if not settings.get("enabled"):
        return None
    if settings.get("bind_error"):
        logger.warning("%s", settings["bind_error"])
    from server.remote import cert as cert_mod

    host, port = settings["host"], settings["port"]
    lan = LanAddress(host)
    try:
        cert = await asyncio.to_thread(
            cert_mod.load_or_create, cert_mod.names_for(settings.get("hosts", []), [lan()]))
    except Exception as e:  # noqa: BLE001 -- reported; the server goes on without it
        error = f"the remote listener's certificate could not be made or read: {e}"
        logger.error("Remote listener: %s", error)
        return RemoteListener(cert=None, port=None, error=error)
    raise_fd_limit()
    gate = RemoteGate(app, allowed_hosts(host, settings.get("hosts", []), lan))
    server = uvicorn.Server(uvicorn.Config(
        gate, host=host, port=port, lifespan="off",
        ssl_certfile=str(cert.cert_path), ssl_keyfile=str(cert.key_path),
        # The viewer's limits -- a connection cap, and requestless sockets
        # closed -- since this too is reachable from the LAN without a key.
        http=RemoteH11Protocol, timeout_graceful_shutdown=2, log_config=None))
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    async def serve() -> None:
        try:
            await server.serve()
        except SystemExit as e:     # uvicorn's answer to a port it can't bind
            raise RuntimeError(f"the remote listener exited (code {e.code})") from None

    task = asyncio.create_task(serve(), name="remote-listener")
    for _ in range(100):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if not server.started:
        server.should_exit = True
        error = f"could not listen on {host}:{port} (in use, or not permitted)"
        if task.done() and task.exception() is not None:
            error += f": {task.exception()}"
        logger.error("Remote listener: %s", error)
        return RemoteListener(cert=cert, port=None, error=error)
    listener = RemoteListener(cert=cert, port=port, lan=lan, _server=server, _task=task)
    logger.info("Remote listener on %s:%d (%s); certificate %s", host, port, listener.url,
                cert_mod.show(cert.fingerprint))
    return listener
