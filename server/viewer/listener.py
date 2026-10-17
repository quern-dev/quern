"""Start and stop the viewer listener inside the server (#449)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import resource
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import uvicorn

from server.viewer.app import create_viewer_app
from server.viewer.grants import ViewerGrants
from server.viewer.protocol import GuardedH11Protocol

logger = logging.getLogger(__name__)

_ANY = {"0.0.0.0", "::", ""}
#: How long a looked-up LAN address is trusted: a laptop changes networks.
LAN_REFRESH_SECONDS = 30.0


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
    return "127.0.0.1"


class LanAddress:
    """`lan_address`, looked up again at most every LAN_REFRESH_SECONDS: links
    issued after a Mac moves networks point at its new address, and requests
    to it are accepted. A lookup that finds none is said once per change."""

    def __init__(self, bind_host: str, lookup: Callable[[str], str] = lan_address,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bind = bind_host
        self._lookup = lookup
        self._clock = clock
        self._value = ""
        self._at = -LAN_REFRESH_SECONDS

    def __call__(self) -> str:
        now = self._clock()
        if now - self._at >= LAN_REFRESH_SECONDS:
            value = self._lookup(self._bind)
            if value != self._value and value == "127.0.0.1" and self._bind in _ANY:
                logger.warning("Could not find this Mac's LAN address; viewer links will use "
                               "127.0.0.1, which only this Mac can open")
            self._value, self._at = value, now
        return self._value


#: The soft file-descriptor limit the viewer asks for: macOS starts a process
#: at 256, which a busy server with a LAN listener could reach. 10240 is the
#: most macOS lets setrlimit grant under an unlimited hard limit.
FD_SOFT_LIMIT = 10240


def raise_fd_limit(target: int = FD_SOFT_LIMIT) -> int | None:
    """Raise the soft RLIMIT_NOFILE toward `target`, never lowering it and
    never past the hard limit. The new soft limit, or None if it could not be
    read or set -- which is logged, not raised: the connection cap is the
    real defence, and this is headroom."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            return want
        return soft
    except (OSError, ValueError) as e:
        logger.warning("Could not raise the file-descriptor limit: %s", e)
        return None


def allowed_hosts(bind_host: str, hosts, lan: Callable[[], str]) -> Callable[[], set[str]]:
    """The Host names requests may carry: the LAN address as it is now, the
    configured extras, and a specific bind address. Never a wildcard: no
    browser sends `Host: 0.0.0.0` for a page it means to reach."""
    fixed = {*hosts} | ({bind_host} - _ANY)
    return lambda: fixed | ({lan()} - _ANY)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


@dataclass
class ViewerListener:
    """What the API needs from a running viewer: tokens, and where links point."""

    grants: ViewerGrants
    port: int | None
    error: str | None = None
    lan: Callable[[], str] | None = field(default=None, repr=False)
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _app: Callable | None = field(default=None, repr=False)

    @property
    def base_url(self) -> str | None:
        """Where links point now; None when the listener isn't listening."""
        if self.port is None or self.lan is None:
            return None
        return f"http://{_url_host(self.lan())}:{self.port}"

    def url_for(self, token: str) -> str | None:
        base = self.base_url
        return f"{base}/v/{token}/" if base else None

    async def stop(self) -> None:
        # Streams first, so they end cleanly rather than being cancelled
        # mid-write by uvicorn's shutdown, which logs each as a traceback.
        if self._app is not None:
            self._app.stop_streams()
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
    raise_fd_limit()
    grants = ViewerGrants(recordings, grace_seconds=settings["grace_seconds"])
    host, port = settings["host"], settings["port"]
    lan = LanAddress(host)
    app = create_viewer_app(recordings=recordings, grants=grants,
                            allowed_hosts=allowed_hosts(host, settings.get("hosts", []), lan),
                            jmuxer_path=settings.get("jmuxer_path"))
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, lifespan="off",
        # The guarded protocol closes a connection that sends no request
        # within a few seconds and refuses past a cap: the listener is
        # reachable from the LAN without a token, and idle sockets held open
        # by anyone there would otherwise use up the whole server's file
        # descriptors. It also keeps the access log off -- every URL here
        # carries a token -- without uvicorn's access_log=False, which
        # would silence the main server's access log as well.
        http=GuardedH11Protocol,
        # Open streams are ended by stop() first; this bounds what is left.
        timeout_graceful_shutdown=2,
        # The main server owns logging: no log_config, no log_level, so
        # uvicorn configures no logger at all.
        log_config=None))
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
        return ViewerListener(grants=grants, port=None, error=error)
    viewer = ViewerListener(grants=grants, port=port, lan=lan, _server=server, _task=task,
                            _app=app)
    logger.info("Viewer listener on %s:%d; links point at %s", host, port, viewer.base_url)
    return viewer
