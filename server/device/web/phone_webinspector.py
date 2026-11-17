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

from server.device.web.webinspector import (
    _CLEAR_FOCUSED_JS,
    _COLLECT_JS,
    _VISIBILITY_JS,
    VISIBLE_S,
)
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
        self._shown: set[tuple[str, int]] | None = None
        #: One session per page: a second on the same page, on the same
        #: connection, never answers -- its `runtime_enable` did not return in
        #: 60 s on an iPhone 11, while the first answered again at once.
        self._sessions: dict[tuple[str, int], Any] = {}
        #: Pages whose session never got going: asked again, a new session
        #: would not answer either, so they answer None (CodeRabbit on #715).
        self._silent: set[tuple[str, int]] = set()

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
        # A session starts a receive loop that never ends by itself, and
        # pymobiledevice3 (11.55) has no call that ends it (CodeRabbit on #715).
        for session in self._sessions.values():
            task = getattr(session, "_receive_task", None)
            if task is not None:
                task.cancel()
        self._sessions.clear()
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

    def _web_pages(self, application_id: str) -> list[Any]:
        # Web pages only: an app's JSContexts and automation targets are
        # listed too, and have no document to collect.
        return [e for e in self._pages
                if e.application.id_ == application_id
                and e.page.type_.value in ("WIRTypeWeb", "WIRTypeWebPage")]

    async def pages(self, application_id: str) -> list[dict]:
        """The application's web pages that are on screen.

        A page in the background -- a Safari tab behind another -- does not
        answer an evaluation, so asking it held a call until `CALL_TIMEOUT_S`
        and failed it (CodeRabbit on #715, measured on an iPhone 11). What is
        sent to it may still run when the tab comes back -- not measured -- so
        nothing that acts is sent to a page that has not answered. Every
        page is asked at once, in a script that changes nothing, and only those
        that answer "visible" are given.
        """
        shown = await self._on_screen()
        return [{"page_id": e.page.id_, "title": e.page.web_title, "url": e.page.web_url}
                for e in self._web_pages(application_id)
                if (application_id, e.page.id_) in shown]

    async def _on_screen(self) -> set[tuple[str, int]]:
        if self._shown is None:
            entries = [e for e in self._pages
                       if e.page.type_.value in ("WIRTypeWeb", "WIRTypeWebPage")]

            async def visible(entry: Any) -> bool:
                try:
                    state = await asyncio.wait_for(
                        self.evaluate(entry.application.id_, entry.page.id_, _VISIBILITY_JS),
                        VISIBLE_S)
                except Exception:  # noqa: BLE001 -- no answer is not on screen
                    return False
                return state == "visible"
            answers = await asyncio.gather(*(visible(e) for e in entries))
            self._shown = {(e.application.id_, e.page.id_)
                           for e, shown in zip(entries, answers, strict=True) if shown}
        return self._shown

    async def evaluate(self, application_id: str, page_id: int, script: str) -> Any:
        """`script`'s value in the page, or None when the page is gone."""
        session = await self._session(application_id, page_id)
        if session is None:
            return None
        return await session.runtime_evaluate(script, return_by_value=True)

    async def send_command(self, application_id: str, page_id: int, method: str,
                           params: dict) -> dict | None:
        """One WebKit protocol message to the page, and its reply message --
        `{"result": ...}` or `{"error": ...}` -- as the simulator's
        `send_command` gives it; None when the page is gone. The caller bounds
        the wait (`bounded`)."""
        session = await self._session(application_id, page_id)
        if session is None:
            return None
        response = await session.send_and_receive({"method": method, "params": params})
        # Behind a target, the reply is wrapped as pymobiledevice3's own
        # `_parse_runtime_evaluate` unwraps it.
        if getattr(session, "target_id", None) is None:
            return response
        return json.loads(response["params"]["message"])

    async def console_history(self, application_id: str, page_id: int) -> list[dict] | None:
        """The page's recent console messages, as WebKit replays them when the
        Console domain is enabled (#714); None for a page not listed. The
        replay arrives before the enable's answer, and pymobiledevice3's
        session hands each event to its handler in arrival order, so by the
        time the enable returns every replayed message is collected. The
        domain is disabled again after, and the session's own handlers -- which
        print each message -- put back."""
        from server.device.web.console import Collector

        session = await self._session(application_id, page_id)
        if session is None:
            return None
        collector = Collector()
        methods = ("Console.messageAdded", "Console.messageRepeatCountUpdated")
        saved = {m: session.response_methods.get(m) for m in methods}
        for m in methods:
            session.response_methods[m] = collector.on_event
        try:
            await session.send_and_receive({"method": "Console.enable", "params": {}})
            try:
                await session.send_and_receive({"method": "Console.disable", "params": {}})
            except Exception:  # noqa: BLE001 -- the history is already read
                pass
        finally:
            for m, handler in saved.items():
                if handler is None:
                    session.response_methods.pop(m, None)
                else:
                    session.response_methods[m] = handler
        return collector.messages

    async def _session(self, application_id: str, page_id: int) -> Any:
        """The page's one session, started on first use; None for a page not
        listed, or one whose session never got going."""
        entry = next((e for e in self._pages
                      if e.application.id_ == application_id and e.page.id_ == page_id), None)
        if entry is None or self._service is None:
            return None
        key = (application_id, page_id)
        if key in self._silent:
            return None
        session = self._sessions.get(key)
        if session is None:
            session = await self._service.inspector_session(entry.application, entry.page)
            self._sessions[key] = session
            try:
                await session.runtime_enable()
            except BaseException:
                # Given up on, as the visibility probe does after VISIBLE_S: a
                # session left unenabled would be reused as if it were not.
                self._silent.add(key)
                raise
        return session

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
            # Every page, in the background too: a URL is read from the
            # listing, and nothing is sent to the page.
            for entry in self._web_pages(app["application_id"]):
                if entry.page.web_url:
                    urls.append({"url": str(entry.page.web_url), "process": app["bundle_id"]})
        return urls


async def bounded(coro: Any) -> Any:
    """`coro` within `CALL_TIMEOUT_S`, a timeout said as a DeviceError."""
    try:
        return await asyncio.wait_for(coro, CALL_TIMEOUT_S)
    except TimeoutError as e:
        raise DeviceError(f"the phone's Web Inspector did not answer within "
                          f"{CALL_TIMEOUT_S:.0f} s", tool=TOOL) from e
