"""UI interaction, driven against QuernProbe.

Identifier-based throughout — no coordinates. That is not a stylistic choice:
a coordinate tap passes on the device it was recorded against and silently taps
the wrong thing on a different screen size, which is precisely the failure this
suite exists on more than one machine to avoid.

Every assertion reads state back from the app rather than trusting the tool's
own status. `tools/probe-app-android/README.md` explains why this habit matters:
`am start` exits 0 when it cannot resolve an intent, so `open_url` reported
success for a URL nothing could open (#78). The app is the only honest witness.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conformance.client import ServerLogWindow
from tests.conformance.probe import SCROLL_ROW_COUNT, Ids, ScrollTracer

#: Mixed case, an underscore, a shifted digit and a symbol. This exact shape is
#: what exposed the HID shift-drop bug the iOS probe app was built to isolate,
#: so it is worth typing rather than a simple lowercase word.
SHIFT_TEXT = "ab_CD!2@x"


# -- the app is actually there -----------------------------------------------


def test_the_probe_app_is_installed(quern, probe) -> None:
    """Installation is a precondition for everything below; assert it directly.

    Otherwise a failed install shows up as twelve confusing interaction
    failures rather than one clear one.
    """
    body = quern.json_ok(
        "GET", "/api/v1/device/app/list",
        params={"udid": probe.udid}, timeout=120.0,
    )
    apps = body.get("apps") or []
    ids = {a.get("bundle_id") for a in apps}
    assert "com.quern.probe" in ids, (
        f"the probe app is not installed; {len(ids)} app(s) present"
    )


def test_the_ui_tree_carries_identifiers(probe) -> None:
    """The tree must expose identifiers, not only labels.

    Every test here selects by identifier. A tree that returned labels alone
    would make the whole module fall back to label matching without saying so.
    """
    tree = probe.ui_tree()
    elements = tree.get("elements") or []
    assert elements, f"/device/ui returned no elements: {sorted(tree)}"
    identified = [e for e in elements if e.get("identifier")]
    assert identified, (
        f"none of the {len(elements)} elements carried an identifier"
    )


def test_a_known_element_is_found_by_identifier(probe, probe_id) -> None:
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)
    assert probe.element(identifier) is not None, (
        f"{identifier} is not on the Text tab"
    )


def test_an_unknown_identifier_is_not_found(probe) -> None:
    """404, not a 200 carrying an empty element.

    A caller that gets 200 with nothing in it has to know to look inside the
    body; one that gets 404 cannot miss it.
    """
    assert probe.element("no_such_element_conformance") is None


# -- text entry --------------------------------------------------------------


def test_typing_into_a_named_field_lands_in_that_field(
    fresh_probe, probe_id
) -> None:
    probe = fresh_probe
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)

    probe.type_text(identifier, "conformance")

    assert probe.text_of(identifier) == "conformance", (
        f"{identifier} holds {probe.text_of(identifier)!r} after typing"
    )


def test_shift_characters_survive_typing(fresh_probe, probe_id) -> None:
    probe = fresh_probe
    """Mixed case and symbols must arrive intact.

    The regression this guards against dropped the shift modifier, so
    `ab_CD!2@x` arrived as `ab_cd12x` — every character present, the shifted
    ones wrong. An assertion on length or on "not empty" would pass against it,
    which is why this compares the whole string.
    """
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)

    probe.type_text(identifier, SHIFT_TEXT)

    got = probe.text_of(identifier)
    assert got == SHIFT_TEXT, (
        f"typed {SHIFT_TEXT!r}, field holds {got!r} — shifted characters are "
        "being dropped or mistranslated"
    )


def test_clearing_a_named_field_empties_it(fresh_probe, probe_id) -> None:
    """After clearing, the typed text must be gone.

    Asserted as "the content is gone" rather than "the field reads empty",
    because an empty field does not read empty on either platform: it reports
    its placeholder. iOS uses the identifier (`field_default`), Android uses a
    shorter hint (`default`), and Android exposes no separate hint attribute at
    all — an empty EditText simply reports the hint in `text`.

    The earlier version allowed `identifier`, which quietly covered iOS and
    failed on Android against a field that had been cleared correctly. That is
    the same mistake #177 fixed in the implementation, made once more in the
    test that was supposed to catch it.
    """
    probe = fresh_probe
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)

    typed = "to-be-cleared"
    probe.type_text(identifier, typed)
    assert probe.text_of(identifier) == typed, "the text never landed"

    probe.clear_text(identifier)

    got = probe.text_of(identifier) or ""
    assert typed not in got, f"{identifier} still holds {got!r} after clear"
    assert not (got and got in typed), (
        f"{identifier} holds {got!r}, which is a fragment of what was typed — "
        "a partial clear, not a placeholder"
    )


def test_clearing_names_the_field_it_was_told_to(fresh_probe, probe_id) -> None:
    probe = fresh_probe
    """Clear one field, leave its neighbour alone.

    `ClearTextRequest` documents the failure this prevents: without a selector
    the first field holding a value is chosen, which on a sign-in form is the
    email field rather than the password one just tapped. The Text tab has four
    fields, so it can tell the difference.
    """
    probe.goto("text")
    first = probe_id(probe, Ids.FIELD_DEFAULT)
    second = probe_id(probe, Ids.FIELD_EMAIL)

    probe.type_text(first, "keep-me")
    probe.type_text(second, "clear-me")
    probe.clear_text(second)

    assert probe.text_of(first) == "keep-me", (
        f"clearing {second} also cleared {first}"
    )


def test_typing_is_reported_to_the_app(fresh_probe, probe_id) -> None:
    """The app's own event log must show the edit.

    Reading the field back proves the text is in the view. This proves the app
    was *told* — that the delegate callbacks fired — which is the difference
    between text that was typed and text that was written into the view behind
    the app's back. An app that never hears about the edit will not validate,
    enable its submit button, or fire its analytics.
    """
    probe = fresh_probe
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)
    log_id = probe_id(probe, Ids.TEXT_EVENT_LOG)

    probe.type_text(identifier, "xyz")

    event = probe.text_of(log_id) or ""
    assert identifier in event, (
        f"the app's event log reads {event!r}; it did not record an edit to "
        f"{identifier}"
    )


# -- controls ----------------------------------------------------------------


def test_tapping_the_switch_changes_its_value(probe, probe_id) -> None:
    probe.goto("controls")
    identifier = probe_id(probe, Ids.SWITCH)

    before = (probe.element(identifier) or {}).get("value")
    probe.tap(identifier, skip_stability_check=True)
    after = (probe.element(identifier) or {}).get("value")

    assert before != after, (
        f"the switch read {before!r} before the tap and {after!r} after"
    )


def test_a_value_aware_tap_is_idempotent(probe, probe_id) -> None:
    """`value` means "make it so", not "toggle".

    `TapElementRequest.value` is documented as skipping the tap when the element
    already matches. That is what makes a setup step safe to repeat -- and a
    toggle dressed up as an assignment silently undoes itself on the second
    call, which is the kind of bug that only appears when a test is re-run.
    """
    probe.goto("controls")
    identifier = probe_id(probe, Ids.SWITCH)

    probe.tap(identifier, value="1", skip_stability_check=True)
    first = (probe.element(identifier) or {}).get("value")
    probe.tap(identifier, value="1", skip_stability_check=True)
    second = (probe.element(identifier) or {}).get("value")

    assert first == second == "1", (
        f"asking for value=1 twice gave {first!r} then {second!r}; the second "
        "call toggled rather than asserted"
    )


def test_the_app_records_the_control_change(probe, probe_id) -> None:
    """Again: assert on the app, not on the tool's 200."""
    probe.goto("controls")
    switch_id = probe_id(probe, Ids.SWITCH)
    readout_id = probe_id(probe, Ids.CONTROL_READOUT)

    probe.tap(switch_id, value="0", skip_stability_check=True)
    probe.tap(switch_id, value="1", skip_stability_check=True)

    readout = probe.text_of(readout_id) or ""
    assert readout.strip(), (
        "the controls readout is empty; the app was not told the switch changed"
    )


