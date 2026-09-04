"""Wait for the screen to stop changing.

A sleep cannot answer "has this finished drawing?" -- whatever constant you
pick is too long for a cached screen and too short for a slow one. Measured
against a web form in an out-of-process SFSafariViewController, where typing is
silently lost if it arrives early: a 0.2s sleep landed the keystroke 1 time in
5, a 1.0s sleep landed 5 of 5, and waiting for the screen to settle landed 5 of
5 in 0.65-0.69s. Faster than the sleep that was reliable, and reliable unlike
the sleep that was fast.

Comparing frames needs no privileged channel, which is what makes it general.
The accessibility tree cannot see inside a WKWebView, and an
ASWebAuthenticationSession is not published to the Web Inspector at all -- but
both draw pixels.

What this cannot answer is whether anything has *arrived*. A blank page part way
through a slow load is perfectly still, and settles: measured against a request
to a non-routable address, Safari showed a white screen with its progress bar
stalled and this reported settled after 1.6s with no change at all between
frames. A longer timeout does not help, because nothing is moving. Waiting for a
slow load means waiting on the content -- and then waiting for it to settle.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("quern-debug-server.settle")

CaptureFn = Callable[[], Awaitable[bytes | None]]

# Fraction of pixels that may differ between two frames and still count as
# unchanged. Not a guess: an idle simulator screen measures 0.00017 frame to
# frame -- rendering noise and the status bar clock -- so this clears the floor
# with room, while a blinking caret (1.8% of pixels at its peak) exceeds it and
# merely delays settling by one blink.
DEFAULT_EPSILON = 0.001

# Consecutive unchanged comparisons required. One is not enough: a frame taken
# mid-animation can match its predecessor by coincidence when an element pauses
# at the extreme of an ease curve.
DEFAULT_STABLE_FRAMES = 2

# Per-pixel intensity difference below which a pixel counts as unchanged, on a
# 0-255 grayscale. Absorbs compression and subpixel-rendering noise.
PIXEL_THRESHOLD = 12

# Captures are downscaled by this factor before comparison. Settling is a
# whole-screen question, and at quarter scale the comparison is small change
# against the capture, which dominates at ~130-210ms.
DOWNSCALE = 4


@dataclass
class SettleResult:
    settled: bool
    elapsed_ms: float
    frames: int
    last_change: float | None = None
    """Fraction of pixels that differed on the final comparison. Useful when a
    wait times out: a screen with a spinner sits at some steady non-zero value
    rather than approaching the threshold."""

    reason: str | None = None


def _prepare(png: bytes):
    """Decode to a small grayscale image, or None if it cannot be read."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return None
    try:
        image = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        logger.debug("settle: could not decode a capture", exc_info=True)
        return None
    if DOWNSCALE > 1:
        image = image.resize((max(1, image.width // DOWNSCALE),
                              max(1, image.height // DOWNSCALE)))
    return image


def changed_fraction(before, after, threshold: int = PIXEL_THRESHOLD) -> float:
    """Fraction of pixels differing by more than `threshold`.

    Two frames of different sizes have wholly changed -- a rotation or a device
    swap, not a settling screen.
    """
    from PIL import ImageChops

    if before.size != after.size:
        return 1.0
    delta = ImageChops.difference(before, after)
    mask = delta.point(lambda p: 255 if p > threshold else 0)
    return sum(mask.histogram()[255:]) / float(before.width * before.height)


async def wait_settled(
    capture: CaptureFn,
    *,
    timeout: float = 10.0,
    epsilon: float = DEFAULT_EPSILON,
    stable_frames: int = DEFAULT_STABLE_FRAMES,
) -> SettleResult:
    """Poll captures until consecutive frames stop differing.

    `capture` is injected so this is testable without a device, the same
    contract `probing.probe_container` uses for `describe_point`.

    There is deliberately no delay between captures. A capture already costs
    ~130-210ms, which is the natural cadence, and adding a sleep on top would
    reintroduce the guessed constant this exists to remove -- measured, polling
    at capture rate settles a presented sheet in 0.65s where a 0.25s inter-frame
    delay would take roughly twice as long to reach the same conclusion.
    """
    started = time.perf_counter()
    deadline = started + timeout
    previous = None
    stable = 0
    frames = 0
    last_change: float | None = None

    while True:
        png = await capture()
        if png is None:
            return SettleResult(
                settled=False, elapsed_ms=(time.perf_counter() - started) * 1000,
                frames=frames, last_change=last_change,
                reason="could not capture the screen",
            )
        current = _prepare(png)
        if current is None:
            return SettleResult(
                settled=False, elapsed_ms=(time.perf_counter() - started) * 1000,
                frames=frames, last_change=last_change,
                reason="could not decode the capture",
            )
        frames += 1

        if previous is not None:
            last_change = changed_fraction(previous, current)
            stable = stable + 1 if last_change < epsilon else 0
            if stable >= stable_frames:
                return SettleResult(
                    settled=True,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    frames=frames, last_change=last_change,
                )
        previous = current

        # Checked after a comparison, not before a capture: a screen that
        # settles exactly at the deadline should be reported settled.
        if time.perf_counter() >= deadline:
            return SettleResult(
                settled=False, elapsed_ms=(time.perf_counter() - started) * 1000,
                frames=frames, last_change=last_change,
                reason=(
                    "the screen was still changing when the timeout expired"
                    + (f" (last comparison differed by {last_change:.2%})"
                       if last_change is not None else "")
                    + ". Something is animating -- a spinner, a video, a "
                    "carousel -- and will not settle."
                ),
            )
