"""Did the screen change since the last tap? (#502)

After a miss, the question an agent asks is "did my last tap do anything?".
#499 answers it from a knowledge base (`previous_tap`); this answers it
without one, by comparing a frame of the screen taken just before the last
delivered `tap_element` with one taken at the miss.

It is said on the miss, never on the tap: "nothing changed" is often meant --
a label tapped, a switch already set -- and only once something has failed is
it the useful fact. The frame before the tap is the per-tap cost, so each
backend uses its fastest capture.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Below this fraction of changed pixels the screen counts as unchanged. The
#: frames are the content area at a quarter of the resolution, grayscale,
#: compared per pixel with settle's threshold -- the comparison #475's sweep
#: trusts to tell a list that moved from one that did not (0.008 there).
UNCHANGED_BELOW = 0.008

#: The top of the screen left out of the comparison: the status bar, whose
#: clock and indicators change on their own. A fraction of the frame's height,
#: so it holds at any scale; a little generous, since cutting a sliver of
#: content costs nothing and leaving the clock in costs the whole signal.
STATUS_BAR_FRACTION = {"ios": 0.07, "android": 0.05}

#: A capture slower than this is abandoned: the tap must not wait on it.
CAPTURE_TIMEOUT = 3.0

#: How long a tap's frame is kept for the next miss, as `previous_tap` is.
TTL = 120.0


@dataclass
class TapFrame:
    """A delivered tap and the screen just before it."""

    tapped: dict
    #: The capture as the backend returned it. Decoded only at a miss: the
    #: decode and resize are most of the cost on a fast backend, and a tap
    #: that is not followed by a miss never needs them.
    png: bytes
    platform: str
    at: float
    #: The device's last screen change as the tap left it (#499's stamp).
    stamp: float | None
    capture_ms: float
    source: str


async def _capture(controller, udid: str) -> tuple[bytes | None, float, str]:
    """The screen as the backend returns it, on its fastest path.

    Returns `(image bytes, milliseconds, source)`; the bytes are None when
    the capture failed or was too slow, which the caller treats as "no
    frame", never as a changed screen.
    """
    start = time.monotonic()
    source = "none"
    try:
        if controller._served_by_wda(udid):
            source = "wda"
            png = await asyncio.wait_for(controller.wda_client.screenshot(udid), CAPTURE_TIMEOUT)
        elif controller._is_android(udid):
            source = "adb"
            png = await asyncio.wait_for(controller.raw_screenshot(udid), CAPTURE_TIMEOUT)
        elif controller._ui_backend(udid) is getattr(controller, "sim_bridge", None):
            source = "sim-bridge"
            png = await asyncio.wait_for(
                controller.sim_bridge.screenshot(udid, quality=0.5), CAPTURE_TIMEOUT)
        else:
            source = "simctl"
            png = await asyncio.wait_for(controller.raw_screenshot(udid), CAPTURE_TIMEOUT)
    except Exception:  # noqa: BLE001 -- no frame is an answer, never a failure
        logger.debug("frame capture failed on %s", source, exc_info=True)
        png = None
    if not png:
        png = None
    return png, (time.monotonic() - start) * 1000, source


def platform_of(source: str) -> str:
    """Which status bar to crop for a capture from `source`."""
    return "android" if source == "adb" else "ios"


def prepare(png: bytes | None, platform: str):
    """A capture as a small grayscale image of the content, or None."""
    from server.device.settle import _prepare

    if not png:
        return None
    image = _prepare(png)
    if image is None:
        return None
    top = int(image.height * STATUS_BAR_FRACTION[platform])
    return image.crop((0, top, image.width, image.height))


#: The capture the routes call. A name of its own so the test suite can stand
#: it in for every test at once (`tests/conftest.py`): a route test that taps
#: would otherwise capture a real screen.
capture = _capture


def unchanged_note(
    tap: TapFrame, after_png: bytes | None, now: float, *, stamp: float | None,
) -> dict | None:
    """`screen_unchanged_since_tap`, when the screen has not changed since
    the tap -- or None.

    Silent unless every one of these holds: there is a frame after; nothing
    has changed the screen through quern since the tap (`stamp`); the tap is
    under two minutes old; both captures decode; and they differ by less than
    `UNCHANGED_BELOW`. Frames of different sizes are a changed screen.
    """
    from server.device.settle import changed_fraction

    if after_png is None or stamp != tap.stamp or now - tap.at > TTL:
        return None
    before = prepare(tap.png, tap.platform)
    after = prepare(after_png, tap.platform)
    if before is None or after is None:
        return None
    change = changed_fraction(before, after)
    if change >= UNCHANGED_BELOW:
        return None
    label = tap.tapped.get("label") or tap.tapped.get("identifier") or "the element"
    return {
        "tapped": {k: tap.tapped.get(k) for k in ("label", "identifier", "type")},
        "seconds_ago": round(now - tap.at, 1),
        "change": round(change, 4),
        "note": (
            f"The screen looks the same as just before the previous tap on {label!r}: "
            "that tap changed nothing visible. If it was meant to navigate or open "
            "something, it probably did not register, which would explain this miss. "
            "Check the screen before repeating it -- not blindly for a submit, a "
            "purchase or a send."
        ),
    }
