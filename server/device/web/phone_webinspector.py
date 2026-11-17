"""The Web Inspector of a physical iPhone, as `collect_web_content` asks it (#708).

A simulator's Web Inspector is a socket on the Mac (`webinspector.py`). A
phone's is a lockdown service, `com.apple.webinspector`, which pymobiledevice3
speaks over USB with no Xcode. It is the service Safari's Develop menu uses,
and it answers only while the phone's Settings > Apps > Safari > Advanced >
Web Inspector switch is on.

The page contents are collected with the simulator's own script
(`_COLLECT_JS`), so a page reads the same on both. Measured on an iPhone 11
(iOS 27.2.1) against the probe app's web view: the page was listed in 1.2 s
and its five elements collected in 0.1 s.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.device.web.webinspector import _CLEAR_FOCUSED_JS, _COLLECT_JS
from server.models import DeviceError

TOOL = "web-content"
#: How long the phone has to list its applications' pages after it is asked.
LISTING_S = 1.0
#: The whole of one call: connect, list, and every page's collection.
CALL_TIMEOUT_S = 30.0

SWITCH = "Settings > Apps > Safari > Advanced > Web Inspector"


class PhoneWebInspector:
    """`InspectorLike` over a phone's Web Inspector service, for one call.

    `async with PhoneWebInspector(udid) as inspector:` connects, and the
    listing taken then answers `connected_applications` and `pages`.
    """

    def __init__(self, udid: str) -> None:
        self.udid = udid
        self._service: Any = None
        self._pages: list[Any] = []

    async def __aenter__(self) -> PhoneWebInspector:
        from pymobiledevice3.exceptions import WebInspectorNotEnabledError
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.webinspector import WebinspectorService

        try:
            lockdown = await create_using_usbmux(serial=self.udid, autopair=False)
            self._service = WebinspectorService(lockdown=lockdown)
            await self._service.connect()
            self._pages = await self._service.get_open_application_pages(timeout=LISTING_S)
        except WebInspectorNotEnabledError as e:
            await self._close()
            raise DeviceError(
                f"The phone's Web Inspector is off. Turn it on in {SWITCH}, "
                "then ask again.", tool=TOOL) from e
        except Exception as e:
            await self._close()
            raise DeviceError(f"could not reach the phone's Web Inspector: {e}",
                              tool=TOOL) from e
        except asyncio.CancelledError:
            # `bounded` giving up mid-connect: `__aexit__` does not run after a
            # failed entry, so the service would stay open (CodeRabbit on #715).
            await self._close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._service is not None:
            try:
                await self._service.close()
            except Exception:  # noqa: BLE001 -- closing what failed to open
                pass
            self._service = None

    async def connected_applications(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for entry in self._pages:
            app = entry.application
            seen.setdefault(app.id_, {"application_id": app.id_, "bundle_id": app.bundle,
                                      "name": app.name, "pid": app.pid})
        return list(seen.values())

    async def pages(self, application_id: str) -> list[dict]:
        # Web pages only: an app's JSContexts and automation targets are
        # listed too, and have no document to collect.
        return [{"page_id": e.page.id_, "title": e.page.web_title, "url": e.page.web_url}
                for e in self._pages
                if e.application.id_ == application_id
                and e.page.type_.value in ("WIRTypeWeb", "WIRTypeWebPage")]

    async def evaluate(self, application_id: str, page_id: int, script: str) -> Any:
        """`script`'s value in the page, or None when the page is gone."""
        entry = next((e for e in self._pages
                      if e.application.id_ == application_id and e.page.id_ == page_id), None)
        if entry is None or self._service is None:
            return None
        session = await self._service.inspector_session(entry.application, entry.page)
        try:
            await session.runtime_enable()
            return await session.runtime_evaluate(script, return_by_value=True)
        finally:
            # A session starts a receive loop that never ends by itself, and
            # pymobiledevice3 (11.55) has no call that ends it (CodeRabbit on #715).
            task = getattr(session, "_receive_task", None)
            if task is not None:
                task.cancel()

    async def page_contents(self, application_id: str, page_id: int) -> dict | None:
        """The page's elements in page space, by `_COLLECT_JS`."""
        raw = await self.evaluate(application_id, page_id, _COLLECT_JS)
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None

    async def clear_focused_input(self, application_id: str, page_id: int) -> bool:
        """Empty the focused input on a page, by `_CLEAR_FOCUSED_JS`: True only
        when it emptied one, as the simulator's says."""
        return await self.evaluate(application_id, page_id, _CLEAR_FOCUSED_JS) == "cleared"

    async def page_urls(self) -> list[dict]:
        """Every web page's URL, with the bundle hosting it, as
        `web_page_urls` gives a simulator's."""
        from server.device.web.web_content import _is_app

        urls = []
        for app in await self.connected_applications():
            if not _is_app(app):
                continue
            for page in await self.pages(app["application_id"]):
                if page.get("url"):
                    urls.append({"url": str(page["url"]), "process": app["bundle_id"]})
        return urls


async def bounded(coro: Any) -> Any:
    """`coro` within `CALL_TIMEOUT_S`, a timeout said as a DeviceError."""
    try:
        return await asyncio.wait_for(coro, CALL_TIMEOUT_S)
    except TimeoutError as e:
        raise DeviceError(f"the phone's Web Inspector did not answer within "
                          f"{CALL_TIMEOUT_S:.0f} s", tool=TOOL) from e
