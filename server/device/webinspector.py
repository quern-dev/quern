"""WebInspector — DOM access inside WKWebViews on iOS simulators.

The accessibility tree does not descend into `WKWebView` on simulators, so web
content is invisible to sim-bridge and idb. WebKit exposes a separate channel for
this: the Web Inspector protocol, the same one Safari's Develop menu uses.

On a simulator that channel is a plain unix socket — no usbmux, no lockdown, no
pairing, no developer disk image:

    /private/tmp/com.apple.launchd.*/com.apple.webinspectord_sim.socket

Traffic is length-prefixed plists carrying `__selector` / `__argument` RPC pairs,
which `pymobiledevice3.ServiceConnection` already speaks. We supply the transport
and drive it directly rather than going through `WebinspectorService`, which
requires a lockdown object a simulator does not have.

Requires the target app to opt its webviews in with `isInspectable = true`
(defaults to false since iOS 16.4). Debug/internal builds only — never ship it.

Note this imports `pymobiledevice3` as a library. `server/device/pmd3.py`
separately shells out to a pipx-installed CLI for physical-device work; the two
are different installs and are floored independently.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import socket
import uuid
from typing import Any

logger = logging.getLogger("quern-debug-server.webinspector")

SIM_SOCKET_GLOB = "/private/tmp/com.apple.launchd.*/com.apple.webinspectord_sim.socket"

# Selectors we send. The listening side sends many more; we only handle the ones
# we act on and log the rest at debug level.
_SEL_REPORT_IDENTIFIER = "_rpc_reportIdentifier:"
_SEL_FORWARD_GET_LISTING = "_rpc_forwardGetListing:"
_SEL_FORWARD_SOCKET_SETUP = "_rpc_forwardSocketSetup:"
_SEL_FORWARD_SOCKET_DATA = "_rpc_forwardSocketData:"


class WebInspectorError(RuntimeError):
    """Raised when the inspector channel is unavailable or misbehaves."""


def find_simulator_sockets() -> list[str]:
    """Return candidate webinspectord simulator socket paths.

    Most of these are dead. Socket files are left behind when a simulator shuts
    down and are never cleaned up - on a machine that has booted a few
    simulators it is normal to find a dozen or more paths with only one live
    listener. Existence tells you nothing; only connecting does.

    They are also not labelled with a UDID, so callers needing a specific device
    must disambiguate by connecting and reading the reported application list.

    Newest first, since the live one is usually the most recently created.
    """
    paths = glob.glob(SIM_SOCKET_GLOB)
    return sorted(paths, key=lambda p: _mtime(p), reverse=True)


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


class SimulatorWebInspector:
    """A connection to one simulator's Web Inspector channel.

    Usage:

        async with SimulatorWebInspector() as inspector:
            apps = await inspector.connected_applications()
            pages = await inspector.pages(app_id)
    """

    def __init__(self, socket_path: str | None = None, timeout: float = 10.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout
        self._service: Any = None
        self._sock: socket.socket | None = None
        self._connection_id = str(uuid.uuid4()).upper()
        self._applications: dict[str, dict] = {}

    async def __aenter__(self) -> SimulatorWebInspector:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the socket and complete the identifier handshake."""
        path = self._socket_path
        candidates = [path] if path else find_simulator_sockets()
        sock, chosen, errors = None, None, []
        for candidate in candidates:
            attempt = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            attempt.settimeout(self._timeout)
            try:
                attempt.connect(candidate)
            except OSError as exc:
                attempt.close()
                errors.append(f"{candidate}: {exc}")
                continue
            sock, chosen = attempt, candidate
            break

        if sock is None:
            raise WebInspectorError(
                f"no live webinspectord socket among {len(candidates)} candidate(s). "
                "Socket files persist after a simulator shuts down, so most are stale. "
                f"Tried: {errors[:3]}"
            )

        logger.debug("webinspector: connected via %s", chosen)
        self._socket_path = chosen
        self._sock = sock

        # Imported only once a live socket is in hand. A missing dependency is a
        # less useful thing to report than "no simulator is booted", and the
        # socket probing above needs nothing but the standard library.
        try:
            from pymobiledevice3.service_connection import ServiceConnection
        except ImportError as exc:  # pragma: no cover - dependency is declared
            sock.close()
            self._sock = None
            raise WebInspectorError(
                "pymobiledevice3>=8.0 is required for webview inspection"
            ) from exc

        self._service = ServiceConnection(sock)
        await self._send(
            _SEL_REPORT_IDENTIFIER,
            {"WIRConnectionIdentifierKey": self._connection_id},
        )
        await self._drain_handshake()

    async def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._service = None

    async def _send(self, selector: str, argument: dict) -> None:
        await self._service.send_plist({"__selector": selector, "__argument": argument})

    async def _recv(self) -> dict | None:
        try:
            return await asyncio.wait_for(self._service.recv_plist(), timeout=self._timeout)
        except TimeoutError:
            return None

    async def _drain_handshake(self, settle: float = 1.5) -> None:
        """Read the burst of reports the daemon sends after identification.

        Applications do not all arrive at once: the first
        `_rpc_reportConnectedApplicationList:` is often partial, with further
        apps following as `_rpc_applicationConnected:`. Stopping at the first
        list therefore misses hosts - including, in practice, the one actually
        showing web content. Drain until the channel goes quiet instead.
        """
        deadline = asyncio.get_running_loop().time() + settle
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                message = await asyncio.wait_for(self._service.recv_plist(), timeout=remaining)
            except TimeoutError:
                break
            if message is None:
                break
            self._absorb(message)

    def _absorb(self, message: dict) -> None:
        selector = message.get("__selector")
        argument = message.get("__argument") or {}
        if selector == "_rpc_reportConnectedApplicationList:":
            self._applications.update(argument.get("WIRApplicationDictionaryKey") or {})
        elif selector in ("_rpc_applicationConnected:", "_rpc_applicationUpdated:"):
            app_id = argument.get("WIRApplicationIdentifierKey")
            if app_id:
                self._applications[app_id] = argument
        elif selector == "_rpc_applicationDisconnected:":
            self._applications.pop(argument.get("WIRApplicationIdentifierKey"), None)
        else:
            logger.debug("webinspector: unhandled selector %s", selector)

    async def connected_applications(self) -> list[dict]:
        """Applications the inspector can see, newest state first."""
        return [
            {
                "application_id": app_id,
                "bundle_id": info.get("WIRApplicationBundleIdentifierKey"),
                "name": info.get("WIRApplicationNameKey"),
                "proxy": bool(info.get("WIRIsApplicationProxyKey")),
            }
            for app_id, info in self._applications.items()
        ]

    async def pages(self, application_id: str) -> list[dict]:
        """Inspectable pages within one application.

        An application with no inspectable webviews returns an empty list. That
        usually means the app has not set `isInspectable`, not that it has no
        web content.
        """
        await self._send(_SEL_FORWARD_GET_LISTING, {
            "WIRConnectionIdentifierKey": self._connection_id,
            "WIRApplicationIdentifierKey": application_id,
        })
        for _ in range(8):
            message = await self._recv()
            if message is None:
                break
            if message.get("__selector") == "_rpc_applicationSentListing:":
                listing = (message.get("__argument") or {}).get("WIRListingKey") or {}
                return [
                    {
                        "page_id": info.get("WIRPageIdentifierKey"),
                        "title": info.get("WIRTitleKey"),
                        "url": info.get("WIRURLKey"),
                    }
                    for info in listing.values()
                ]
            self._absorb(message)
        return []
