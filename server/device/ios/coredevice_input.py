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
- **Turned screens are rotated into the panel's frame.** Touch coordinates
  are normalised to the panel's native frame, whatever the interface's
  orientation, so a point on a turned screen is rotated into it (#624).
  Calibrated on an iPhone 11 in both landscapes; upside down is not
  measured, and is `NotSent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from server.device.gestures import TAP_HOLD_MS, Plan, check_edge_start
from server.device.ios.native_tunnel import rsd_for
from server.models import DeviceError, InvalidDeviceRequestError, NoTunnelError

logger = logging.getLogger(__name__)

#: The touchscreen's coordinate range: 0..65535 across the panel.
DIGITIZER_MAX = 65535

#: How long a plain tap holds its contact. #624's taps used 50 ms.
TAP_HOLD_S = 0.05

#: Contact samples a swipe sends per second: one a frame at 60 Hz.
SWIPE_RATE_HZ = 60

#: The most contacts one touchscreen report carries
#: (pymobiledevice3's TOUCHSCREEN_CONTACT_COUNT_MAXIMUM).
MAX_CONTACTS = 5

#: A held session idle this long is closed.
IDLE_CLOSE_S = 60.0

#: Each step's bound when opening a session or closing one.
CONNECT_S = 5.0
#: Seconds for each step with the phone's pasteboard: saving it, setting it,
#: putting it back. Measured on an iPhone 11: a set took 0.07 s.
PASTEBOARD_S = 5.0

#: How long a newly opened HID session waits before its first report. One
#: sent sooner is dropped: measured on an iPhone 11, a tap on Settings' General
#: row straight after opening did nothing, three of three, and after 0.5 s
#: opened General, three of three. Only 0 s and 0.5 s were measured.
#: pymobiledevice3 waits 0.5 s too, but after starting a media stream, which
#: quern does not open: its TOUCH_AUTH_SETTLE_SECONDS is a related wait, not
#: this one. Sessions close after IDLE_CLOSE_S, so without
#: this the first tap or swipe after any minute's pause was lost -- a scroll
#: sweep read the dropped drag as the end of the list and swept back up.
FIRST_REPORT_S = 0.5

#: Each report's bound, and the display read's. A half-open tunnel otherwise
#: hangs a write for good while it holds the device's session lock, and every
#: later action on that phone queues behind it with no error and no fallback.
SEND_S = 5.0

#: Typing is over a virtual HID keyboard, which iOS treats as a hardware
#: keyboard (#664): keys arrive through the phone's layout and input method for
#: the keyboard in use, which nothing reports. On an iPhone 11 with a Japanese
#: keyboard "Hello abc #@" arrived as "Helloあbc＃＠". So this types US key
#: positions, and the controller reads the field back and corrects it when
#: what arrived is not what was meant.
#:
#: How long a key is held, and the pause after its release: the 0.02 s
#: pymobiledevice3's own `type` uses.
KEY_HOLD_S = 0.02

#: Between the tap that focuses a field and the first key for it.
FOCUS_S = 0.4

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


class _NoTunnel(NotSent):
    """Nothing was sent because there is no tunnel to the phone -- which WDA
    would need too, so it is not handed to WDA (see `NoTunnelError`)."""


class _Transient(NotSent):
    """Nothing was sent because the connection failed, not because of the
    phone or the request: worth one more try on a fresh session (#780).
    With no WDA behind HID, a one-off failure here was a refusal."""


class _ReadFailed(_Transient):
    """The display could not be read: nothing was sent, and the session is
    not trusted again."""


class _Progress:
    """Set by an action just before its first report reaches the phone."""

    reached = False


@dataclass
class Screen:
    """The primary display, in points, as the interface is turned now."""

    width: float
    height: float
    orientation: str  # interface rotation, e.g. "rot0"
    #: Degrees the interface is turned from the panel's native orientation:
    #: 0, 90 or 270 (#624).
    rotation: int = 0

    def point(self, x: float, y: float) -> tuple[int, int]:
        """A point on screen as touchscreen coordinates.

        The point is in the interface's own frame -- the accessibility tree's
        and the caller's -- and the touchscreen takes the panel's native one,
        so a turned screen's point is rotated into it. Measured on an iPhone
        11 (iOS 27.2.1), each landscape by which of two taps landed on a
        segmented control: home bar on the right (rot270) is (1 - y, x), on
        the left (rot90) is (y, 1 - x), as an iPad in landscape had shown.
        """
        nx, ny = self._native(x, y)
        return round(nx * DIGITIZER_MAX), round(ny * DIGITIZER_MAX)

    def native_point(self, x: float, y: float) -> tuple[float, float]:
        """A point on screen in the panel's native frame, in points.

        What the phone's accessibility hit-test takes: measured on an iPhone
        11 in landscape (home bar on the right), the hit-test at the turned
        interface's (82, 236) answered with the element at (236, 332), until
        the point was rotated -- while the frames it answers with, like the
        tree's, are the turned interface's own (#624).
        """
        nx, ny = self._native(x, y)
        native_w, native_h = ((self.height, self.width) if self.rotation in (90, 270)
                              else (self.width, self.height))
        return nx * native_w, ny * native_h

    def _native(self, x: float, y: float) -> tuple[float, float]:
        """(x, y) normalised and rotated into the panel's native frame."""
        nx = min(1.0, max(0.0, x / self.width))
        ny = min(1.0, max(0.0, y / self.height))
        if self.rotation == 90:
            return ny, 1 - nx
        if self.rotation == 270:
            return 1 - ny, nx
        return nx, ny


#: The rotations touch has been calibrated for (#624), in degrees from the
#: panel's native orientation. Upside down (180) is not measured: most iPhone
#: apps do not turn that way, and a guessed mapping taps the wrong place.
CALIBRATED_ROTATIONS = (0, 90, 270)


def _degrees(orientation: Any) -> int | None:
    """Degrees from an orientation name such as "rot270", or None."""
    if isinstance(orientation, str) and orientation.startswith("rot"):
        digits = orientation[3:]
        if digits.isdigit() and int(digits) % 90 == 0:
            return int(digits) % 360
    return None


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
    native_deg, current_deg = _degrees(native), _degrees(current)
    if native_deg is None or current_deg is None:
        raise NotSent(f"the display's orientation could not be read ({current}, native {native})")
    rotation = (current_deg - native_deg) % 360
    if rotation not in CALIBRATED_ROTATIONS:
        raise NotSent(f"the screen is turned {rotation} degrees ({current}, native {native}); "
                      "touch is calibrated for portrait and both landscapes only")
    # `size` is the panel's, in its native orientation, whatever the
    # interface's; a quarter turn swaps the interface's width and height.
    width, height = size[0] / scale, size[1] / scale
    if rotation in (90, 270):
        width, height = height, width
    return Screen(width=width, height=height, orientation=current, rotation=rotation)


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
            Contact=hid_service.TouchscreenContact,
            CONTACT=hid_service.TOUCHSCREEN_STATE_CONTACT,
            RELEASE=hid_service.TOUCHSCREEN_STATE_RELEASE,
            BUTTON_DOWN=hid_service.HID_BUTTON_STATE_DOWN,
            BUTTON_UP=hid_service.HID_BUTTON_STATE_UP,
            KEYS=hid_service.ASCII_TO_HID,
            SHIFT=hid_service.KEY_LEFT_SHIFT,
            COMMAND=hid_service.KEY_LEFT_GUI,
            KEY_A=hid_service.KEY_A,
            KEY_V=hid_service.KEY_V,
            BACKSPACE=hid_service.KEY_BACKSPACE,
        )
    except (ImportError, AttributeError) as e:
        raise NotSent(f"pymobiledevice3's HID services are not available ({e})") from e


def types_on_us_keys(text: str) -> bool:
    """Whether every character of `text` has a US key. False when the HID
    services are not there to ask."""
    try:
        keys = _pmd3().KEYS
    except NotSent:
        return False
    return all(ch in keys for ch in text)


async def _press(lib: SimpleNamespace, session: Any, progress: Any,
                 chords: list[tuple[int | None, int]]) -> None:
    """Press each (modifier, key) on a virtual keyboard of its own (#664).

    Registered on a HID service of its own and closed with it afterwards:
    while one is attached iOS hides the on-screen keyboard, so a keyboard left
    on the held session would hide it for the minute that session stays open.
    Measured on an iPhone 11: closing the service brings the on-screen
    keyboard straight back.
    """
    keyboard = lib.UniversalHID(session.rsd)
    try:
        await asyncio.wait_for(keyboard.connect(), CONNECT_S)
        service = await asyncio.wait_for(keyboard.create_keyboard_service(), CONNECT_S)
        progress.reached = True
        for modifier, usage in chords:
            if modifier is not None:
                # Pressed on its own first: iOS can ignore a modifier that
                # arrives with the key, as pymobiledevice3's own `type` found
                # for Shift.
                await _report(keyboard.send_keyboard(service, (modifier,)))
                await _sleep(KEY_HOLD_S)
            held = (modifier, usage) if modifier is not None else (usage,)
            await _report(keyboard.send_keyboard(service, held))
            await _sleep(KEY_HOLD_S)
            await _report(keyboard.send_keyboard(service, ()))
            await _sleep(KEY_HOLD_S)
    finally:
        await asyncio.shield(_bounded_close(keyboard.close))


def _pasteboard_service() -> Any:
    """CoreDevice's pasteboard service class, or NotSent where this
    pymobiledevice3 has none: apart from `_pmd3`, so typing US keys does not
    depend on it."""
    try:
        from pymobiledevice3.remote.core_device.pasteboard_service import PasteboardService
    except ImportError as e:
        raise NotSent(f"pymobiledevice3 has no pasteboard service ({e})") from e
    return PasteboardService


def _pasteboard_items(snapshot: Any) -> list | None:
    """The items of a pasteboard pull, as `set` takes them back, or None when
    the reply is not that shape -- which is not "the clipboard was empty"."""
    if not isinstance(snapshot, dict):
        return None
    board = snapshot.get("pasteboard", snapshot)
    items = board.get("items") if isinstance(board, dict) else None
    return list(items) if isinstance(items, list) else None


async def _paste(lib: SimpleNamespace, session: Any, progress: Any, text: str,
                 restore_failed: Any = None) -> None:
    """Type `text` by pasting it: set on the phone's clipboard, then Cmd+V on a
    keyboard of its own, the clipboard put back afterwards (#780).

    For text with a character no US key types, which went through WDA.
    Measured on an iPhone 11 (iOS 27.2.1): "café 🎉 Ωμέγα" landed exactly in
    Settings' search, with no "Allow Paste" prompt -- a key-chord paste is the
    user's -- and the app searched for it as for typing.

    Nothing is pasted over a clipboard that could not be saved: what quern
    sets on a phone it undoes, and a clipboard it could not read it could
    not put back. Its contents never leave this function. A clipboard that
    could not be put back is handed to `restore_failed` -- on the answer,
    since the text was typed and is not typed again -- and is handed over
    even when the paste itself raised: the clipboard then holds the text
    whatever happens to it next (CodeRabbit on #784).
    """
    pasteboard = _pasteboard_service()(session.rsd)
    try:
        try:
            await asyncio.wait_for(pasteboard.connect(), CONNECT_S)
            saved = _pasteboard_items(await asyncio.wait_for(pasteboard.get(), PASTEBOARD_S))
        except Exception as e:  # noqa: BLE001 -- any failure here sent nothing
            raise NotSent(f"the clipboard could not be saved, so it was not used to "
                          f"paste the text: {e!r}") from e
        if saved is None:
            raise NotSent("the clipboard's contents could not be read, so it was not used "
                          "to paste the text")
        try:
            await asyncio.wait_for(pasteboard.set_text(text), PASTEBOARD_S)
            await _press(lib, session, progress, [(lib.COMMAND, lib.KEY_V)])
        finally:
            try:
                await asyncio.shield(asyncio.wait_for(pasteboard.set(saved), PASTEBOARD_S))
            except Exception as e:  # noqa: BLE001 -- reported, never raised over the paste
                if restore_failed is not None:
                    restore_failed(f"The phone's clipboard could not be put back ({e!r}): it "
                                   "now holds the typed text.")
    finally:
        await asyncio.shield(_bounded_close(pasteboard.close))


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
    #: How fast a sweep drags, and how long it holds at the end (#678). A
    #: faster HID drag carries a list on past the finger, hold or no hold:
    #: measured on an iPhone 11, 672 pt in 0.3 s moved it about 1,500 pt
    #: and 200 pt about 2.3x, while at 250 pt/s 672 pt moved it 647-660
    #: pt, six of six -- the 0.97 WDA's drag travels.
    sweep_drag_pt_s = 250.0
    #: Several contacts in one report, up to five: pinch, rotate, two-finger
    #: pan and taps (#252). Without it every gesture on an iOS 27 phone went
    #: through WDA, starting one where none was running (#769).
    multitouch = True
    sweep_hold_s = 0.5
    #: A physical device decides an edge swipe from where it starts, as for
    #: WDA; no flag is involved (#251).
    edge_swipes = True
    edge_flag = False
    #: A swipe can hold its contact still at the start first (#769).
    press_drag = True

    def __init__(self, fallback: Any) -> None:
        self._fallback = fallback
        self._sessions: dict[str, _Session] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._dropping: set[asyncio.Task] = set()
        # udid -> what the last typing on it has to say: a clipboard that
        # could not be put back after a paste.
        self._notes: dict[str, str] = {}

    def take_note(self, udid: str) -> str | None:
        """What the last typing on this phone has to say, once."""
        return self._notes.pop(udid, None)

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
                    edge: str | None = None, press: float = 0.0) -> str:
        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            if edge is not None:
                # As WDA does: the device takes a swipe from the edge from
                # where it starts, so one from the middle is refused, not sent.
                check_edge_start(edge, start_x, start_y, screen.width, screen.height,
                                 tool=self.TOOL_NAME)
            steps = max(2, round(duration * SWIPE_RATE_HZ))
            interval = duration / steps
            down: tuple[int, int] | None = None

            async def contact(point: tuple[int, int]) -> None:
                nonlocal down
                down = point        # before the send: it may land, then fail
                await _report(session.hid.send_touchscreen(lib.CONTACT, *point))

            progress.reached = True
            try:
                start = screen.point(start_x, start_y)
                if press:
                    # Pressed before it moves, the contact reported still at
                    # the start throughout, as a held end is: what
                    # drag-and-drop and reordering wait for (#769).
                    await contact(start)
                    for _ in range(max(1, round(press * SWIPE_RATE_HZ))):
                        await _sleep(1 / SWIPE_RATE_HZ)
                        await contact(start)
                for i in range(steps + 1):
                    f = i / steps
                    await contact(screen.point(start_x + (end_x - start_x) * f,
                                               start_y + (end_y - start_y) * f))
                    if i < steps:
                        await _sleep(interval)
                if hold:
                    # Held at the end, so a list stops where the finger does:
                    # the still contact is reported throughout, not just left
                    # down.
                    end = screen.point(end_x, end_y)
                    for _ in range(max(1, round(hold * SWIPE_RATE_HZ))):
                        await _sleep(1 / SWIPE_RATE_HZ)
                        await contact(end)
                await _report(session.hid.send_touchscreen(
                    lib.RELEASE, *screen.point(end_x, end_y)))
                down = None
            except BaseException:
                # Lifted where it last was, best effort: dropping the session
                # alone may leave the finger down (CodeRabbit on #770).
                if down is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(_report(
                            session.hid.send_touchscreen(lib.RELEASE, *down)))
                raise

        extra: dict = {"edge": edge} if edge is not None else {}
        if press:
            extra["press"] = press
        return await self._or_fallback(
            udid, "swipe", send,
            lambda: self._fallback.swipe(udid, start_x, start_y, end_x, end_y,
                                         duration, hold=hold, **extra))

    async def perform_gesture(self, udid: str, plan: Plan) -> str:
        """A gesture `server.device.gestures` laid out, every finger in every
        report: a contact left out of one is lifted. A moving gesture is sent
        at SWIPE_RATE_HZ along the plan's waypoints, paced against the clock
        so each report's round trip does not stretch it past `duration`; a
        tap gesture puts its fingers down, holds TAP_HOLD_MS and lifts them,
        `count` times. Points off the screen, and more fingers than a report
        carries, are refused. A failure or cancel with fingers down lifts
        them, best effort, before it is raised. Returns which of HID and WDA
        carried it."""
        fingers = len(plan.paths if plan.paths is not None else plan.points or [])
        if fingers > MAX_CONTACTS:
            # Refused before anything is sent: building the report would fail
            # after `progress.reached`, as "may have been partly delivered",
            # when nothing was (review).
            raise InvalidDeviceRequestError(
                f"a gesture of {fingers} fingers: a touchscreen report carries at most "
                f"{MAX_CONTACTS}", tool=self.TOOL_NAME)

        def check(screen: Screen) -> None:
            points = [p for path in plan.paths or [] for p in path] + list(plan.points or [])
            for x, y in points:
                if not (0 <= x <= screen.width and 0 <= y <= screen.height):
                    raise InvalidDeviceRequestError(
                        f"point ({x:.0f}, {y:.0f}) is off the "
                        f"{screen.width:.0f}x{screen.height:.0f} screen", tool=self.TOOL_NAME)

        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            check(screen)
            down: list[tuple[float, float]] | None = None

            async def report(points: list[tuple[float, float]], touching: bool = True) -> None:
                nonlocal down
                if touching:
                    # Before the send, not after: one that reached the phone
                    # and then failed or was cancelled left these fingers
                    # down with nothing to lift them (CodeRabbit on #770).
                    down = points
                await _report(session.hid.send_multitouch(
                    [lib.Contact(i, *screen.point(x, y), touching)
                     for i, (x, y) in enumerate(points)]))
                if not touching:
                    down = None

            async def moving(paths: list[list[tuple[float, float]]]) -> None:
                legs = len(paths[0]) - 1
                # At most SWIPE_RATE_HZ: a long rotation's waypoints are
                # interpolated between, not each sent (review).
                frames = max(1, round(plan.duration * SWIPE_RATE_HZ))

                def at(f: float) -> list[tuple[float, float]]:
                    # Along each finger's waypoints, linearly between them.
                    pos = f * legs
                    i = min(int(pos), legs - 1)
                    t = pos - i
                    return [(p[i][0] + (p[i + 1][0] - p[i][0]) * t,
                             p[i][1] + (p[i + 1][1] - p[i][1]) * t) for p in paths]

                start = time.monotonic()
                for n in range(frames + 1):
                    await report(at(n / frames))
                    if n < frames:
                        # To the frame's own time: a report's round trip is
                        # taken out of the wait, not added to it (review).
                        due = start + (n + 1) * plan.duration / frames
                        await _sleep(max(0.0, due - time.monotonic()))
                await report([p[-1] for p in paths], touching=False)

            async def tapping(points: list[tuple[float, float]]) -> None:
                for n in range(plan.count):
                    await report(points)
                    await _sleep(TAP_HOLD_MS / 1000)
                    await report(points, touching=False)
                    if n < plan.count - 1:
                        await _sleep(plan.interval)

            progress.reached = True
            try:
                if plan.paths is not None:
                    await moving(plan.paths)
                else:
                    await tapping(list(plan.points or []))
            except BaseException:
                # Fingers left down by a failure or a cancel are lifted where
                # they were, best effort: dropping the session alone may not
                # lift them on the phone (review).
                if down is not None:
                    lift = down
                    with contextlib.suppress(Exception):
                        await asyncio.shield(_report(session.hid.send_multitouch(
                            [lib.Contact(i, *screen.point(x, y), False)
                             for i, (x, y) in enumerate(lift)])))
                raise

        return await self._or_fallback(udid, plan.kind, send,
                                       lambda: self._fallback.perform_gesture(udid, plan))

    async def press_button(self, udid: str, button: str) -> str:
        code = BUTTONS.get(button.lower())

        async def send(lib: SimpleNamespace, session: _Session, screen: Screen | None,
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

        # No screen read: a button report has no coordinates, so a phone in
        # landscape presses Home over HID as well as one in portrait.
        return await self._or_fallback(udid, f"{button} button", send,
                                       lambda: self._fallback.press_button(udid, button),
                                       needs_screen=False)

    async def type_text(self, udid: str, text: str) -> str:
        """Type `text` as US key positions on a virtual keyboard (#664).

        On a keyboard of its own, closed after the text (`_press`). Text
        with a character no US key types is pasted instead (`_paste`, #780);
        the controller sets such text through the accessibility service
        instead when it has a field to set it in. Why the clipboard could not
        be put back, if it could not, is kept for `take_note`.
        """
        self._notes.pop(udid, None)

        async def send(lib: SimpleNamespace, session: _Session, screen: Screen | None,
                       progress: _Progress) -> None:
            keys = [lib.KEYS.get(ch) for ch in text]
            if None in keys:
                await _paste(lib, session, progress, text,
                             restore_failed=lambda note: self._notes.__setitem__(udid, note))
                return
            await _press(lib, session, progress,
                         [(lib.SHIFT if shift else None, usage) for usage, shift in keys])

        return await self._or_fallback(udid, "typing", send,
                                       lambda: self._fallback.type_text(udid, text),
                                       needs_screen=False)

    async def select_all_and_delete(self, udid: str, x: float, y: float,
                                    element_type: str | None = None,
                                    identifier: str | None = None) -> str:
        """Clear the field at (x, y): tap it, then Cmd+A and Backspace (#664).

        Keystrokes, as WDA's clear is, so the app sees the edit: measured on an
        iPhone 11, its delegate saw one replacement of all 8 characters of an
        email field with "" -- a password field's 6 too. The type and
        identifier WDA's version takes are not needed here.
        """
        async def send(lib: SimpleNamespace, session: _Session, screen: Screen,
                       progress: _Progress) -> None:
            hx, hy = screen.point(x, y)
            progress.reached = True
            await _report(session.hid.send_touchscreen(lib.CONTACT, hx, hy))
            await _sleep(TAP_HOLD_S)
            await _report(session.hid.send_touchscreen(lib.RELEASE, hx, hy))
            await _sleep(FOCUS_S)
            await _press(lib, session, progress,
                         [(lib.COMMAND, lib.KEY_A), (None, lib.BACKSPACE)])

        return await self._or_fallback(
            udid, "clearing", send,
            lambda: self._fallback.select_all_and_delete(
                udid, x=x, y=y, element_type=element_type, identifier=identifier))

    # -- delivery ----------------------------------------------------------

    async def _or_fallback(self, udid: str, what: str, send: Any, fallback: Any,
                           needs_screen: bool = True) -> str:
        """Send over HID; if nothing could be sent, do it through WDA. Returns
        which of the two carried it."""
        try:
            await self._send(udid, what, send, needs_screen)
        except NotSent as e:
            from server.device.ios.phone_readiness import (
                NO_IMAGE_FIX,
                ensure_image,
                missing_image,
            )

            # HID is the image's: mounted, the same send goes over HID.
            if missing_image(e) and await ensure_image(udid):
                try:
                    await self._send(udid, what, send, needs_screen)
                    return self.TOOL_NAME
                except NotSent as again:
                    e = again
            if isinstance(e, _NoTunnel):
                raise NoTunnelError(
                    f"{what} on {udid[:8]} was not sent: {e}. WebDriverAgent is "
                    "reached through the same tunnel, so it was not started instead.",
                    tool=self.TOOL_NAME) from e
            logger.info("[coredevice] %s on %s through WDA: %s", what, udid[:8], e)
            try:
                await fallback()
            except DeviceError as wda:
                from server.models import WdaNotAutoStartedError

                if isinstance(wda, WdaNotAutoStartedError):
                    # No-WDA mode (#780): why HID could not send it is the
                    # first thing to say, then that WDA was not started for it
                    # -- the image's own fix when that is what was missing.
                    why = NO_IMAGE_FIX if missing_image(e) else e
                    raise WdaNotAutoStartedError(
                        f"{what} on {udid[:8]} was not sent over CoreDevice: {why}. {wda}",
                        tool=self.TOOL_NAME) from wda
                # WDA's refusal alone named WDA and Xcode, when what was
                # missing is the image HID needs (#664 phase 5).
                if missing_image(e):
                    raise DeviceError(
                        f"{what} on {udid[:8]} was not sent. {NO_IMAGE_FIX}. "
                        f"WebDriverAgent could not send it instead: {wda}",
                        tool=self.TOOL_NAME) from wda
                raise
            return getattr(self._fallback, "TOOL_NAME", "wda")
        return self.TOOL_NAME

    async def _send(self, udid: str, what: str, send: Any, needs_screen: bool = True) -> None:
        """Send on the phone's session, once more on a fresh one if the first
        failed before anything reached the phone (#780). The failed session
        is already dropped, so the second try opens its own."""
        try:
            await self._send_once(udid, what, send, needs_screen)
        except _Transient as e:
            logger.info("[coredevice] %s on %s: trying a fresh session: %s", what, udid[:8], e)
            await self._send_once(udid, what, send, needs_screen)

    async def _send_once(self, udid: str, what: str, send: Any, needs_screen: bool) -> None:
        lib = _pmd3()
        while True:
            session = await self._session(udid, lib)
            async with session.lock:
                # Retired while this waited for the lock -- by the reaper, or
                # by an action that failed on it -- so not used: take a fresh
                # one (CodeRabbit on #671).
                if self._sessions.get(udid) is session:
                    await self._send_on(udid, what, send, needs_screen, lib, session)
                    return

    async def _send_on(self, udid: str, what: str, send: Any, needs_screen: bool,
                       lib: SimpleNamespace, session: _Session) -> None:
        """One action on a session whose lock the caller holds."""
        session.last_used = time.monotonic()
        progress = _Progress()
        try:
            screen = await self._screen(session, lib) if needs_screen else None
            await send(lib, session, screen, progress)
        except _ReadFailed:
            await self._drop(udid, session)
            raise
        except (NotSent, InvalidDeviceRequestError):
            # Nothing sent, and nothing wrong with the session: a screen this
            # cannot map, a button with no code, an edge swipe that does not
            # start at its edge.
            raise
        except asyncio.CancelledError:
            # Cancelled mid-gesture, perhaps with a contact still down. The
            # session goes with it, so the next action does not start on a
            # finger that never lifted.
            await asyncio.shield(self._drop(udid, session))
            raise
        except Exception as e:
            # A session that failed is not trusted again; the next action opens
            # a fresh one.
            await self._drop(udid, session)
            if not progress.reached:
                raise _Transient(f"the HID service failed before sending: {e!r}") from e
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
            except NoTunnelError as e:
                raise _NoTunnel(str(e)) from e
            except Exception as e:
                # Not only DeviceError: tunneld or a resolver can fail any
                # way, and nothing has been sent.
                raise _Transient(f"the device's tunnel address could not be found: {e!r}") from e
            rsd = rsd_for(lib, address)
            hid = None
            try:
                await asyncio.wait_for(rsd.connect(), CONNECT_S)
                hid = lib.UniversalHID(rsd)
                await asyncio.wait_for(hid.connect(), CONNECT_S)
                # Inside the open, so every action on a new session waits and
                # one cancelled here is closed like any half-open session; an
                # action on a session already open does not wait.
                await _sleep(FIRST_REPORT_S)
            except asyncio.CancelledError:
                # Cancelled half open: neither is registered yet, so nothing
                # else would ever close them. Still a cancellation, not NotSent
                # (CodeRabbit on #671).
                await asyncio.shield(_bounded_close(
                    *(c for c in (getattr(hid, "close", None), rsd.close) if c)))
                raise
            except Exception as e:
                await _bounded_close(*(c for c in (getattr(hid, "close", None), rsd.close) if c))
                raise _Transient(f"the HID service could not be opened: {e!r}") from e
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
                    drop = asyncio.ensure_future(self._retire(udid, session))
                    self._dropping.add(drop)
                    drop.add_done_callback(self._dropping.discard)
                    await asyncio.shield(drop)

    async def _retire(self, udid: str, session: _Session) -> None:
        """Close an idle session, holding its lock so no action is on it, and
        only if it is still idle once the lock is had: an action may have
        taken it between the reaper's look and now (CodeRabbit on #671)."""
        async with session.lock:
            if time.monotonic() - session.last_used >= IDLE_CLOSE_S:
                await self._drop(udid, session)

    async def close(self) -> None:
        """Close every held session (server shutdown)."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        await asyncio.gather(*self._dropping, return_exceptions=True)
        for udid, session in list(self._sessions.items()):
            await self._drop(udid, session)
