"""Landmarks and screen identification, driven against QuernProbe.

Each platform's probe app carries its own knowledge base -- `contract.screens`
in `probe.py`, written to landmark conventions v2 -- and every test here loads
it under an app name no one else uses, so a developer's own loaded landmarks
are never replaced. They are unloaded afterwards.

One caveat about that isolation: `get_screen_summary?identify=true`, the
screen context on action responses and `tap_element`'s scrollability lookup
match against *every* loaded app, with no way to scope them. Against a server
that has other knowledge bases loaded, those tests can read `ambiguous` rather
than the probe's screen. That is quern answering correctly about what it was
given, and the failure message says which screens collided.
"""

from __future__ import annotations

import time
import uuid
from urllib.parse import urlparse

import pytest

LANDMARKS = "/api/v1/landmarks"


def _app(tag: str) -> str:
    return f"conformance.{tag}.{uuid.uuid4().hex[:10]}"


def _inline(screens: dict[str, dict], declared: int | None = 2) -> dict:
    if declared is None:
        return screens
    return {name: {**entry, "landmark_conventions": declared} for name, entry in screens.items()}


@pytest.fixture
def knowledge(quern):
    """Load landmark sets under throwaway app names; unload every one of them."""
    loaded: list[str] = []

    def _load(screens: dict, tag: str = "probe", declared: int | None = 2) -> tuple[str, dict]:
        app = _app(tag)
        body = quern.json_ok(
            "POST", f"{LANDMARKS}/load",
            json={"app": app, "landmarks": _inline(screens, declared)}, timeout=30.0,
        )
        loaded.append(app)
        return app, body

    yield _load
    for app in loaded:
        quern.delete(f"{LANDMARKS}/", params={"app": app}, timeout=30.0)


@pytest.fixture
def probe_kb(probe, knowledge):
    """The probe's own knowledge base, loaded. Returns (app, load response)."""
    return knowledge(probe.contract.screens)


def _identify(quern, probe, app: str) -> dict:
    return quern.json_ok(
        "POST", f"{LANDMARKS}/identify",
        json={"app": app, "udid": probe.udid}, timeout=90.0,
    )


def _unmatched(result: dict, screen: str) -> list:
    """The landmarks of `screen` that did not match, for a failure message."""
    for partial in result.get("partial_matches") or []:
        if partial.get("screen") == screen:
            return [lm["landmark"] for lm in partial.get("landmarks", []) if not lm.get("matched")]
    return []


# -- the knowledge base itself -------------------------------------------------


def test_the_fixture_knowledge_base_is_portable_and_collision_free(quern, probe, probe_kb) -> None:
    """Written to conventions v2, so every screen should audit as current.

    A failure here is either the knowledge base drifting from the conventions
    or the conventions check changing underneath it -- either is worth a look,
    because this file is the worked example the docs can point at.
    """
    app, body = probe_kb
    expected = len(probe.contract.screens)
    assert body["screens"] == expected, body
    assert body["skipped"] == [], body["skipped"]
    counts = body["conventions"]["counts"]
    assert counts == {"current": expected, "failing": 0, "behind": 0, "undeclared": 0}, (
        f"conventions audit: {body['conventions']}"
    )

    report = quern.json_ok("POST", f"{LANDMARKS}/validate", params={"app": app}, timeout=30.0)
    assert report["collisions"] == [], report["collisions"]
    assert report["no_landmarks"] == [], report["no_landmarks"]


# -- identification against the live app ---------------------------------------


def test_every_screen_is_identified_as_itself(quern, probe, probe_kb) -> None:
    """Visit every tab and ask quern where it is.

    One test rather than one per tab, so a failure lists every screen that is
    misidentified -- a landmark that is wrong usually breaks more than one.
    """
    app, _ = probe_kb
    probe.relaunch()
    wrong = {}
    for screen in probe.contract.screens:
        probe.goto(screen)
        result = _identify(quern, probe, app)
        if (result.get("matched"), result.get("confidence")) != (screen, "exact"):
            wrong[screen] = {
                "matched": result.get("matched"),
                "confidence": result.get("confidence"),
                "ambiguous_with": result.get("ambiguous_with"),
                "unmatched_landmarks": _unmatched(result, screen),
            }
    assert not wrong, f"misidentified screens on {probe.contract.platform}: {wrong}"


def test_screen_summary_names_the_screen(quern, probe, probe_kb) -> None:
    probe.goto("text")
    summary = quern.json_ok(
        "GET", "/api/v1/device/screen-summary",
        params={"udid": probe.udid, "identify": True}, timeout=90.0,
    )
    assert (summary.get("identified_as"), summary.get("confidence")) == ("text", "exact"), (
        f"identified_as={summary.get('identified_as')!r} "
        f"confidence={summary.get('confidence')!r}"
    )