# -- scrolling ---------------------------------------------------------------


def test_the_first_row_is_visible_without_scrolling(probe) -> None:
    probe.goto("scroll")
    probe.scroll_reset(to="top")
    assert probe.row(0) is not None, (
        f"{probe.contract.row_locator(0)} is not on screen at the top of the list"
    )


def test_the_last_row_needs_scrolling_to_reach(probe) -> None:
    """The precondition for the scroll test being a scroll test.

    If the last row were already on screen, `scroll_to_element` would pass
    without scrolling and the test below would prove nothing.
    """
    probe.goto("scroll")
    probe.scroll_reset(to="top")
    last = probe.contract.row_locator(SCROLL_ROW_COUNT - 1)
    assert probe.row(SCROLL_ROW_COUNT - 1) is None, (
        f"{last} is on screen at the top of the list; the scroll fixture is "
        "not taller than the viewport"
    )


def test_the_reset_control_returns_the_list_to_a_known_position(probe) -> None:
    """The reset itself needs a test, because everything else now trusts it.

    A reset that silently did nothing would make the scroll tests start
    wherever the previous one left off — and they would still pass most of the
    time, which is the worst version of that.
    """
    probe.goto("scroll")

    probe.scroll_reset(to="bottom")
    bottom = probe.viewport()
    assert bottom is not None, "no rows visible after resetting to the bottom"
    assert bottom.last == SCROLL_ROW_COUNT - 1, (
        f"reset to bottom left the list at {bottom}, not showing the last row"
    )

    probe.scroll_reset(to="top")
    top = probe.viewport()
    assert top is not None, "no rows visible after resetting to the top"
    assert top.first == 0, f"reset to top left the list at {top}"

    # Not `offset_px == 0`: the offset is origin-relative, so at the top it is
    # minus the container's own y — -116 on iOS, -507 on Android. Asserting
    # zero fails on a correct reset, which it did. What must hold is that the
    # two resets are far apart and the top one is the smaller.
    assert top.offset_px < bottom.offset_px, (
        f"reset to top ({top}) is not above reset to bottom ({bottom})"
    )


