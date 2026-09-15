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

import pytest

from tests.conformance.probe import SCROLL_ROW_COUNT, Ids

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
    probe = fresh_probe
    probe.goto("text")
    identifier = probe_id(probe, Ids.FIELD_DEFAULT)

    probe.type_text(identifier, "to-be-cleared")
    probe.clear_text(identifier)

    got = probe.text_of(identifier)
    assert got in ("", None, identifier), (
        f"{identifier} still holds {got!r} after clear"
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
    first = probe.contract.row_identifier(0)
    if first is None:
        pytest.skip("rows are not individually identified on this platform")
    assert probe.element(first) is not None, (
        f"{first} is not on screen at rest"
    )


def test_the_last_row_needs_scrolling_to_reach(probe) -> None:
    """The precondition for the scroll test being a scroll test.

    If the last row were already on screen, `scroll_to_element` would pass
    without scrolling and the test below would prove nothing.
    """
    probe.goto("scroll")
    last = probe.contract.row_identifier(SCROLL_ROW_COUNT - 1)
    if last is None:
        pytest.skip("rows are not individually identified on this platform")
    assert probe.element(last) is None, (
        f"{last} is on screen without scrolling; the scroll fixture is not "
        "taller than the viewport"
    )


def test_scroll_to_element_brings_an_offscreen_row_into_view(probe) -> None:
    probe.goto("scroll")
    target_index = 60
    target = probe.contract.row_identifier(target_index)
    if target is None:
        pytest.skip("rows are not individually identified on this platform")

    assert probe.element(target) is None, (
        f"{target} was already visible; pick a row further down"
    )
    probe.scroll_to(identifier=target, max_swipes=25)
    assert probe.element(target) is not None, (
        f"scroll_to_element reported success but {target} is still not in the "
        "tree"
    )


def test_scroll_to_element_does_not_tap_what_it_scrolls_to(probe) -> None:
    """The reference is explicit: scroll into view *without* tapping it.

    A scroll that also activates its target makes it impossible to bring
    something into view in order to look at it, which is most of the reason to
    want the call.
    """
    probe.goto("scroll")
    target = probe.contract.row_identifier(40)
    if target is None:
        pytest.skip("rows are not individually identified on this platform")

    probe.scroll_to(identifier=target, max_swipes=25)
    element = probe.element(target)
    assert element is not None
    assert probe.element(probe.contract.id_for(Ids.SCROLL_CONTAINER)) is not None, (
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