def test_an_action_says_which_screen_it_landed_on(quern, probe, probe_kb) -> None:
    """The tap's own response carries the identification, no second read."""
    probe.goto("text")
    if probe.contract.platform == "ios":
        target = {"identifier": "tab_links"}
    else:
        target = {"label": "Links"}
    resp = quern.json_ok(
        "POST", "/api/v1/device/ui/tap-element",
        json={"udid": probe.udid, **target, "scroll_to_find": False,
              "include_screen_context": True},
        timeout=90.0,
    )
    context = resp.get("screen_context") or {}
    assert context.get("identified_as") == "links", (
        f"the tap landed on Links; its response says {context.get('identified_as')!r} "
        f"({context.get('confidence')!r}). On Android this is F25: the context is "
        "read before the pager has finished moving, so it describes a page in "
        "passing. A read a second later identifies Links correctly."
    )


def test_a_screen_nothing_matches_is_none_and_says_how_close_each_came(
    quern, probe, knowledge,
) -> None:
    """`partial_matches` is the debugging answer: every screen, best first,
    with each landmark's own result."""
    text_heading = probe.contract.screens["text"]["landmarks"][0]
    app, _ = knowledge({
        "nowhere": {"landmarks": [{"element": "Heading", "label": "Conformance Nowhere"}]},
        "half": {"landmarks": [
            text_heading,
            {"element": "Button", "identifier": "conformance_no_such_button"},
        ]},
    }, tag="nomatch")
    probe.goto("text")
    result = _identify(quern, probe, app)
    assert result["matched"] is None and result["confidence"] == "none", result
    partial = [(p["screen"], p["matched"], p["total"]) for p in result["partial_matches"]]
    assert partial == [("half", 1, 2), ("nowhere", 0, 1)], partial
    half = result["partial_matches"][0]["landmarks"]
    assert [lm["matched"] for lm in half] == [True, False], half


def test_two_screens_that_both_match_are_ambiguous_and_validate_says_why(
    quern, probe, knowledge,
) -> None:
    text = probe.contract.screens["text"]
    app, _ = knowledge({"first": text, "second": text}, tag="ambiguous")
    probe.goto("text")
    result = _identify(quern, probe, app)
    assert result["confidence"] == "ambiguous", result
    assert {result["matched"], *result.get("ambiguous_with", [])} == {"first", "second"}, result

    report = quern.json_ok("POST", f"{LANDMARKS}/validate", params={"app": app}, timeout=30.0)
    named = {name for c in report["collisions"] for name in c.get("screens", [])}
    assert {"first", "second"} <= named, f"validate missed the collision: {report['collisions']}"


# -- loading and unloading -----------------------------------------------------


def test_an_invalid_landmark_refuses_the_whole_load(quern) -> None:
    app = _app("invalid")
    resp = quern.post(f"{LANDMARKS}/load", json={"app": app, "landmarks": {
        "good": [{"element": "Heading", "label": "Fine"}],
        "bad": ["not an object"],
    }}, timeout=30.0)
    try:
        # 422 when the request schema refuses it, 400 when quern's own check
        # does; either is a refusal, and what matters is that it names the
        # screen and loads nothing.
        assert resp.status_code in (400, 422), f"{resp.status_code}: {resp.text[:300]}"
        assert "bad" in resp.text, f"the refusal does not name the screen: {resp.text[:300]}"
        sets = quern.json_ok("GET", f"{LANDMARKS}/", timeout=30.0)["sets"]
        assert app not in sets, f"a refused load left {sets.get(app)} screen(s) loaded"
    finally:
        quern.delete(f"{LANDMARKS}/", params={"app": app}, timeout=30.0)


def test_unload_is_scoped_to_its_app(quern, knowledge) -> None:
    keep, _ = knowledge({"k": {"landmarks": [{"element": "Heading", "label": "Keep"}]}}, tag="keep")
    drop, _ = knowledge({"d": {"landmarks": [{"element": "Heading", "label": "Drop"}]}}, tag="drop")
    quern.json_ok("DELETE", f"{LANDMARKS}/", params={"app": drop}, timeout=30.0)
    sets = quern.json_ok("GET", f"{LANDMARKS}/", timeout=30.0)["sets"]
    assert keep in sets and drop not in sets, sets


# -- a knowledge base on disk --------------------------------------------------