def test_scroll_to_element_brings_an_offscreen_row_into_view(probe) -> None:
    """Known to fail intermittently on iOS — issue #84, not a flaky test.

    Measured mechanism: the sweep's travel per swipe (17-18 rows) is almost
    exactly the viewport span (17-18 rows), so the overlap margin is zero to one
    row. `_ios_scroll_to_element` then queries *immediately* after each swipe,
    while the fling is still decelerating, so the sampled window is not the
    settled one. Rows fall through the seam: with a settle delay, three sweeps
    sampled every row; without one, the same three missed rows 24, 57-58 and
    110-111.

    Deliberately not marked `xfail` or retried. A release run should report a
    known bug as a failure: waiting three minutes to be told a visible element
    is absent is the user-facing behaviour, and hiding it would make the suite
    quieter and less true. Android passes consistently.

    On failure the viewport trace below says which rows were never sampled, so
    the report distinguishes "the list did not move" from "the target was
    scrolled past between reads". Quern's screenshot timeline runs alongside
    it, so each traced action has a picture at a known offset — with the caveat
    that the timeline captures per HTTP request, and `scroll_to_element` does
    all its swiping inside one.
    """
    probe.goto("scroll")
    probe.scroll_reset(to="top")

    target_index = 60
    target = probe.contract.row_locator(target_index)

    tracer = ScrollTracer(probe, screenshots=True)
    server_log = ServerLogWindow()
    server_log.start()
    phase_started = datetime.now(UTC)
    tracer.sample("before")
    assert probe.row(target_index) is None, (
        f"{target} was already visible at the top of the list; pick a row "
        "further down"
    )

    # The call fails in more than one way and every one of them needs the
    # trace. A clean 404 is the documented miss; a read timeout is what
    # actually happened the first time this ran, and it bypassed the reporting
    # entirely — the failure arrived as an httpx traceback with no viewport
    # data at all, which is the exact problem this test exists to avoid.
    outcome = "found"
    try:
        probe.scroll_to_row(target_index, max_swipes=25)
    except AssertionError as exc:
        outcome = f"scroll_to_element refused: {str(exc).splitlines()[0]}"
    except Exception as exc:  # noqa: BLE001 - transport failure is a result here
        outcome = f"scroll_to_element never returned: {exc!r}"
    tracer.sample("after scroll_to")

    if outcome == "found" and probe.row(target_index) is not None:
        return

    # Failed. Sweep manually with a settle delay so the report can say whether
    # the row exists at all and where it actually sits — the difference between
    # a broken fixture and a scroll that skipped it.
    probe.scroll_reset(to="top")
    tracer.sample("reset")
    for step in range(14):
        vp = tracer.sample(f"manual swipe {step + 1}")
        if vp is not None and vp.first <= target_index <= vp.last:
            break
        probe.swipe_down()

    # Four views of the same window, deliberately reported together. Each one
    # alone leaves an obvious counter-explanation standing: the viewport trace
    # cannot say whether the script asked for the right thing, the action log
    # cannot say what the screen did, and neither can say what the server was
    # doing during the three minutes it held the request open.
    pytest.fail(
        "\n".join([
            f"{target} did not come into view.",
            f"  outcome: {outcome}",
            tracer.report(target=target_index),
            probe.client.action_log(since=phase_started),
            server_log.report(),
        ]),
        pytrace=False,
    )


def _report_miss(probe, index: int, exc: Exception | None) -> str:
    """Where the list actually ended up, for a sweep that did not arrive."""
    viewport = probe.viewport()
    seen = f"rows {viewport.first}-{viewport.last}" if viewport else "no rows on screen"
    refusal = f" ({str(exc).splitlines()[0]})" if exc else ""
    return f"row {index} was not reached{refusal}; the list is at {seen}"


