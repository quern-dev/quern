"""Has the screen changed since the last tap? (#502)

After a miss, the question an agent asks is "did my last tap do anything?".
#499 answers it from a knowledge base (`previous_tap`); this answers it
without one, by comparing a frame of the screen taken just before the last
delivered `tap_element` with one taken at the miss.

It is said on the miss, never on the tap: "nothing changed" is often meant --
a label tapped, a switch already set -- and only once something has failed is
it the useful fact. The frame before the tap is the per-tap cost, so each
backend uses its fastest capture.

**It must never claim "unchanged" over a real change.** So the comparison is
near-exact: identical screens measured 0.0 on every backend, while what a tap
that does not navigate actually changes is small -- a switch flipping is
~0.5% of the frame, a checkmark appearing ~0.05%, one digit ~0.045% (#502
review). A spinner or a blinking caret therefore reads as *changed*, and an
animated screen gets no note: a missed hint, never a false one.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: At or above this fraction of changed pixels the screen has changed. About
#: four pixels of a 100-pixel-wide frame: identical screens compare at 0.0 on
#: every backend (WDA, sim-bridge, uiautomator2, adb), and the smallest real
#: effect measured -- a digit changing -- is ~0.00045.
UNCHANGED_BELOW = 0.0002

#: Every frame is decoded straight to this width, whatever captured it, so a
#: frame from one path compares with a frame from another.
FRAME_WIDTH = 100

#: The top of a portrait screen left out of the comparison: the status bar,
#: whose clock and indicators change on their own. A fraction of the height,
#: so it holds at any scale. A landscape iPhone has no status bar, so a
#: landscape frame is not cropped. A change confined to this band is missed:
#: the safe direction.
STATUS_BAR_FRACTION = {"ios": 0.07, "android": 0.05}

#: The whole capture, fallback included, is abandoned past this: the tap must
#: not wait on it.
CAPTURE_TIMEOUT = 3.0

#: How long a tap's frame is kept for the next miss, as `previous_tap` is.
TTL = 120.0

#: A miss sooner than this after the tap says nothing: the screen may still be
#: arriving. The same grace #499 gives `previous_tap` (`MISS_MIN_AGE`); a
#: documented edge's `timeout:` extends it.
MIN_AGE = 1.0


@dataclass
class TapFrame:
    """A delivered tap and the screen just before it."""

    tapped: dict
    #: The capture as the backend returned it. Decoded only at a miss: a tap
    #: that is not followed by one never needs it.
    png: bytes
    platform: str
    at: float
    #: The device's last screen change as the tap left it (#499's stamp).
    stamp: float | None
    capture_ms: float
    source: str
    #: How long after the tap a miss must wait before this can be said.
    grace: float = MIN_AGE


async def _capture(controller, udid: str) -> tuple[bytes | None, float, str]:
    """The screen as the backend returns it, on its fastest path.

    Returns `(image bytes, milliseconds, source)`; the bytes are None when
    the capture failed, was too slow, or would have had to start something
    -- a WDA connection, a uiautomator2 server -- that a tap must not start
    as a side effect. None is "no frame", never a changed screen.
    """
    start = time.monotonic()
    deadline = start + CAPTURE_TIMEOUT
    source = "none"

    def left() -> float:
        return max(0.01, deadline - time.monotonic())

    try:
        if controller._served_by_wda(udid):
            source = "wda"
            if not controller.wda_client.has_connection(udid):
                return None, 0.0, source
            png = await asyncio.wait_for(controller.wda_client.screenshot(udid), left())
        elif controller._is_android(udid):
            png = None
            # uiautomator2's scaled JPEG is about three times as fast as adb's
            # full PNG -- but only on a device already connected: connecting,
            # or a failed call, can restart the u2 server in the background,
            # racing the tap's own u2 call (#502 review).
            if controller.u2.is_connected(udid):
                try:
                    source = "u2"
                    png = await asyncio.wait_for(controller.u2.frame(udid), left())
                except Exception:  # noqa: BLE001
                    logger.debug("u2 frame failed; falling back to adb", exc_info=True)
                    png = None
            if png is None:
                source = "adb"
                png = await asyncio.wait_for(controller.adb.screenshot(udid), left())
        elif controller._ui_backend(udid) is getattr(controller, "sim_bridge", None):
            source = "sim-bridge"
            png = await asyncio.wait_for(
                controller.sim_bridge.screenshot(udid, quality=0.5), left())
        else:
            source = "simctl"
            png = await asyncio.wait_for(controller.raw_screenshot(udid), left())
    except Exception:  # noqa: BLE001 -- no frame is an answer, never a failure
        logger.debug("frame capture failed on %s", source, exc_info=True)
        png = None
    if not png:
        png = None
    return png, (time.monotonic() - start) * 1000, source


def platform_of(source: str) -> str:
    """Which status bar to crop for a capture from `source`."""
    return "android" if source in ("u2", "adb") else "ios"


def prepare(png: bytes | None, platform: str):
    """A capture as a small grayscale image of the content, or None.

    Decoded straight to `FRAME_WIDTH`: resizing in two steps rounded a scaled
    JPEG and a full PNG of one screen to different heights, so they never
    compared (#502 review).
    """
    if not png:
        return None
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("L")
    except Exception:  # noqa: BLE001 -- an undecodable capture is no frame
        logger.debug("frame could not be decoded", exc_info=True)
        return None
    portrait = image.height >= image.width
    height = max(1, round(image.height * FRAME_WIDTH / image.width))
    image = image.resize((FRAME_WIDTH, height))
    top = int(image.height * STATUS_BAR_FRACTION[platform]) if portrait else 0
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
    older than its grace and younger than two minutes; both captures decode
    to the same size; and they differ by less than `UNCHANGED_BELOW`.
    """
    from server.device.settle import changed_fraction

    if after_png is None or stamp != tap.stamp:
        return None
    age = now - tap.at
    if age < tap.grace or age > TTL:
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
        "seconds_ago": round(age, 1),
        "change": round(change, 5),
        "note": (
            f"The screen looks exactly as it did just before the previous tap on "
            f"{label!r}, {round(age, 1)}s ago, and nothing quern did has changed it "
            "since. If that tap was meant to navigate, open or toggle something, it "
            "probably did not register, which would explain this miss. Check the "
            "screen before repeating it -- not blindly for a submit, a purchase or a "
            "send."
        ),
    }
