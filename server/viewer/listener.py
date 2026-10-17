"""Start and stop the viewer listener inside the server (#449)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from dataclasses import dataclass, field

import uvicorn

from server.viewer.app import create_viewer_app
from server.viewer.grants import ViewerGrants

logger = logging.getLogger(__name__)

_ANY = {"0.0.0.0", "::", ""}


def lan_address(bind_host: str) -> str:
    """The address to put in viewer links.

    The bound address when it is a specific one. Otherwise the interface the
    default route leaves through, found by connecting a UDP socket -- which
    sends nothing -- to a TEST-NET address. Falls back to loopback, which
    works only on this machine, and says so in the log.
    """
    if bind_host not in _ANY:
        return bind_host
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        try:
            s.connect(("192.0.2.1", 9))
            addr = s.getsockname()[0]
            if addr and not addr.startswith("0."):
                return addr
        except OSError:
            pass
    logger.warning("Could not find this Mac's LAN address; viewer links will use 127.0.0.1")
    return "127.0.0.1"


@dataclass
class ViewerListener:
    """What the API needs from a running viewer: tokens, and where links point."""

    grants: ViewerGrants
    base_url: str | None
    error: str | None = None
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def url_for(self, token: str) -> str | None:
        return f"{self.base_url}/v/{token}/" if self.base_url else None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, 5)


async def start_viewer(recordings, settings: dict) -> ViewerListener | None:
    """The viewer listener, when `viewer.enabled`; None when it is off.

    A port that can't be bound is not retried elsewhere -- links must not move
    -- and is reported: the listener comes back with `error` set and no base
    URL, and `start_recording(viewer=true)` says why there is no link.
    """
    if not settings.get("enabled"):
        return None
    if settings.get("bind_error"):
        logger.warning("%s", settings["bind_error"])
    grants = ViewerGrants(recordings, grace_seconds=settings["grace_seconds"])
    host, port = settings["host"], settings["port"]
    lan = lan_address(host)
    allowed = {lan, *settings.get("hosts", [])} | ({host} - _ANY)
    app = create_viewer_app(recordings=recordings, grants=grants, allowed_hosts=allowed,
                            jmuxer_path=settings.get("jmuxer_path"))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           lifespan="off", access_log=False))
    # The main server owns SIGINT/SIGTERM. A second Server installing its own
    # handlers would take shutdown away from it.
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    async def serve() -> None:
        # uvicorn answers a port it can't bind with sys.exit(3). A SystemExit
        # escaping a task can end the whole quern server -- the opposite of
        # "the viewer must never stop the server" -- so it becomes an ordinary
        # error here, reported below.
        try:
            await server.serve()
        except SystemExit as e:
            raise RuntimeError(f"the viewer listener exited (code {e.code})") from None

    task = asyncio.create_task(serve(), name="viewer-listener")
    for _ in range(100):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if not server.started:
        server.should_exit = True
        error = f"could not listen on {host}:{port} (in use, or not permitted)"
        if task.done() and task.exception() is not None:
            error += f": {task.exception()}"
        logger.error("Viewer listener: %s", error)
        return ViewerListener(grants=grants, base_url=None, error=error)
    base = f"http://{lan}:{port}"
    logger.info("Viewer listener on %s:%d; links point at %s", host, port, base)
    return ViewerListener(grants=grants, base_url=base, _server=server, _task=task)