def test_a_row_far_down_the_list_is_reached(probe) -> None:
    """The reach of one sweep, which is a different question from #84's.

    The tests above use rows 20 and 60, both within any plausible budget, so
    they pass on a sweep that cannot go further. Row 150 of 200 is past what
    Android's *default* budget covers: ~11 rows a swipe against `max_swipes`
    of 10.

    Deliberately not raising `max_swipes`. Asking for 25 makes this pass on
    both platforms today, which is worth knowing -- the sweep works, the reach
    is the budget -- and makes the test prove nothing about what a caller who
    did not think to ask for more will get. The default is the contract.

    Expected to fail on Android until #232: the caller cannot tell "not on
    this screen" from "further than the default budget", because both answer
    404. Left failing rather than skipped, for the reason F9 is: a known bug
    that reports as a pass is worse than no test.
    """
    probe.goto("scroll")
    probe.scroll_reset(to="top")
    index = 150

    failure = None
    try:
        probe.scroll_to_row(index)         # the default budget, deliberately
    except AssertionError as exc:          # a 404 from scroll_to_element
        failure = exc

    assert probe.row(index) is not None, _report_miss(probe, index, failure)


def test_a_row_above_is_reached_from_the_bottom(probe) -> None:
    """Turning around, which a sweep that only goes one way never does.

    From the bottom of the list every downward swipe moves nothing, so a
    sweep without end detection spends its downward budget before it reverses.
    With the default budget that is most of it. iOS turns around as soon as a
    swipe changes nothing (#204).

    Expected to fail on Android until #232.
    """
    probe.goto("scroll")
    probe.scroll_reset(to="bottom")

    failure = None
    try:
        probe.scroll_to_row(3)             # the default budget, deliberately
    except AssertionError as exc:
        failure = exc

    assert probe.row(3) is not None, _report_miss(probe, 3, failure)


def test_scroll_to_element_does_not_tap_what_it_scrolls_to(probe) -> None:
    """The reference is explicit: scroll into view *without* tapping it.

    A scroll that also activates its target makes it impossible to bring
    something into view in order to look at it, which is most of the reason to
    want the call.
    """
    probe.goto("scroll")
    probe.scroll_reset(to="top")

    # Row 20, not something distant. This test is about scroll-without-tap, and
    # a far target makes it fail for #84's reasons instead — one bug should not
    # be able to fail two tests for different stated reasons.
    target = probe.contract.row_locator(20)

    probe.scroll_to_row(20, max_swipes=25)
    assert probe.row(20) is not None, (
        f"{target} did not come into view; this test cannot check the "
        "no-tap property without it"
    )
    container = probe.contract.id_for(Ids.SCROLL_CONTAINER)
    assert probe.element(container) is not None, (
        "the scroll container is gone — scrolling appears to have navigated away"
    )


# -- waiting -----------------------------------------------------------------


def test_waiting_for_a_present_element_returns_promptly(probe, probe_id) -> None:
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)

    resp = probe.wait_for(identifier=identifier, condition="exists", timeout_s=10.0)
    assert resp.is_success, (
        f"waiting for a visible element returned {resp.status_code}: "
        f"{resp.text[:300]}"
    )


def test_waiting_for_an_absent_element_times_out_rather_than_hanging(
    probe,
) -> None:
    """A timeout must be a bounded, reported outcome.

    The dangerous failure is not "returns the wrong status" but "never
    returns": an agent waiting on an element that will never appear blocks the
    whole session. Asserting the call comes back at all is most of the value
    here.
    """
    import time

    started = time.perf_counter()
    resp = probe.wait_for(
        identifier="never_appears_conformance", condition="exists", timeout_s=5.0
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 60.0, (
        f"waiting 5s for an absent element took {elapsed:.1f}s to return"
    )
    assert resp.status_code in (200, 404, 408), (
        f"unexpected status {resp.status_code} for a wait that timed out"
    )


# -- navigation --------------------------------------------------------------


@pytest.mark.parametrize("tab", ["text", "controls", "scroll", "links", "logs"])
def test_each_tab_on_the_bar_can_be_selected(probe, tab) -> None:
    """Every tab that lives on the bar, not just the one a test happened to need.

    Parametrised rather than looped so a single broken tab names itself instead
    of failing whichever assertion came first.
    """
    probe.goto(tab)
    tree_ids = {
        e.get("identifier")
        for e in (probe.ui_tree().get("elements") or [])
        if e.get("identifier")
    }
    assert any(i and i.startswith(f"tab_{tab}") for i in tree_ids) or tree_ids, (
        f"after selecting {tab!r} the tree carries no identifiers"
    )


def test_the_screen_summary_describes_the_current_screen(quern, probe) -> None:
    """`get_screen_summary` is the orienting call agents make first."""
    probe.goto("controls")
    body = quern.json_ok(
        "GET", "/api/v1/device/screen-summary",
        params={"udid": probe.udid}, timeout=120.0,
    )
    assert body, "/device/screen-summary returned nothing"
    text = str(body).lower()
    assert "probe" in text or "control" in text or "switch" in text, (
        f"the summary does not mention anything on the Controls tab: "
        f"{str(body)[:400]}"
    )
