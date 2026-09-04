"""Waiting for the screen to stop changing.

The alternative is a sleep, and no constant is right: the same screen settles in
0.65s cached and takes arbitrarily longer on a cold load. These tests drive the
detector with synthetic frames, so the behaviour under test is the decision rule
rather than any particular device's timing.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from server.device.settle import (
    DEFAULT_EPSILON,
    changed_fraction,
    wait_settled,
)

SIZE = (400, 800)   # quarter-scale: 100x200 = 20,000 px, near a real screen


def frame(fill: int = 255, *, blot: tuple[int, int, int, int] | None = None,
          size: tuple[int, int] = SIZE) -> bytes:
    """A PNG of a flat colour, optionally with a rectangle painted on it."""
    image = Image.new("L", size, fill)
    if blot:
        for x in range(blot[0], blot[2]):
            for y in range(blot[1], blot[3]):
                image.putpixel((x, y), 0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# Captures allowed before a test decides the loop is not terminating. Generous
# against the handful any of these need, and small enough to fail fast.
MAX_CAPTURES = 200


class LoopDidNotTerminate(AssertionError):
    pass


def paced(sequence, delay: float = 0.005):
    """A capture that takes a little real time, cycling through `sequence`.

    A synthetic capture returns instantly, so a wall-clock timeout of 0.3s would
    fit tens of thousands of frames and trip any sane capture cap. A real one
    costs ~114ms; this keeps the ratio of frames to seconds within sight of that
    without making the suite slow.
    """
    index = 0

    async def capture():
        nonlocal index
        await asyncio.sleep(delay)
        png = sequence[index % len(sequence)]
        index += 1
        return png

    return capture


def capped(capture, limit: int = MAX_CAPTURES):
    """Make a capture raise once it has been called too many times.

    asyncio.wait_for cannot rescue this: wait_settled polls in a loop whose
    await returns without suspending, so a cancellation never gets delivered
    and the task spins instead of stopping. The only reliable brake is the
    thing the loop calls -- so the capture counts itself and raises, turning a
    missing timeout into a failed test rather than a hung suite.
    """
    calls = 0

    async def limited():
        nonlocal calls
        calls += 1
        if calls > limit:
            raise LoopDidNotTerminate(
                f"wait_settled made {calls} captures without returning"
            )
        return await capture()

    return limited


async def bounded(awaitable, limit: float = 10.0):
    return await asyncio.wait_for(awaitable, timeout=limit)


def capturing(*frames: bytes):
    """A capture callable that yields the given frames, then repeats the last."""
    sequence = list(frames)
    calls: list[int] = []

    async def capture() -> bytes | None:
        index = min(len(calls), len(sequence) - 1)
        calls.append(index)
        return sequence[index]

    capture.calls = calls
    return capture


# ------------------------------------------------------------ settling

async def test_a_static_screen_settles():
    result = await bounded(wait_settled(capped(capturing(frame())), timeout=5))
    assert result.settled is True
    assert result.frames == 3, "two comparisons need three frames"


async def test_a_screen_still_changing_does_not_settle_early():
    """Frames that keep differing must not be mistaken for a settled screen.

    The blot cycles rather than running out of positions: a sequence that goes
    static partway through would settle legitimately and prove nothing.
    """
    moving = [frame(blot=(0, y, 200, y + 200)) for y in range(0, 400, 50)]
    result = await bounded(wait_settled(capped(paced(moving)), timeout=0.35))
    assert result.settled is False
    assert "still changing" in result.reason


async def test_one_matching_pair_is_not_enough():
    """A frame taken mid-animation can match its predecessor by coincidence --
    an element pausing at the extreme of an ease curve. Two consecutive
    comparisons are required."""
    pause = frame(blot=(0, 0, 40, 40))
    sequence = [pause, pause, frame(blot=(0, 60, 40, 100)), frame(), frame(), frame()]

    async def capture(_i=[0]):
        png = sequence[min(_i[0], len(sequence) - 1)]
        _i[0] += 1
        return png

    result = await bounded(wait_settled(capped(capture), timeout=5, stable_frames=2))
    assert result.settled is True
    # Not at frame 2, where the first coincidental match happened.
    assert result.frames == 6


async def test_a_settled_screen_reports_how_little_changed():
    result = await bounded(wait_settled(capped(capturing(frame())), timeout=5))
    assert result.last_change == 0.0


async def test_the_wait_is_bounded():
    """Animated content never settles; the wait has to end anyway."""
    flicker = [frame(), frame(blot=(0, 0, 400, 800))]
    result = await bounded(wait_settled(capped(paced(flicker)), timeout=0.3))
    assert result.settled is False
    assert result.elapsed_ms >= 300
    assert "animating" in result.reason


async def test_a_timeout_reports_how_much_was_still_moving():
    """A spinner sits at a steady non-zero value rather than approaching the
    threshold, which is the difference between "slow" and "never"."""
    flicker = [frame(), frame(blot=(0, 0, 400, 400))]
    result = await bounded(wait_settled(capped(paced(flicker)), timeout=0.3))
    assert result.last_change is not None and result.last_change > 0.1


# ------------------------------------------------------------ capture failure

async def test_a_capture_that_fails_is_not_a_settled_screen():
    async def capture():
        return None

    result = await bounded(wait_settled(capped(capture), timeout=5))
    assert result.settled is False
    # Specifically the capture failure, not the decode one: both messages
    # mention "capture", so a loose check passes either way and cannot tell
    # whether a missing capture even reached the decoder.
    assert result.reason == "could not capture the screen"


async def test_an_undecodable_capture_is_not_a_settled_screen():
    async def capture():
        return b"not a png"

    result = await bounded(wait_settled(capped(capture), timeout=5))
    assert result.settled is False
    assert result.reason == "could not decode the capture"


# ------------------------------------------------------------ comparison

def test_a_resized_screen_counts_as_wholly_changed():
    """A rotation or a device swap is not a settling screen."""
    from server.device.settle import _prepare
    assert changed_fraction(_prepare(frame()), _prepare(frame(size=(800, 400)))) == 1.0


def test_small_noise_stays_under_the_threshold():
    """An idle simulator measures 0.00017 frame to frame. The threshold has to
    clear that, or nothing ever settles."""
    from server.device.settle import _prepare
    speck = changed_fraction(_prepare(frame()), _prepare(frame(blot=(0, 0, 2, 2))))
    assert speck < DEFAULT_EPSILON


def test_a_visible_change_exceeds_the_threshold():
    from server.device.settle import _prepare
    change = changed_fraction(_prepare(frame()), _prepare(frame(blot=(0, 0, 200, 200))))
    assert change > DEFAULT_EPSILON


@pytest.mark.parametrize("stable_frames", [1, 3])
async def test_the_required_run_of_stable_frames_is_configurable(stable_frames):
    result = await bounded(wait_settled(capped(capturing(frame())), timeout=5,
                                        stable_frames=stable_frames))
    assert result.settled is True
    assert result.frames == stable_frames + 1


async def test_a_capture_that_blocks_does_not_outlast_the_timeout():
    """The deadline is only consulted between frames, so a capture that hangs
    would run past it entirely. A wedged simulator does exactly that."""
    async def slow():
        await asyncio.sleep(30)
        return frame()

    started = asyncio.get_running_loop().time()
    result = await bounded(wait_settled(slow, timeout=0.4), limit=5)
    assert result.settled is False
    assert asyncio.get_running_loop().time() - started < 3, "ran past its timeout"
    assert result.frames == 0
    assert "not answering" in result.reason


async def test_an_animating_screen_is_described_as_animating():
    """Captures run back to back, so the deadline nearly always expires while
    one is in flight. That must not be reported as a slow device."""
    flicker = [frame(), frame(blot=(0, 0, 400, 800))]
    result = await bounded(wait_settled(capped(paced(flicker)), timeout=0.3))
    assert result.settled is False
    assert "animating" in result.reason
    assert result.frames > 1
