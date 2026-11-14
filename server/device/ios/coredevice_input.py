"""Touch and buttons on a physical iPhone without WebDriverAgent (#664, phase 2a).

On iOS 27 the device takes input over CoreDevice's HID services, reached
over the same RSD tunnel as quern's other in-process pymobiledevice3 paths
(#624): a touchscreen report per contact sample on
`com.apple.coredevice.hid.universalhidservice`, and button events on
`com.apple.coredevice.hid.indigo`. Nothing is installed on the phone, and
iOS 27 needs no media stream for it. Measured on an iPhone 15 Pro on
27.0.1, a tap reached the screen 0.7-1.5 s sooner than through WDA, and it is
not blocked by the "Enable UI Automation" passcode sheet that blocks WDA's
first start.

Two rules shape the module:

- **Never re-sent.** A failure before anything reached the phone -- no
  tunnel, a screen this cannot map, a button or character it has no code
  for -- raises `NotSent`, and the caller does it through WDA instead. A
  failure after the first report raises DeviceError and is never retried: a
  contact already down and then retried through WDA is two taps, the rule
  WDA's own writes follow (#393).
- **Portrait only, for now.** Touch coordinates are normalised to the
  panel's portrait frame, so another orientation needs its rotation, which
  is calibrated only for one landscape of one iPad (#624). A screen in any
  orientation but its native one is `NotSent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from server.device.gestures import check_edge_start
from server.models import DeviceError, InvalidDeviceRequestError

logger = logging.getLogger(__name__)

#: The touchscreen's coordinate range: 0..65535 across the panel.
DIGITIZER_MAX = 65535

#: How long a plain tap holds its contact. #624's taps used 50 ms.
TAP_HOLD_S = 0.05

#: Contact samples a swipe sends per second: one a frame at 60 Hz.
SWIPE_RATE_HZ = 60

#: A held session idle this long is closed.
IDLE_CLOSE_S = 60.0

#: Each step's bound when opening a session or closing one.
CONNECT_S = 5.0

#: Each report's bound, and the display read's. A half-open tunnel otherwise
#: hangs a write for good while it holds the device's session lock, and every
#: later action on that phone queues behind it with no error and no fallback.
SEND_S = 5.0

#: Typing is not here: a HID keyboard is a hardware keyboard to iOS, typed
#: through the phone's hardware-keyboard layout for its input language. On an
#: iPhone 11 with a Japanese keyboard, "Hello abc" arrived as "Hっぉあ BC".
#: type_text stays with WDA, which inserts the text itself.

#: Hardware buttons quern names, as (HID usage page, usage). Consumer page:
#: AXe's capture and #624's probe agree on these.
BUTTONS = {
    "home": (0x0C, 0x40),
    "volumeup": (0x0C, 0xE9),
    "volumedown": (0x0C, 0xEA),
}


#: The pause between reports. Its own name, so a test can stand it in without
#: touching asyncio.sleep, which every other task -- the idle reaper included --
#: still needs to be the real one.
_sleep = asyncio.sleep


class NotSent(Exception):
    """Nothing reached the phone; the caller may do it another way."""


class _ReadFailed(NotSent):
    """The display could not be read: nothing was sent, and the session is
    not trusted again."""


class _Progress:
    """Set by an action just before its first report reaches the phone."""

    reached = False


@dataclass
class Screen:
    """The primary display, in points."""

    width: float
    height: float
    orientation: str  # interface rotation, e.g. "rot0"

    def point(self, x: float, y: float) -> tuple[int, int]:
        """A point on screen as touchscreen coordinates."""
        def scale(v: float, extent: float) -> int:
            return max(0, min(DIGITIZER_MAX, round(v / extent * DIGITIZER_MAX)))
        return scale(x, self.width), scale(y, self.height)


def screen_from_display_info(info: dict) -> Screen:
    """The primary display from `getdisplayinfo`, or NotSent when it cannot
    be mapped."""
    displays = info.get("displays") if isinstance(info, dict) else None
    # Exactly one built-in primary display, as AXe requires: two, or one
    # marked external (a phone mirroring to a screen), is a guess at which
    # panel the touchscreen belongs to, and goes through WDA instead.
    primaries = [d for d in displays or []
                 if isinstance(d, dict) and d.get("primary") is True and not d.get("external")]
    if len(primaries) != 1:
        raise NotSent(f"the device reported {len(primaries)} built-in primary displays, not one")
    primary = primaries[0]
    mode = primary.get("currentMode") or {}
    size, scale = mode.get("size"), mode.get("preferredUIScale")
    if not (isinstance(size, list) and len(size) == 2
            and all(isinstance(v, (int, float)) and v > 0 for v in size)
            and isinstance(scale, (int, float)) and scale > 0):
        raise NotSent(f"the primary display's size could not be read: {mode!r}")
    native, current = primary.get("nativeOrientation"), primary.get("currentOrientation")
    if native is None or current is None:
        # Not read as portrait: a reply without them may be a turned screen.
        raise NotSent(f"the display's orientation could not be read ({current}, native {native})")
    if native != current:
        raise NotSent(f"the screen is turned ({current}, native {native}); "
                      "touch is mapped in the native orientation only")
    return Screen(width=size[0] / scale, height=size[1] / scale, orientation=current)


def _pmd3() -> SimpleNamespace:
    """The pymobiledevice3 pieces input uses, in one place so it can be
    tested without a phone."""
    # AttributeError too: a pymobiledevice3 with the module but without one
    # of these names is as unable to send as one without the module.
    try:
        from pymobiledevice3.remote.core_device import hid_service
        from pymobiledevice3.remote.core_device.device_info import DeviceInfoService
        from pymobiledevice3.remote.remote_service_discovery import (
            RemoteServiceDiscoveryService,
        )
        return SimpleNamespace(
            RemoteServiceDiscoveryService=RemoteServiceDiscoveryService,
            DeviceInfoService=DeviceInfoService,
            UniversalHID=hid_service.UniversalHIDServiceService,
            IndigoHID=hid_service.IndigoHIDService,
            CONTACT=hid_service.TOUCHSCREEN_STATE_CONTACT,
            RELEASE=hid_service.TOUCHSCREEN_STATE_RELEASE,
            BUTTON_DOWN=hid_service.HID_BUTTON_STATE_DOWN,
            BUTTON_UP=hid_service.HID_BUTTON_STATE_UP,
        )
    except (ImportError, AttributeError) as e:
        raise NotSent(f"pymobiledevice3's HID services are not available ({e})") from e


async def _report(send: Any) -> None:
    """One report to the phone, bounded by SEND_S."""
    await asyncio.wait_for(send, SEND_S)


async def _bounded_close(*closers: Any) -> None:
    for close in closers:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(close(), CONNECT_S)


@dataclass
class _Session:
    """One phone's held RSD connection and touch service."""

    rsd: Any
    hid: Any
    indigo: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)

    async def close(self) -> None:
        closers = [self.hid.close, self.rsd.close]
        if self.indigo is not None:
            closers.insert(0, self.indigo.close)
        await _bounded_close(*closers)


