"""Screenshot timeline — auto-capture screenshots around UI actions."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger("quern-debug-server.timeline")

_TIMELINE_BASE = Path("/tmp/quern/timeline")


# ---------------------------------------------------------------------------
# Action label formatters — high-fidelity labels from request body JSON
# ---------------------------------------------------------------------------


def _fmt_tap_element(b: dict) -> str:
    parts: list[str] = []
    if b.get("label"):
        parts.append(f"label={b['label']!r}")
    elif b.get("label_contains"):
        parts.append(f"label_contains={b['label_contains']!r}")
    elif b.get("label_prefix"):
        parts.append(f"label_prefix={b['label_prefix']!r}")
    elif b.get("identifier"):
        parts.append(f"id={b['identifier']!r}")
    if b.get("element_type"):
        parts.append(f"type={b['element_type']}")
    return f"tap_element({', '.join(parts)})"


def _fmt_wait_for(b: dict) -> str:
    parts: list[str] = []
    if b.get("label"):
        parts.append(f"label={b['label']!r}")
    elif b.get("label_contains"):
        parts.append(f"label_contains={b['label_contains']!r}")
    elif b.get("identifier"):
        parts.append(f"id={b['identifier']!r}")
    cond = b.get("condition", "exists")
    parts.append(f"condition={cond}")
    return f"wait_for_element({', '.join(parts)})"


ACTION_FORMATTERS: dict[str, callable] = {
    "/api/v1/device/ui/tap-element": _fmt_tap_element,
    "/api/v1/device/ui/tap": lambda b: f"tap({b.get('x')}, {b.get('y')})",
    "/api/v1/device/ui/type": lambda b: f"type_text({b.get('text', '')[:30]!r})",
    "/api/v1/device/ui/clear": lambda b: "clear_text",
    "/api/v1/device/ui/swipe": lambda b: "swipe",
    "/api/v1/device/ui/press": lambda b: f"press_button({b.get('button', '')})",
    "/api/v1/device/ui/wait-for-element": _fmt_wait_for,
    "/api/v1/device/app/launch": lambda b: f"launch_app({b.get('bundle_id', '')})",
    "/api/v1/device/app/terminate": lambda b: f"terminate_app({b.get('bundle_id', '')})",
    "/api/v1/device/open-url": lambda b: f"open_url({b.get('url', '')[:60]})",
}


# ---------------------------------------------------------------------------
# Timeline model
# ---------------------------------------------------------------------------


class TimelineEntry:
    __slots__ = ("timestamp", "action", "screenshot", "status_code")

    def __init__(
        self, timestamp: str, action: str, screenshot: str, status_code: int,
    ) -> None:
        self.timestamp = timestamp
        self.action = action
        self.screenshot = screenshot
        self.status_code = status_code

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "screenshot": self.screenshot,
            "status_code": self.status_code,
        }


class ScreenshotTimeline:
    """Manages an active screenshot timeline session."""

    def __init__(self, udid: str | None = None, session_id: str | None = None) -> None:
        self.session_id = session_id or f"tl_{uuid4().hex[:10]}"
        self.udid = udid
        self.started_at = datetime.now(UTC)
        self.entries: list[TimelineEntry] = []
        self.output_dir = _TIMELINE_BASE / self.session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        logger.info("Screenshot timeline started: %s -> %s", self.session_id, self.output_dir)

    async def capture(
        self, controller, action: str, udid: str, status_code: int = 200,
    ) -> TimelineEntry | None:
        """Capture a screenshot and append to the timeline."""
        try:
            self._counter += 1
            filename = f"{self._counter:03d}.png"
            filepath = self.output_dir / filename
            image_bytes, _ = await controller.screenshot(udid=udid, scale=0.5)
            filepath.write_bytes(image_bytes)
            entry = TimelineEntry(
                timestamp=datetime.now(UTC).isoformat(),
                action=action,
                screenshot=str(filepath),
                status_code=status_code,
            )
            self.entries.append(entry)
            return entry
        except Exception as e:
            logger.warning("Timeline screenshot failed: %s", e)
            return None

    def get_manifest(self) -> dict:
        """Return the timeline as a JSON-serializable manifest."""
        duration = (datetime.now(UTC) - self.started_at).total_seconds()
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(duration, 2),
            "total_screenshots": len(self.entries),
            "output_dir": str(self.output_dir),
            "entries": [e.to_dict() for e in self.entries],
        }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TimelineMiddleware:
    """ASGI middleware that captures screenshots after UI actions.

    When an active timeline exists on app.state, intercepts requests to
    known action endpoints, builds a label from the request body, executes
    the endpoint, then captures a screenshot after a settle delay.

    Short-circuits immediately when no timeline is active.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request

        request = Request(scope, receive)
        timeline = getattr(request.app.state, "active_timeline", None)

        # Fast exit: no active timeline or not an action endpoint
        if timeline is None or request.url.path not in ACTION_FORMATTERS:
            await self.app(scope, receive, send)
            return

        # Buffer the request body so it can be read by both us and the endpoint
        body_bytes = await request.body()
        action = format_action(request.url.path, body_bytes)

        # Make body re-readable for the endpoint
        async def cached_receive():  # noqa: ANN202
            return {"type": "http.request", "body": body_bytes}

        # Capture response status code
        status_code = 200

        async def send_wrapper(message) -> None:  # noqa: ANN001
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        # Execute the actual endpoint
        await self.app(scope, cached_receive, send_wrapper)

        # Capture screenshot after action
        if action:
            controller = getattr(request.app.state, "device_controller", None)
            udid = None
            try:
                parsed = json.loads(body_bytes) if body_bytes else {}
                udid = parsed.get("udid") or timeline.udid
            except (json.JSONDecodeError, TypeError):
                udid = timeline.udid

            if controller and udid:
                await asyncio.sleep(1.0)  # let screen settle
                await timeline.capture(controller, action, udid, status_code)


def format_action(path: str, body: bytes | None) -> str | None:
    """Build a high-fidelity action label from a request path and body."""
    formatter = ACTION_FORMATTERS.get(path)
    if formatter is None:
        return None
    try:
        parsed = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    return formatter(parsed)
