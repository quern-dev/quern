"""Which element a tap reached: the probe's Taps tab (#511).

A tap check elsewhere usually means `tap_element` said `ok` and the next read
looked plausible. A tap on the wrong element can look the same: both return
`ok`, and on a screen where nothing visibly changes, both read the same
afterwards. Every button on the Taps tab counts its own taps into a label,
`<identifier>_count`, so these tests read which button the tap reached and
assert that nothing else moved.

Not asserted here: the slider that never stops jumping (`tap_slide_arm`). A tap
sent right after arming reached a stale position, with `ok`, in 10 of 10 tries
on a simulator (nine reached nothing, one the behind button) and 1 in 10 on an
Android emulator. An assertion would pass or fail by device and by run, so
there is none until quern checks that a target is still before tapping it
(#514).
"""

from __future__ import annotations

import time

import pytest

COUNTED = (
    "tap_target", "tap_decoy", "tap_twin_a", "tap_twin_b", "tap_slide_behind",
    "tap_slider", "tap_remount", "tap_overlay_target", "tap_overlay",
)


@pytest.fixture
def taps(probe):
    probe.goto("taps")
    _reset(probe)
    return probe


def _reset(probe) -> None:
    probe.tap("tap_reset")
    time.sleep(0.6)
    stale = {k: v for k, v in _counts(probe).items() if v}
    assert not stale, f"tap_reset left counters set: {stale}"


def _counts(probe) -> dict[str, int]:
    """Every counter on the tab, by the button it counts.

    All of them, every time: a missing label fails here rather than reading
    as zero, which would let a tap on that button pass for no tap at all.
    """
    labels = {
        e.get("identifier"): e.get("label") or e.get("value") or ""
        for e in probe.ui_tree().get("elements") or []
    }
    out = {}
    for button in COUNTED:
        text = labels.get(f"{button}_count")
        assert text, f"{button}_count is not on screen: {sorted(k for k in labels if k)}"
        out[button] = int(text.rsplit(" ", 1)[-1])
    return out


def _reached(probe, expected: str | None) -> None:
    """Exactly `expected` counted one tap, and no other button counted any."""
    time.sleep(0.6)
    counts = _counts(probe)
    hit = {k: v for k, v in counts.items() if v}
    want = {expected: 1} if expected else {}
    assert hit == want, f"expected {want or 'no button'} to count a tap; counters read {hit}"


def _remount_elements(probe) -> list[dict]:
    return [e for e in probe.ui_tree().get("elements") or []
            if e.get("identifier") == "tap_remount"]


def _at(element: dict) -> tuple:
    frame = element.get("frame") or {}
    return (frame.get("x"), frame.get("y"))


def _tap(probe, **locator) -> dict:
    """The tap's answer, whatever its HTTP status: a 404 `not_found` or a
    refusal is an answer these tests read, not an error."""
    resp = probe.client.post(
        "/api/v1/device/ui/tap-element",
        json={"udid": probe.udid, **locator, **probe.NO_SCROLL, **probe.READ},
        timeout=90.0,
    )
    body = resp.json()
    if not resp.is_success and isinstance(body.get("detail"), dict):
        body = body["detail"]
    assert body.get("status"), f"{resp.status_code}: {resp.text[:300]}"
    return body


def test_a_tap_by_label_reaches_that_button_and_not_its_neighbour(taps) -> None:
    body = _tap(taps, label="Target")
    assert body.get("status") == "ok", body
    _reached(taps, "tap_target")


def test_a_tap_by_identifier_reaches_that_button_and_not_its_neighbour(taps) -> None:
    body = _tap(taps, identifier="tap_decoy")
    assert body.get("status") == "ok", body
    _reached(taps, "tap_decoy")


def test_a_label_two_buttons_share_is_ambiguous_and_taps_neither(taps) -> None:
    """Two buttons read "Twin". Picking one is a guess, so neither may count.

    Fails on Android until #512: the fast path taps the first match and says
    `ok`, so this reads twin_a 1.
    """
    body = _tap(taps, label="Twin")
    assert body.get("status") == "ambiguous", body
    _reached(taps, None)


def test_a_tap_after_a_remount_reaches_the_new_view(taps) -> None:
    """The button is replaced by a new one a column over and a row down.

    The screen is read first, so a tap from anything remembered about the old
    button aims at an empty spot and moves no counter.
    """
    # Read first, and check the button really moved: if arming did nothing,
    # a tap on the old view would pass this test too (CodeRabbit).
    before = _remount_elements(taps)
    assert len(before) == 1, before
    taps.tap("tap_remount_arm")
    time.sleep(2.0)
    after = _remount_elements(taps)
    assert len(after) == 1, after
    assert _at(after[0]) != _at(before[0]), (before, after)
    body = _tap(taps, label="Remount")
    assert body.get("status") == "ok", body
    _reached(taps, "tap_remount")


def test_a_tap_during_a_remount_is_honest_about_it(taps) -> None:
    """Sent while the button is gone: waiting for it and missing are both
    fine, and `ok` with nothing tapped is not."""
    taps.tap("tap_remount_arm")
    assert not _remount_elements(taps), "the button is still there: arming did nothing"
    body = _tap(taps, label="Remount")
    # Measured: Android's selector click waits for the new view (ok), and
    # iOS reads the screen once (not_found). Anything else is a wrong answer.
    assert body.get("status") in {"ok", "not_found"}, body
    _reached(taps, "tap_remount" if body.get("status") == "ok" else None)


def test_a_tap_on_a_covered_button_is_refused(taps) -> None:
    """The overlay covers the middle of the button, so a tap there is the
    overlay's. Quern should say so rather than tap it.

    Fails on Android until #521: the selector fast path presses the
    button's centre without asking what is on top, says `ok`, and the overlay
    counts it.
    """
    taps.tap("tap_overlay_arm")
    time.sleep(1.2)
    try:
        body = _tap(taps, label="Covered")
        assert body.get("status") == "obstructed", body
        _reached(taps, None)
    finally:
        taps.tap("tap_overlay_clear")
