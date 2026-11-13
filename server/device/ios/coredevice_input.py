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

from server.models import DeviceError

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
    primary = next((d for d in displays or [] if isinstance(d, dict) and d.get("primary")), None)
    if primary is None:
        raise NotSent("the device reported no primary display")
    mode = primary.get("currentMode") or {}
    size, scale = mode.get("size"), mode.get("preferredUIScale")
    if not (isinstance(size, list) and len(size) == 2 and all(size)
            and isinstance(scale, (int, float)) and scale > 0):
        raise NotSent(f"the primary display's size could not be read: {mode!r}")
    native, current = primary.get("nativeOrientation"), primary.get("currentOrientation")
    if native != current:
        raise NotSent(f"the screen is turned ({current}, native {native}); "
                      "touch is mapped in the native orientation only")
    return Screen(width=size[0] / scale, height=size[1] / scale, orientation=current)


def _pmd3() -> SimpleNamespace:
    """The pymobiledevice3 pieces input uses, in one place so it can be
    tested without a phone."""
    try:
        from pymobiledevice3.remote.core_device import hid_service
        from pymobiledevice3.remote.core_device.device_info import DeviceInfoService
        from pymobiledevice3.remote.remote_service_discovery import (
            RemoteServiceDiscoveryService,
        )
    except ImportError as e:
        raise NotSent(f"pymobiledevice3's HID services are not available ({e})") from e
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
    with WDA. `delivered_by` records, per device, which of the two carried the
    last action.
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
        self.delivered_by: dict[str, str] = {}
        self._reaper: asyncio.Task | None = None

    # -- the actions -------------------------------------------------------

    async def tap(self, udid: str, x: float, y: float, hold: float | None = None) -> None:
        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            hx, hy = screen.point(x, y)
            progress.reached = True
            await session.hid.send_touchscreen(lib.CONTACT, hx, hy)
            await _sleep(TAP_HOLD_S if hold is None else float(hold))
            await session.hid.send_touchscreen(lib.RELEASE, hx, hy)

        await self._or_fallback(udid, "tap", send,
                                lambda: self._fallback.tap(udid, x, y, hold=hold))

    async def swipe(self, udid: str, start_x: float, start_y: float, end_x: float,
                    end_y: float, duration: float = 0.5, hold: float = 0.0,
                    edge: str | None = None) -> None:
        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            steps = max(2, round(duration * SWIPE_RATE_HZ))
            interval = duration / steps
            progress.reached = True
            for i in range(steps + 1):
                f = i / steps
                await session.hid.send_touchscreen(lib.CONTACT, *screen.point(
                    start_x + (end_x - start_x) * f, start_y + (end_y - start_y) * f))
                if i < steps:
                    await _sleep(interval)
            if hold:
                # Held at the end, so a list stops where the finger does.
                await _sleep(hold)
            await session.hid.send_touchscreen(lib.RELEASE, *screen.point(end_x, end_y))

        extra = {"edge": edge} if edge is not None else {}
        await self._or_fallback(
            udid, "swipe", send,
            lambda: self._fallback.swipe(udid, start_x, start_y, end_x, end_y,
                                         duration, hold=hold, **extra))

    async def press_button(self, udid: str, button: str) -> None:
        code = BUTTONS.get(button.lower())

        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            if code is None:
                raise NotSent(f"no HID code for the {button!r} button")
            if session.indigo is None:
                indigo = lib.IndigoHID(session.rsd)
                await asyncio.wait_for(indigo.connect(), CONNECT_S)
                session.indigo = indigo
            progress.reached = True
            await session.indigo.send_button(*code, lib.BUTTON_DOWN)
            await _sleep(TAP_HOLD_S)
            await session.indigo.send_button(*code, lib.BUTTON_UP)

        await self._or_fallback(udid, f"{button} button", send,
                                lambda: self._fallback.press_button(udid, button))

    # -- delivery ----------------------------------------------------------

    async def _or_fallback(self, udid: str, what: str, send: Any, fallback: Any) -> None:
        """Send over HID; if nothing could be sent, do it through WDA."""
        try:
            await self._send(udid, what, send)
        except NotSent as e:
            logger.info("[coredevice] %s on %s through WDA: %s", what, udid[:8], e)
            self.delivered_by[udid] = getattr(self._fallback, "TOOL_NAME", "wda")
            await fallback()
            return
        self.delivered_by[udid] = self.TOOL_NAME

    async def _send(self, udid: str, what: str, send: Any) -> None:
        lib = _pmd3()
        session = await self._session(udid, lib)
        async with session.lock:
            session.last_used = time.monotonic()
            progress = _Progress()
            try:
                screen = await self._screen(session, lib)
                await send(lib, session, screen, progress)
            except NotSent:
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
        try:
            async with lib.DeviceInfoService(session.rsd) as info:
                raw = await asyncio.wait_for(info.get_display_info(), CONNECT_S)
        except Exception as e:
            raise NotSent(f"the display could not be read: {e!r}") from e
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
            except DeviceError as e:
                raise NotSent(str(e)) from e
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
                    await self._drop(udid, session)

    async def close(self) -> None:
        """Close every held session (server shutdown)."""
        if self._reaper is not None:
            self._reaper.cancel()
        for udid, session in list(self._sessions.items()):
            await self._drop(udid, session)
