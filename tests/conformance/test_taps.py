"""Which element a tap reached: the probe's Taps tab (#511).

A tap check elsewhere usually means `tap_element` said `ok` and the next read
looked plausible. A tap on the wrong element can look the same: both return
`ok`, and on a screen where nothing visibly changes, both read the same
afterwards. Every button on the Taps tab counts its own taps into a label,
`<identifier>_count`, so these tests read which button the tap reached and
assert that nothing else moved.

Not covered here: the slider that never stops jumping (`tap_slide_arm`). A tap
on it reaches a stale position only some of the time -- measured at about one
run in seven on an Android emulator and two in three on a simulator, each an
`ok` that reached nothing -- so there is no deterministic assertion to make
until quern checks that a target is still before tapping it (#514).
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


def _tap(probe, **locator) -> dict:
    return probe.client.json_ok(
        "POST", "/api/v1/device/ui/tap-element",
        json={"udid": probe.udid, **locator, **probe.NO_SCROLL, **probe.READ},
        timeout=90.0,
    )


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


def test_a_tap_reaches_the_button_on_top_not_the_one_beneath(taps) -> None:
    """The slider, at rest, lies over the behind button."""
    body = _tap(taps, label="Slider")
    assert body.get("status") == "ok", body
    _reached(taps, "tap_slider")


def test_a_tap_after_a_remount_reaches_the_new_view(taps) -> None:
    """Removed and added back 1.5 s later, lower down: the same label, a new view."""
    taps.tap("tap_remount_arm")
    time.sleep(2.0)
    body = _tap(taps, label="Remount")
    assert body.get("status") == "ok", body
    _reached(taps, "tap_remount")


def test_a_tap_after_a_late_overlay_reaches_the_overlay(taps) -> None:
    """The overlay arrives 500 ms after arming, over the covered button."""
    taps.tap("tap_overlay_arm")
    time.sleep(1.2)
    body = _tap(taps, label="Overlay")
    assert body.get("status") == "ok", body
    _reached(taps, "tap_overlay")
    taps.tap("tap_overlay_clear")
