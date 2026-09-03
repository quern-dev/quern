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
import json
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

# Replies to a forwarded WebKit message come back under this selector, carrying
# the raw protocol JSON. Unlike the sim-bridge wire protocol, WebKit messages
# carry their own ids, so a reply can be matched to its request rather than to
# whatever arrived next.
_SEL_APPLICATION_SENT_DATA = "_rpc_applicationSentData:"

# Collects every element a page can show, with the geometry needed to touch it.
# Written as one expression so a page yields its whole structure in a single
# round trip: a DOM.getDocument walk would need one message per node, and the
# channel is slow enough that the difference is seconds.
_COLLECT_JS = """
(function () {
  var out = [];
  var nodes = document.querySelectorAll(
    'a, button, input, select, textarea, [role], [onclick], h1, h2, h3, h4, ' +
    'h5, h6, p, li, label, span, div');
  for (var i = 0; i < nodes.length && out.length < 400; i++) {
    var n = nodes[i];
    var r = n.getBoundingClientRect();
    // Degenerate boxes are layout artefacts rather than content: skip-links and
    // framework route announcers park themselves at (-1, -1, 1x1), and probing
    // one lands outside the app frame entirely.
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.top > window.innerHeight) continue;
    if (r.right < 0 || r.left > window.innerWidth) continue;
    var own = '';
    for (var c = n.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) own += c.nodeValue;
    }
    own = own.trim();
    var tag = n.tagName.toLowerCase();
    var interactive = (tag === 'a' || tag === 'button' || tag === 'input' ||
                       tag === 'select' || tag === 'textarea' ||
                       n.hasAttribute('onclick') || n.getAttribute('role'));
    // Structural wrappers with no text of their own are noise; keep them only
    // when they are something a user can act on.
    if (!own && !interactive) continue;
    // A block element's border box spans the whole column, but accessibility
    // reports only the rendered glyphs, so the box centre lands in whitespace
    // beside the text and a hit-test there answers with the nearest element
    // instead. A Range over the text nodes reproduces the accessibility frame:
    // measured against joinmastodon.org, an <h1> with a 354x50 border box has a
    // text rect and an AX frame that both read 144x53.
    //
    // This applies to controls too, not just prose. A nav link whose <a> spans
    // a 394pt row reports an accessibility frame only as wide as the word
    // "Apps", so probing the row centre answers with a neighbouring element.
    // The text box is inside the control either way, so it is the safer target.
    var box = r;
    if (own) {
      try {
        var range = document.createRange();
        range.selectNodeContents(n);
        var textRect = range.getBoundingClientRect();
        if (textRect.width >= 2 && textRect.height >= 2) box = textRect;
      } catch (e) {}
    }
    // An element under an overlay is still in the DOM with a valid box, but a
    // tap at its centre hits whatever is on top. Measured with the site's nav
    // menu open: the page heading and body paragraph were still reported, and
    // probing them answered with the menu items covering them.
    var atPoint = document.elementFromPoint(
      box.left + box.width / 2, box.top + box.height / 2);
    if (atPoint && atPoint !== n && !n.contains(atPoint) && !atPoint.contains(n)) continue;
    out.push({
      tag: tag,
      id: n.id || null,
      name: n.getAttribute('name'),
      role: n.getAttribute('role'),
      type: n.getAttribute('type'),
      href: tag === 'a' ? n.getAttribute('href') : null,
      text: (own || n.getAttribute('aria-label') || n.value || '').slice(0, 200),
      x: box.left, y: box.top, width: box.width, height: box.height,
      interactive: !!interactive
    });
  }
  return JSON.stringify({
    url: location.href, title: document.title,
    viewport: {width: window.innerWidth, height: window.innerHeight},
    scroll: {x: window.scrollX, y: window.scrollY},
    elements: out
  });
})()
"""


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
        self._sender_id = str(uuid.uuid4()).upper()
        self._sessions: set[tuple[str, int]] = set()
        self._targets: dict[tuple[str, int], str] = {}
        self._message_id = 0
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

    async def _open_session(self, application_id: str, page_id: int) -> None:
        session = (application_id, page_id)
        if session in self._sessions:
            return
        await self._send(_SEL_FORWARD_SOCKET_SETUP, {
            "WIRApplicationIdentifierKey": application_id,
            "WIRConnectionIdentifierKey": self._connection_id,
            "WIRPageIdentifierKey": page_id,
            "WIRSenderKey": self._sender_id,
        })
        self._sessions.add(session)

    async def _send_to_page(
        self, application_id: str, page_id: int, message: dict,
    ) -> None:
        await self._send(_SEL_FORWARD_SOCKET_DATA, {
            "WIRApplicationIdentifierKey": application_id,
            "WIRConnectionIdentifierKey": self._connection_id,
            "WIRPageIdentifierKey": page_id,
            "WIRSenderKey": self._sender_id,
            "WIRSocketDataKey": json.dumps(message).encode(),
        })

    async def _forward(
        self, application_id: str, page_id: int, method: str, params: dict | None = None,
    ) -> dict | None:
        """Send one WebKit protocol message to a page and wait for its reply.

        Messages are wrapped in `Target.sendMessageToTarget` and replies arrive
        inside `Target.dispatchMessageFromTarget`. Sending a bare
        `Runtime.evaluate` is answered with

            {"error": {"code": -32601, "message": "'Runtime' domain was not
             found"}}

        which reads as "this WebKit has no JavaScript" rather than "you skipped
        a layer". The target id comes from the `Target.targetCreated` event the
        page emits when the session opens, so the session has to be established
        and drained before anything can be sent.

        Replies are matched on the inner message id. WebKit messages carry
        their own ids, unlike the sim-bridge wire protocol, so pairing by
        arrival order is unnecessary here — and would be wrong, since the
        daemon interleaves unrelated events.
        """
        session = (application_id, page_id)
        await self._open_session(application_id, page_id)

        if session not in self._targets:
            target_id = await self._await_target(application_id, page_id)
            if target_id is None:
                # Drop the session too. Setup is skipped when the session is
                # cached, and Target.targetCreated is only announced when it is
                # established — so keeping a session whose target never arrived
                # means every later call waits for an event that cannot come.
                self._sessions.discard(session)
                logger.debug("webinspector: no target for page %s", page_id)
                return None
            self._targets[session] = target_id

        self._message_id += 1
        inner_id = self._message_id
        inner = {"id": inner_id, "method": method, "params": params or {}}

        self._message_id += 1
        await self._send_to_page(application_id, page_id, {
            "id": self._message_id,
            "method": "Target.sendMessageToTarget",
            "params": {"targetId": self._targets[session], "message": json.dumps(inner)},
        })

        deadline = asyncio.get_running_loop().time() + self._timeout
        while asyncio.get_running_loop().time() < deadline:
            decoded = await self._next_page_message(page_id, deadline)
            if decoded is None:
                return None
            if decoded.get("method") == "Target.dispatchMessageFromTarget":
                raw_inner = (decoded.get("params") or {}).get("message")
                try:
                    unwrapped = json.loads(raw_inner) if raw_inner else None
                except ValueError:
                    continue
                if unwrapped and unwrapped.get("id") == inner_id:
                    return unwrapped
        return None

    async def _next_page_message(self, page_id: int, deadline: float) -> dict | None:
        """The next decoded protocol message from one page, ignoring the rest.

        The page id filter is not decoration. An app can have several
        inspectable pages — Metatext has two, a YouTube embed and the instance
        picker — and every one of them announces its own Target.targetCreated
        on the shared connection. Without this, opening a session on one page
        can cache another page's target id and then send every message to the
        wrong document.
        """
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                message = await asyncio.wait_for(
                    self._service.recv_plist(), timeout=remaining)
            except TimeoutError:
                return None
            if message is None:
                return None
            if message.get("__selector") != _SEL_APPLICATION_SENT_DATA:
                self._absorb(message)
                continue
            argument = message.get("__argument") or {}
            if argument.get("WIRPageIdentifierKey") not in (None, page_id):
                continue  # another page's traffic on the shared connection
            raw = argument.get("WIRMessageDataKey")
            if not raw:
                continue
            try:
                return json.loads(bytes(raw).decode())
            except (ValueError, UnicodeDecodeError):
                continue
        return None

    async def _await_target(self, application_id: str, page_id: int) -> str | None:
        """Read the target id the page announces after its session opens."""
        deadline = asyncio.get_running_loop().time() + self._timeout
        while asyncio.get_running_loop().time() < deadline:
            decoded = await self._next_page_message(page_id, deadline)
            if decoded is None:
                return None
            if decoded.get("method") == "Target.targetCreated":
                info = (decoded.get("params") or {}).get("targetInfo") or {}
                target = info.get("targetId")
                if target:
                    return str(target)
        return None

    async def evaluate(self, application_id: str, page_id: int, expression: str) -> object:
        """Run JavaScript in a page and return the decoded result."""
        reply = await self._forward(application_id, page_id, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        if not reply:
            return None
        result = ((reply.get("result") or {}).get("result") or {})
        return result.get("value")

    async def page_contents(self, application_id: str, page_id: int) -> dict | None:
        """Every addressable element in a page, with page-space geometry.

        Coordinates are relative to the page viewport, not the screen. A caller
        wanting to touch them has to anchor the two, which one accessibility
        hit-test is enough to do.
        """
        raw = await self.evaluate(application_id, page_id, _COLLECT_JS)
        if not raw:
            return None
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None

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
        # Bounded by time, not message count. The daemon interleaves unrelated
        # reports, and a busy one can emit more than a handful before the
        # listing arrives - a fixed iteration cap silently returns "no pages"
        # for an app that has them. Same reasoning as _drain_handshake.
        deadline = asyncio.get_running_loop().time() + self._timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                message = await asyncio.wait_for(self._service.recv_plist(), timeout=remaining)
            except TimeoutError:
                break
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