def _server_is_local(quern) -> bool:
    host = urlparse(quern.target.url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def _write_knowledge_base(root, screens: dict[str, dict]) -> None:
    """Screen documents as the knowledge-base guide describes them."""
    import yaml

    folder = root / "screens"
    folder.mkdir(parents=True)
    for name, entry in screens.items():
        front = {"screen": name, "landmark_conventions": 2, **entry}
        (folder / f"{name}.md").write_text(
            f"---\n{yaml.safe_dump(front, sort_keys=False)}---\n\n# {name}\n"
        )
    (folder / "legacy.md").write_text(
        "---\nscreen: legacy\nidentify_by:\n  - Heading 'Old'\n---\n"
    )
    (folder / "stub.md").write_text("---\nscreen: stub\n---\n\nNot documented yet.\n")


def test_a_knowledge_base_on_disk_loads_like_the_inline_one(
    quern, probe, knowledge, tmp_path,
) -> None:
    if not _server_is_local(quern):
        pytest.skip("loading by path reads the server's filesystem; this server is remote")
    _write_knowledge_base(tmp_path, probe.contract.screens)
    app = _app("path")
    body = quern.json_ok(
        "POST", f"{LANDMARKS}/load", json={"app": app, "source": str(tmp_path)}, timeout=30.0,
    )
    try:
        assert body["screens"] == len(probe.contract.screens), body
        reasons = {s.get("reason") for s in body["skipped"]}
        assert reasons == {"legacy_format", "no_landmarks"}, body["skipped"]
        legacy = next(s for s in body["skipped"] if s.get("reason") == "legacy_format")
        assert legacy.get("identify_by"), "the legacy entries are not echoed back for renaming"

        probe.goto("text")
        result = _identify(quern, probe, app)
        assert (result["matched"], result["confidence"]) == ("text", "exact"), result
    finally:
        quern.delete(f"{LANDMARKS}/", params={"app": app}, timeout=30.0)


def test_validating_a_directory_does_not_load_it(quern, probe, tmp_path) -> None:
    if not _server_is_local(quern):
        pytest.skip("validating by path reads the server's filesystem; this server is remote")
    _write_knowledge_base(tmp_path, probe.contract.screens)
    before = quern.json_ok("GET", f"{LANDMARKS}/", timeout=30.0)
    report = quern.json_ok(
        "POST", f"{LANDMARKS}/validate", params={"source": str(tmp_path)}, timeout=30.0,
    )
    assert report["total_screens"] == len(probe.contract.screens), report
    assert report["collisions"] == [], report["collisions"]
    after = quern.json_ok("GET", f"{LANDMARKS}/", timeout=30.0)
    assert after == before, "validating a path loaded it"


# -- scrollability ---------------------------------------------------------------


def _tap_missing(quern, probe) -> dict:
    """tap_element with scroll_to_find unset, for something that is not there."""
    resp = quern.post(
        "/api/v1/device/ui/tap-element",
        json={"udid": probe.udid, "label": f"conformance-absent-{uuid.uuid4().hex[:6]}"},
        timeout=120.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"
    return (resp.json().get("detail") or {}).get("scroll") or {}


def test_a_miss_on_a_screen_known_not_to_scroll_says_so(quern, probe, probe_kb) -> None:
    """`scrollable: false` lets a miss say the element is not on this screen,
    without the two swipes it would otherwise take to find that out.

    iOS only, as documented: Android's native-selector tap does not read the
    knowledge base (identifying the screen would cost the tree read that path
    exists to avoid), so there an unset `scroll_to_find` sweeps. The Android
    half asserts that contract instead, so a change to it is noticed.
    """
    probe.goto("text")
    started = time.monotonic()
    scroll = _tap_missing(quern, probe)
    if probe.contract.platform != "ios":
        assert scroll.get("attempted") is True, (
            f"{scroll} -- Android is documented to sweep when scroll_to_find is unset"
        )
        return
    assert scroll.get("reason") == "screen_not_scrollable", scroll
    assert scroll.get("screen") == "text", scroll
    assert scroll.get("attempted") is False, scroll
    assert time.monotonic() - started < 30, "a miss on a non-scrolling screen still swept"


def test_without_a_knowledge_base_the_same_miss_cannot_say(quern, probe) -> None:
    """The negative control for the iOS test above: with nothing loaded the
    answer must be "unknown", or that test proves nothing."""
    if probe.contract.platform != "ios":
        pytest.skip("Android does not read scrollable (documented); nothing to control for")
    sets = quern.json_ok("GET", f"{LANDMARKS}/", timeout=30.0)["sets"]
    if sets:
        pytest.skip(f"other landmarks are loaded on this server: {sorted(sets)}")
    probe.goto("text")
    scroll = _tap_missing(quern, probe)
    assert scroll.get("reason") == "scrollability_unknown", scroll


def test_a_screen_recorded_as_scrolling_is_swept_without_being_asked(
    quern, probe, probe_kb,
) -> None:
    """scroll_to_find unset, on a screen recorded `scrollable: true`: the row
    is off screen and the tap has to scroll to reach it."""
    probe.goto("scroll")
    probe.scroll_reset()
    target = probe.contract.row_locator(40)
    resp = quern.post(
        "/api/v1/device/ui/tap-element",
        json={"udid": probe.udid, **target}, timeout=180.0,
    )
    assert resp.status_code == 200, (
        f"row 40 was not reached on a screen recorded as scrolling: "
        f"{resp.status_code}: {resp.text[:400]}"
    )
