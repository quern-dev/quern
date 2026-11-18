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
#: How long a phone's connection is kept after its last call (#718).
IDLE_S = 30.0
#: How long one page's teardown may take before the connection is given up.
TEARDOWN_S = 2.0


class PhoneConnections:
    """One Web Inspector connection per phone, kept between calls (#718).

    The phone's webinspectord admits a new connection only about 10 s after
    the last one began, so a call that opened its own paid 7-9 s whenever it
    followed another inside that window -- `get_web_content`, then the clear
    a tap makes. Measured on an iPhone 11 (iOS 27.2.1, pmd3 11.55): a second
    connection took 7.15 s where the first took 0.13 s, and three calls on one
    kept connection opened their page's session in 0.02-0.03 s each.

    What makes keeping it safe is the teardown at the end of every call.
    WebKit serves one session per page, and a page whose socket was not torn
    down never answers a later session on the same connection: measured, the
    second and third calls each waited 15 s for nothing. So a call releases
    every page it opened before its connection is kept, and a connection is
    kept only from a call that ended cleanly; anything else closes it, which
    leaves the next call paying the gate, as before.

    Calls on one phone take turns: two at once on one connection could both
    open a session on the same page. The connection closes after `IDLE_S`
    unused, when it drops, and when the server stops (`close_all`).
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        #: Which keep of a phone's connection its idle timer belongs to. A
        #: timer can fire while a call has the connection out and puts it
        #: back; the service is the same object then, so only this tells the
        #: stale expiry from the current keep (review on #718).
        self._keeps: dict[str, object] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: Expiries in flight, held so they are not collected mid-close.
        self._expiring: set[asyncio.Task] = set()

    def lock(self, udid: str) -> asyncio.Lock:
        return self._locks.setdefault(udid, asyncio.Lock())

    async def take(self, udid: str) -> Any:
        """The phone's kept connection, now the caller's; None when there is
        none, or the one there was is no use."""
        timer = self._timers.pop(udid, None)
        if timer is not None:
            timer.cancel()
        self._keeps.pop(udid, None)
        service = self._services.pop(udid, None)
        if service is not None and not healthy(service):
            await close_quietly(service)
            return None
        return service

    def put(self, udid: str, service: Any) -> None:
        """Keep `service` for the phone's next call, for `IDLE_S`."""
        old = self._timers.pop(udid, None)
        if old is not None:
            old.cancel()
        keep = object()
        self._services[udid] = service
        self._keeps[udid] = keep
        loop = asyncio.get_running_loop()

        def fire() -> None:
            task = loop.create_task(self._expire(udid, keep))
            self._expiring.add(task)
            task.add_done_callback(self._expiring.discard)
        self._timers[udid] = loop.call_later(IDLE_S, fire)

    async def _expire(self, udid: str, keep: object) -> None:
        async with self.lock(udid):
            if self._keeps.get(udid) is keep:
                service = await self.take(udid)
                if service is not None:
                    await close_quietly(service)

    async def close_all(self) -> None:
        """Close every kept connection: the server is stopping."""
        for udid in list(self._services):
            async with self.lock(udid):
                service = await self.take(udid)
                if service is not None:
                    await close_quietly(service)

    def forget(self) -> None:
        """Drop everything without closing it -- for tests, whose event loop
        each kept connection belonged to has gone."""
        for timer in self._timers.values():
            timer.cancel()
        self._services.clear()
        self._keeps.clear()
        self._timers.clear()
        self._locks.clear()
        self._expiring.clear()


POOL = PhoneConnections()


def healthy(service: Any) -> bool:
    """Whether a kept connection can serve another call. pymobiledevice3 marks
    a lost connection (`is_connected`), but a receive task that died of
    anything else leaves it True while nothing is read any more: the next
    listing would quietly be the last call's, and no session would answer."""
    task = getattr(service, "_recv_task", None)
    return service.is_connected and not (task is not None and task.done())