class CoreDeviceInput:
    """Input for a physical iPhone over CoreDevice HID, with WDA as the way
    round anything it cannot send.

    Matches the action methods of WdaBackend, so the controller hands it the
    same calls (`DeviceController._input_backend`); everything else stays
    with WDA. Each action returns the TOOL_NAME of whichever of the two
    carried it: per call, since two calls on one device can go different ways.
    """

    TOOL_NAME = "coredevice"
    #: A physical device decides an edge swipe from where it starts, as for
    #: WDA; no flag is involved (#251).
    edge_swipes = True
    edge_flag = False

    def __init__(self, fallback: Any) -> None:
        self._fallback = fallback
        self._sessions: dict[str, _Session] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._dropping: set[asyncio.Task] = set()

    # -- the actions -------------------------------------------------------

    async def tap(self, udid: str, x: float, y: float, hold: float | None = None) -> str:
        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            hx, hy = screen.point(x, y)
            progress.reached = True
            await _report(session.hid.send_touchscreen(lib.CONTACT, hx, hy))
            await _sleep(TAP_HOLD_S if hold is None else float(hold))
            await _report(session.hid.send_touchscreen(lib.RELEASE, hx, hy))

        return await self._or_fallback(udid, "tap", send,
                                       lambda: self._fallback.tap(udid, x, y, hold=hold))

    async def swipe(self, udid: str, start_x: float, start_y: float, end_x: float,
                    end_y: float, duration: float = 0.5, hold: float = 0.0,
                    edge: str | None = None) -> str:
        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            if edge is not None:
                # As WDA does: the device takes a swipe from the edge from
                # where it starts, so one from the middle is refused, not sent.
                check_edge_start(edge, start_x, start_y, screen.width, screen.height,
                                 tool=self.TOOL_NAME)
            steps = max(2, round(duration * SWIPE_RATE_HZ))
            interval = duration / steps
            progress.reached = True
            for i in range(steps + 1):
                f = i / steps
                await _report(session.hid.send_touchscreen(lib.CONTACT, *screen.point(
                    start_x + (end_x - start_x) * f, start_y + (end_y - start_y) * f)))
                if i < steps:
                    await _sleep(interval)
            if hold:
                # Held at the end, so a list stops where the finger does.
                await _sleep(hold)
            await _report(session.hid.send_touchscreen(lib.RELEASE, *screen.point(end_x, end_y)))

        extra = {"edge": edge} if edge is not None else {}
        return await self._or_fallback(
            udid, "swipe", send,
            lambda: self._fallback.swipe(udid, start_x, start_y, end_x, end_y,
                                         duration, hold=hold, **extra))

    async def press_button(self, udid: str, button: str) -> str:
        code = BUTTONS.get(button.lower())

        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            if code is None:
                raise NotSent(f"no HID code for the {button!r} button")
            if session.indigo is None:
                indigo = lib.IndigoHID(session.rsd)
                try:
                    await asyncio.wait_for(indigo.connect(), CONNECT_S)
                except BaseException:
                    await _bounded_close(indigo.close)
                    raise
                session.indigo = indigo
            progress.reached = True
            await _report(session.indigo.send_button(*code, lib.BUTTON_DOWN))
            await _sleep(TAP_HOLD_S)
            await _report(session.indigo.send_button(*code, lib.BUTTON_UP))

        return await self._or_fallback(udid, f"{button} button", send,
                                lambda: self._fallback.press_button(udid, button))

    # -- delivery ----------------------------------------------------------

    async def _or_fallback(self, udid: str, what: str, send: Any, fallback: Any) -> str:
        """Send over HID; if nothing could be sent, do it through WDA. Returns
        which of the two carried it."""
        try:
            await self._send(udid, what, send)
        except NotSent as e:
            logger.info("[coredevice] %s on %s through WDA: %s", what, udid[:8], e)
            await fallback()
            return getattr(self._fallback, "TOOL_NAME", "wda")
        return self.TOOL_NAME

    async def _send(self, udid: str, what: str, send: Any) -> None:
        lib = _pmd3()
        session = await self._session(udid, lib)
        async with session.lock:
            session.last_used = time.monotonic()
            progress = _Progress()
            try:
                screen = await self._screen(session, lib)
                await send(lib, session, screen, progress)
            except _ReadFailed:
                await self._drop(udid, session)
                raise
            except (NotSent, InvalidDeviceRequestError):
                # Nothing sent, and nothing wrong with the session: a screen
                # this cannot map, a button with no code, an edge swipe that
                # does not start at its edge.
                raise
            except asyncio.CancelledError:
                # Cancelled mid-gesture, perhaps with a contact still down. The
                # session goes with it, so the next action does not start on
                # a finger that never lifted.
                await asyncio.shield(self._drop(udid, session))
                raise
            except Exception as e:
                # A session that failed is not trusted again; the next action
                # opens a fresh one.
                await self._drop(udid, session)
                if not progress.reached:
                    raise NotSent(f"the HID service failed before sending: {e!r}") from e
                raise DeviceError(
                    f"{what} on {udid[:8]} may have been partly delivered, so it is "
                    f"not retried: {e!r}", tool=self.TOOL_NAME) from e
            finally:
                session.last_used = time.monotonic()

    async def _screen(self, session: _Session, lib: SimpleNamespace) -> Screen:
        """The display, read per action: a rotation between actions is seen.
        A fresh connection each time, as AXe found a reused one stalls."""
        async def read() -> Any:
            async with lib.DeviceInfoService(session.rsd) as info:
                return await info.get_display_info()

        try:
            # Bounded as a whole: opening and closing the service can stall
            # as well as the read.
            raw = await asyncio.wait_for(read(), SEND_S)
        except Exception as e:
            raise _ReadFailed(f"the display could not be read: {e!r}") from e
        return screen_from_display_info(raw)

    async def _session(self, udid: str, lib: SimpleNamespace) -> _Session:
        existing = self._sessions.get(udid)
        if existing is not None:
            return existing
        opening = self._opening.setdefault(udid, asyncio.Lock())
        async with opening:
            existing = self._sessions.get(udid)
            if existing is not None:
                return existing
            from server.device.performance import _device_address

            try:
                address = await _device_address(udid)
            except Exception as e:
                # Not only DeviceError: tunneld or a resolver can fail any
                # way, and nothing has been sent.
                raise NotSent(f"the device's tunnel address could not be found: {e!r}") from e
            rsd = lib.RemoteServiceDiscoveryService(address)
            hid = None
            try:
                await asyncio.wait_for(rsd.connect(), CONNECT_S)
                hid = lib.UniversalHID(rsd)
                await asyncio.wait_for(hid.connect(), CONNECT_S)
            except Exception as e:
                await _bounded_close(*(c for c in (getattr(hid, "close", None), rsd.close) if c))
                raise NotSent(f"the HID service could not be opened: {e!r}") from e
            session = _Session(rsd=rsd, hid=hid)
            self._sessions[udid] = session
            self._ensure_reaper()
            return session

    async def _drop(self, udid: str, session: _Session) -> None:
        if self._sessions.get(udid) is session:
            del self._sessions[udid]
        await session.close()

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        """Close sessions idle for IDLE_CLOSE_S; end when there are none."""
        while self._sessions:
            await asyncio.sleep(IDLE_CLOSE_S / 4)
            now = time.monotonic()
            for udid, session in list(self._sessions.items()):
                if not session.lock.locked() and now - session.last_used >= IDLE_CLOSE_S:
                    # Its own task, so cancelling the reaper at shutdown does
                    # not cut a close short; close() waits for it instead.
                    drop = asyncio.ensure_future(self._drop(udid, session))
                    self._dropping.add(drop)
                    drop.add_done_callback(self._dropping.discard)
                    await asyncio.shield(drop)

    async def close(self) -> None:
        """Close every held session (server shutdown)."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        await asyncio.gather(*self._dropping, return_exceptions=True)
        for udid, session in list(self._sessions.items()):
            await self._drop(udid, session)