async def close_quietly(service: Any) -> None:
    """Close `service`, whatever it says. Shielded, so a call given up on
    while closing still finishes the close rather than leaving it open."""
    async def close() -> None:
        try:
            await service.close()
        except Exception:  # noqa: BLE001 -- closing what failed or dropped
            pass
    await asyncio.shield(close())


class PhoneWebInspector:
    """`InspectorLike` over a phone's Web Inspector service, for one call.

    `async with PhoneWebInspector(udid) as inspector:` takes the phone's kept
    connection or opens one (`PhoneConnections`), and the listing taken then
    answers `connected_applications` and `pages`. Leaving releases every page
    the call opened, and keeps the connection only when the call ended cleanly.
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
        self._lock: asyncio.Lock | None = None
        #: Whether this call has the phone's kept connection, or one of its own
        #: because another call has that (see `__aenter__`).
        self._pooled = False
        #: A session whose setup did not finish: the phone may hold its page
        #: with nothing here to release it, so the connection is not kept.
        self._unreleasable = False

    async def __aenter__(self) -> PhoneWebInspector:
        from pymobiledevice3.exceptions import WebInspectorNotEnabledError
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.webinspector import WebinspectorService

        # A phone already in use gets a connection of its own, as every call
        # did before #718, rather than a wait: a call queued behind a long
        # `evaluate_web_js` would spend its own budget waiting and then be
        # reported as the phone not answering (review on #718). Checked and
        # taken in one step -- an unheld lock is acquired without yielding.
        lock = POOL.lock(self.udid)
        if not lock.locked():
            await lock.acquire()
            self._lock, self._pooled = lock, True
        try:
            kept = await POOL.take(self.udid) if self._pooled else None
            if kept is not None:
                try:
                    # Every call lists afresh: pages come and go between calls.
                    # Emptied first, so a listing that does not arrive in time
                    # reads as none, as on a new connection, not as the last
                    # call's pages.
                    kept.application_pages.clear()
                    self._service = kept
                    self._pages = await kept.get_open_application_pages(timeout=LISTING_S)
                except Exception:  # noqa: BLE001 -- a new connection is tried
                    self._service = None
                    await close_quietly(kept)
                    kept = None
            if kept is None:
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

    async def __aexit__(self, exc_type: object, *exc: object) -> None:
        await self._close(keep=exc_type is None)

    async def _close(self, keep: bool = False) -> None:
        """End the call: release its pages, then keep the connection for the
        phone's next call if `keep` and every release went through, and close
        it otherwise. The phone's turn is handed on whatever happens."""
        service, self._service = self._service, None
        try:
            try:
                released = await self._release_pages(service)
                if (service is not None and keep and self._pooled and released
                        and not self._unreleasable and healthy(service)):
                    POOL.put(self.udid, service)
                    service = None
            finally:
                # Also when cancelled mid-release: a connection whose pages may
                # still be held must not be kept, and must not be left open.
                if service is not None:
                    await close_quietly(service)
        finally:
            # Its own `finally`: a cancel arriving during the close above must
            # not keep the phone's turn, or every later call waits on it.
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    async def _release_pages(self, service: Any) -> bool:
        """Release every page this call opened a session on. True when each
        release was sent; a page left held would never answer the next
        call's session on this connection."""
        sessions, self._sessions = self._sessions, {}
        # Every receive loop first, before anything that can be cancelled: a
        # session starts one that never ends by itself, polling every 1 ms,
        # and pymobiledevice3 (11.55) has no call that ends it (CodeRabbit on
        # #715). Cancelled one page at a time, a cancel during the first
        # page's release left the others' running (review on #718).
        for session in sessions.values():
            task = getattr(session, "_receive_task", None)
            if task is not None:
                task.cancel()
        if service is None:
            return True
        released = True
        for (application_id, page_id), session in sessions.items():
            try:
                await asyncio.wait_for(service.teardown_inspector_socket(
                    session.protocol.id_, application_id, page_id), TEARDOWN_S)
            except Exception:  # noqa: BLE001 -- the connection is closed instead
                released = False
        return released

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
            try:
                session = await self._service.inspector_session(entry.application, entry.page)
            except BaseException:
                self._unreleasable = True
                raise
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
