"""#491: tap_element checks that a documented navigation tap arrived.

The check must neither cry wolf nor stay silent. Most tests here are about the
first: every case where it cannot be sure must make no claim, and a tap no
edge describes must cost nothing at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.knowledge import navigation
from server.knowledge.landmarks import LandmarkRegistry, scan_knowledge_base
from server.knowledge.navigation import (
    Identified,
    Plan,
    could_match_call,
    link_target,
    matches_tapped,
    parse_action,
    parse_leads_to,
    plan_for,
    previous_tap_note,
    verify,
    warning_for,
)
from server.main import create_app
from server.models import Landmark, NavEdge, ScreenLandmarks, UIElement

# ---------------------------------------------------------------------------
# Parsing the knowledge base
# ---------------------------------------------------------------------------


class TestParseAction:
    def test_template_form(self):
        assert parse_action('tap_element label="More" element_type="Button"') == {
            "label": "More", "element_type": "Button",
        }

    def test_call_form(self):
        assert parse_action('tap_element(identifier="tab_more", element_type="Button")') == {
            "identifier": "tab_more", "element_type": "Button",
        }

    def test_single_quotes_and_mcp_prefix(self):
        assert parse_action("mcp__quern-debug__tap_element label='Done'") == {"label": "Done"}

    def test_a_label_with_spaces(self):
        assert parse_action('tap_element label="Sign In"') == {"label": "Sign In"}

    def test_non_selector_arguments_are_dropped(self):
        assert parse_action('tap_element label="Wi-Fi" snapshot_depth=20 value="1"') == {
            "label": "Wi-Fi",
        }

    def test_another_tool_is_not_an_edge(self):
        assert parse_action('swipe direction="left"') is None
        assert parse_action('open_url url="app://x"') is None

    def test_prose_is_not_an_edge(self):
        assert parse_action("tap the back button") is None

    def test_a_tap_without_label_or_identifier_is_not_an_edge(self):
        # A type alone matches every button on the screen: nothing to check.
        assert parse_action('tap_element element_type="Button"') is None

    def test_not_a_string(self):
        assert parse_action(None) is None
        assert parse_action({"label": "x"}) is None

    def test_a_longer_tool_name_is_not_tap_element(self):
        assert parse_action('tap_element_at x=1 y=2 label="x"') is None


class TestLinkTarget:
    @pytest.mark.parametrize("raw, name", [
        ("[[screens/more]]", "more"),
        ("[[screens/more|More]]", "more"),
        ("screens/more.md", "more"),
        ("more", "more"),
        ("  [[screens/text]]  ", "text"),
    ])
    def test_forms(self, raw, name):
        assert link_target(raw) == name

    @pytest.mark.parametrize("raw", [None, "", "[[screens/...]]", 3])
    def test_unusable(self, raw):
        assert link_target(raw) is None


class TestParseLeadsTo:
    def test_edges_and_unusable_entries(self):
        edges, unusable = parse_leads_to([
            {"screen": "[[screens/more]]", "action": 'tap_element label="More"'},
            {"screen": "[[screens/x]]", "action": "swipe left"},
            {"screen": "", "action": 'tap_element label="X"'},
            "not a mapping",
            {"screen": "[[screens/y]]", "action": 'tap_element label="Y"',
             "condition": "when signed in"},
        ])
        assert [(e.target, e.label, e.condition) for e in edges] == [
            ("more", "More", None), ("y", "Y", "when signed in"),
        ]
        assert [u["reason"] for u in unusable] == [
            "the action is not a tap_element call with a label or identifier",
            "names no screen",
            "not a mapping",
        ]

    def test_absent_is_nothing(self):
        assert parse_leads_to(None) == ([], [])

    def test_an_edge_timeout(self):
        edges, unusable = parse_leads_to([
            {"screen": "a", "action": 'tap_element label="A"', "timeout": 8},
            {"screen": "b", "action": 'tap_element label="B"', "timeout": "slow"},
            {"screen": "c", "action": 'tap_element label="C"', "timeout": True},
        ])
        assert [e.timeout for e in edges] == [8.0, None, None]
        assert len(unusable) == 2 and "default" in unusable[0]["reason"]

    def test_not_a_list_is_reported(self):
        edges, unusable = parse_leads_to({"screen": "x"})
        assert edges == [] and unusable[0]["reason"] == "leads_to is not a list"


def _screen_file(path, name, landmarks, leads_to=""):
    path.write_text(
        f'---\nscreen: "{name}"\nlandmarks:\n{landmarks}\n{leads_to}---\n# {name}\n'
    )


class TestScan:
    def test_links_resolve_to_screen_names(self, tmp_path):
        screens = tmp_path / "screens"
        screens.mkdir()
        # The file is more.md; the screen is called "More list".
        _screen_file(screens / "more.md", "More list",
                     '  - { element: "Heading", label: "More" }')
        _screen_file(
            screens / "text.md", "Text",
            '  - { element: "Heading", label: "Text" }',
            'leads_to:\n'
            '  - screen: "[[screens/more]]"\n'
            "    action: 'tap_element label=\"More\"'\n"
            '  - screen: "[[screens/gone]]"\n'
            "    action: 'tap_element label=\"Gone\"'\n",
        )
        scan = scan_knowledge_base(tmp_path)
        text = next(s for s in scan.screens if s.screen == "Text")
        assert [e.target for e in text.leads_to] == ["More list"]
        assert scan.edge_issues == [{
            "file": "screens/text.md", "screen": "Text", "entry": {"screen": "gone"},
            "reason": "names no loaded screen",
        }]

    def test_the_load_reports_edges(self, tmp_path):
        screens = tmp_path / "screens"
        screens.mkdir()
        _screen_file(screens / "a.md", "a", '  - { element: "Heading", label: "A" }',
                     "leads_to:\n  - screen: \"[[screens/a]]\"\n    action: 'swipe'\n")
        registry = LandmarkRegistry()
        registry.load_from_path("app", str(tmp_path))
        report = registry.navigation_report("app")
        assert report["edges"] == 0
        assert report["unusable"][0]["file"] == "screens/a.md"
        assert "not checked" in report["note"]


# ---------------------------------------------------------------------------
# Matching a tap to an edge
# ---------------------------------------------------------------------------


def _edge(target="more", **sel) -> NavEdge:
    return NavEdge(target=target, **sel)


class TestCouldMatchCall:
    def test_same_label(self):
        assert could_match_call(_edge(label="More"), label="more")

    def test_same_identifier(self):
        assert could_match_call(_edge(identifier="tab_more"), identifier="tab_more")

    def test_different_identifier(self):
        assert not could_match_call(
            _edge(identifier="tab_more", label="More"), identifier="tab_text", label="More")

    def test_type_equivalence_across_backends(self):
        # A tab item: RadioButton on the accessibility tree, Button through WDA.
        assert could_match_call(
            _edge(label="More", element_type="RadioButton"),
            label="More", element_type="Button")

    def test_an_unrelated_type(self):
        assert not could_match_call(
            _edge(label="More", element_type="TextField"), label="More", element_type="Switch")

    def test_unrelated_label(self):
        assert not could_match_call(_edge(label="More"), label="Settings")

    @pytest.mark.parametrize("call, edge", [
        ("OK", "Book"), ("Back", "Feedback"), ("On", "Location"), ("Log", "Logs"),
    ])
    def test_exact_labels_must_be_equal(self, call, edge):
        # tap_element matches labels exactly, so neither can tap the other.
        assert not could_match_call(_edge(label=edge), label=call)

    def test_a_substring_call_against_a_longer_label(self):
        assert could_match_call(_edge(label="Order #1234"), label_contains="Order")
        assert could_match_call(_edge(label="Order #1234"), label_prefix="order #")
        assert not could_match_call(_edge(label="Order #1234"), label_prefix="#1234")

    def test_an_exact_call_against_a_widened_edge(self):
        assert could_match_call(_edge(label_prefix="Order #"), label="Order #77")
        assert could_match_call(_edge(label_contains="Order"), label="My Order")
        assert not could_match_call(_edge(label_prefix="Order #"), label="Re: Order #77")

    def test_identifier_call_against_a_label_edge_is_not_checked(self):
        assert not could_match_call(_edge(label="More"), identifier="tab_more")


class TestMatchesTapped:
    def test_exact_label(self):
        assert matches_tapped(_edge(label="More"), {"label": "More", "type": "Button"})

    def test_a_near_miss_does_not_match(self):
        # The loose pre-check accepts this; the tapped element settles it.
        assert could_match_call(_edge(label="More"), label_contains="More")
        assert not matches_tapped(_edge(label="More"), {"label": "More Info", "type": "Button"})

    def test_type_equivalence(self):
        assert matches_tapped(
            _edge(label="More", element_type="RadioButton"),
            {"label": "More", "type": "Button"})

    def test_an_incompatible_type_does_not_match(self):
        assert not matches_tapped(
            _edge(label="More", element_type="TextField"), {"label": "More", "type": "Button"})

    def test_identifier(self):
        assert matches_tapped(
            _edge(identifier="tab_more"),
            {"label": "More", "type": "Button", "identifier": "tab_more"})
        assert not matches_tapped(
            _edge(identifier="tab_more"),
            {"label": "More", "type": "Button", "identifier": "tab_text"})


# ---------------------------------------------------------------------------
# Planning and verifying
# ---------------------------------------------------------------------------


def _exact(screen, app="app"):
    return Identified("exact", screen=screen, app=app)


class TestPlanFor:
    edges = {("app", "text"): [_edge(target="more", label="More")]}

    def _edges_from(self, app, screen):
        return self.edges.get((app, screen), [])

    def test_a_documented_tap(self):
        plan = plan_for(_exact("text"), self._edges_from, {"label": "More"})
        assert plan is not None and [e.target for e in plan.edges] == ["more"]

    @pytest.mark.parametrize("before", [
        Identified("none"),
        Identified("ambiguous", candidates=["text", "links"]),
    ])
    def test_no_claim_from_an_uncertain_start(self, before):
        assert plan_for(before, self._edges_from, {"label": "More"}) is None

    def test_an_edge_from_another_screen_is_not_used(self):
        assert plan_for(_exact("links"), self._edges_from, {"label": "More"}) is None

    def test_a_tap_that_is_not_an_edge(self):
        assert plan_for(_exact("text"), self._edges_from, {"label": "Settings"}) is None


class _Clock:
    """A clock that advances only when slept on, so timeouts cost no time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def _reads(*results):
    """An identify that returns each result in turn, repeating the last."""
    seq = list(results)
    calls = {"n": 0}

    async def identify():
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        r = seq[i]
        if isinstance(r, Exception):
            raise r
        return r

    identify.calls = calls
    return identify


TAPPED = {"label": "More", "type": "Button"}


async def _verify(plan, *reads, tapped=TAPPED, timeout=1.0):
    clock = _Clock()
    identify = _reads(*reads)
    result = await verify(plan, tapped, identify, timeout=timeout,
                          clock=clock, sleep=clock.sleep)
    return result, identify.calls["n"]


def _plan(*edges, before="text"):
    return Plan(before=_exact(before), edges=list(edges or [_edge(label="More")]))


class TestVerify:
    async def test_arrived_on_the_first_read(self):
        nav, reads = await _verify(_plan(), _exact("more"))
        assert nav["arrived"] is True and nav["expected"] == "more"
        assert nav["from"] == "text" and reads == 1
        assert warning_for(nav) is None

    async def test_waits_out_a_transition(self):
        # The old screen, then neither, then the new one: a push animating.
        nav, reads = await _verify(
            _plan(), _exact("text"), Identified("none"), _exact("more"))
        assert nav["arrived"] is True and reads == 3

    async def test_still_on_the_starting_screen(self):
        nav, _ = await _verify(_plan(), _exact("text"))
        assert nav["arrived"] is False and nav["on"] == "text" and nav["stayed"] is True
        assert "still 'text'" in warning_for(nav)

    async def test_on_another_screen(self):
        nav, _ = await _verify(_plan(), _exact("links"))
        assert nav["arrived"] is False and nav["on"] == "links" and nav["stayed"] is False
        assert "on 'links' instead" in warning_for(nav)

    async def test_ends_unidentified_is_no_claim(self):
        nav, _ = await _verify(_plan(), Identified("none"))
        assert nav["arrived"] is None and nav["reason"] == "unidentified"
        assert warning_for(nav) is None

    async def test_ends_ambiguous_is_no_claim(self):
        nav, _ = await _verify(_plan(), Identified("ambiguous", candidates=["more", "x"]))
        assert nav["arrived"] is None and nav["reason"] == "ambiguous"
        assert nav["candidates"] == ["more", "x"]

    async def test_the_target_in_another_app_is_not_arrival(self):
        nav, _ = await _verify(_plan(), _exact("more", app="other"))
        assert nav["arrived"] is None

    async def test_every_read_failing_is_no_claim(self):
        nav, reads = await _verify(_plan(), OSError("gone"))
        assert nav["arrived"] is None and nav["reason"] == "read_failed"
        assert nav["read_failures"] == reads > 1

    async def test_a_failed_read_then_arrival(self):
        nav, _ = await _verify(_plan(), OSError("blip"), _exact("more"))
        assert nav["arrived"] is True

    async def test_a_conditional_edge_never_reports_a_miss(self):
        plan = _plan(_edge(label="More", condition="when signed in"))
        nav, _ = await _verify(plan, _exact("text"))
        assert nav["arrived"] is None and nav["reason"] == "conditional"
        assert nav["conditional"] is True and warning_for(nav) is None

    async def test_a_conditional_edge_confirms_arrival(self):
        plan = _plan(_edge(label="More", condition="when signed in"))
        nav, _ = await _verify(plan, _exact("more"))
        assert nav["arrived"] is True

    async def test_a_firm_edge_beside_a_conditional_one_can_miss(self):
        plan = _plan(_edge(target="more", label="More"),
                     _edge(target="login", label="More", condition="signed out"))
        nav, _ = await _verify(plan, _exact("text"))
        assert nav["arrived"] is False and nav["expected"] == ["login", "more"]

    async def test_either_target_is_arrival(self):
        plan = _plan(_edge(target="more", label="More"), _edge(target="login", label="More"))
        nav, _ = await _verify(plan, _exact("login"))
        assert nav["arrived"] is True and nav["on"] == "login"

    async def test_a_tapped_element_that_is_not_the_edges(self):
        nav, reads = await _verify(_plan(), _exact("more"),
                                   tapped={"label": "More Info", "type": "Button"})
        assert nav is None and reads == 0

    async def _slow(self, reads, read_s, timeout=3.0):
        """Reads that each take `read_s` -- a sim-bridge read is 1.6-3.9s."""
        clock = _Clock()
        seq = list(reads)
        calls = {"n": 0}

        async def identify():
            i = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            clock.now += read_s
            r = seq[i]
            if isinstance(r, Exception):
                raise r
            return r

        nav = await verify(_plan(), TAPPED, identify, timeout=timeout,
                           clock=clock, sleep=clock.sleep)
        return nav, calls["n"]

    async def test_a_slow_read_begun_with_the_tap_cannot_claim_a_miss(self):
        # The first read begins as the tap lands and runs past the deadline,
        # showing the old screen; the app had navigated meanwhile.
        nav, reads = await self._slow([_exact("text"), _exact("more")], read_s=3.5)
        assert nav["arrived"] is True and reads == 2

    async def test_a_slow_backend_still_reports_a_real_miss(self):
        nav, reads = await self._slow([_exact("text")], read_s=3.5)
        assert nav["arrived"] is False and reads == 2

    async def test_late_reads_failing_is_no_claim(self):
        nav, _ = await self._slow([_exact("text"), OSError("gone")], read_s=3.5)
        assert nav["arrived"] is None and nav["reason"] == "no_settled_read"
        assert warning_for(nav) is None

    async def test_the_wait_is_capped(self):
        nav, reads = await self._slow([_exact("text"), OSError("gone")], read_s=3.5)
        assert nav["waited_ms"] <= (navigation.HARD_CAP + 3.5 + 0.2) * 1000

    async def test_an_edge_timeout_extends_the_wait(self):
        clock = _Clock()
        seen = {"n": 0}

        async def identify():
            seen["n"] += 1
            # The next screen appears 4s in: a sign-in waiting on the network.
            return _exact("more") if clock.now >= 4.0 else _exact("text")

        plan = _plan(_edge(label="More", timeout=6.0))
        nav = await verify(plan, TAPPED, identify, clock=clock, sleep=clock.sleep)
        assert nav["arrived"] is True

    async def test_without_one_a_slow_screen_is_a_miss_that_says_not_to_repeat(self):
        nav, _ = await _verify(_plan(), _exact("text"), timeout=3.0)
        assert nav["arrived"] is False and nav["timeout_s"] == 3.0
        warning = warning_for(nav)
        assert "tap again" not in warning
        assert "not for a submit" in warning and "timeout:" in warning

    async def test_a_call_timeout_overrides_the_edge(self):
        nav, _ = await _verify(_plan(_edge(label="More", timeout=9.0)), _exact("text"),
                               timeout=1.5)
        assert nav["timeout_s"] == 1.5

    async def test_the_full_wait_is_paid_only_by_a_miss(self):
        nav, _ = await _verify(_plan(), _exact("text"), timeout=1.0)
        assert nav["waited_ms"] >= 1000


# ---------------------------------------------------------------------------
# Through the route
# ---------------------------------------------------------------------------


TEXT_TREE = [
    UIElement(type="Heading", label="Text"),
    UIElement(type="RadioButton", label="Text", identifier="tab_text", value="1"),
    UIElement(type="RadioButton", label="More", identifier="tab_more", value="0"),
]
MORE_TREE = [
    UIElement(type="Heading", label="More"),
    UIElement(type="RadioButton", label="Text", identifier="tab_text", value="0"),
    UIElement(type="RadioButton", label="More", identifier="tab_more", value="1"),
]


def _registry_screens(edges_from_text=True):
    return [
        ScreenLandmarks(
            screen="text",
            landmarks=[Landmark(element="Heading", label="Text")],
            leads_to=[NavEdge(target="more", label="More", element_type="RadioButton")]
            if edges_from_text else [],
        ),
        ScreenLandmarks(screen="more", landmarks=[Landmark(element="Heading", label="More")]),
    ]


@pytest.fixture
def app():
    return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                      enable_crash=False, enable_proxy=False)


@pytest.fixture
def controller(app):
    ctrl = DeviceController()
    ctrl._active_udid = "AAAA-1111"
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl.tap_element = AsyncMock(return_value={
        "status": "ok", "tapped": {"label": "More", "type": "RadioButton",
                                   "identifier": "tab_more", "x": 1, "y": 2},
    })
    ctrl.get_ui_elements = AsyncMock(return_value=(TEXT_TREE, "AAAA-1111"))
    app.state.device_controller = ctrl
    return ctrl


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(navigation, "NAVIGATION_TIMEOUT", 0.3)
    monkeypatch.setattr(navigation, "_POLL_GAP", 0.01)


async def _tap(app, body, *, opt_in=True):
    """The per-tap check is opt-in; these route tests ask for it unless told
    not to. TestPreviousTap covers the default."""
    if opt_in:
        body = {"verify_navigation": True, **body}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/device/ui/tap-element", json=body,
                                 headers={"Authorization": "Bearer k"})
    return resp.status_code, resp.json()


class TestRoute:
    async def test_arrived(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.get_ui_elements.side_effect = [
            (TEXT_TREE, "AAAA-1111"), (MORE_TREE, "AAAA-1111"),
        ]
        code, body = await _tap(app, {"label": "More"})
        assert code == 200 and body["status"] == "ok"
        assert body["navigation"]["arrived"] is True
        assert body["navigation"]["from"] == "text" and "warning" not in body
        # The before read may come from the cache; the after read must not.
        assert controller.get_ui_elements.call_args_list[0].kwargs["use_cache"] is True
        assert controller.get_ui_elements.call_args_list[1].kwargs["use_cache"] is False

    async def test_a_dropped_tap_is_reported(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        code, body = await _tap(app, {"label": "More"})
        assert code == 200 and body["status"] == "ok"
        assert body["navigation"]["arrived"] is False
        assert body["navigation"]["on"] == "text"
        assert "the app dropped the tap" in body["warning"]

    async def test_a_miss_keeps_the_device_hub_advisory(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.input_warning = lambda udid: "Device Hub has this simulator's input."
        code, body = await _tap(app, {"label": "More"})
        assert "the app dropped the tap" in body["warning"]
        assert "Device Hub has this simulator's input." in body["warning"]

    async def test_an_unrelated_exact_label_costs_no_read(self, app, controller):
        screens = _registry_screens()
        screens[0].leads_to = [NavEdge(target="more", label="Book")]
        app.state.landmark_registry.load("probe", screens)
        await _tap(app, {"label": "OK"})
        controller.get_ui_elements.assert_not_called()

    async def test_an_incompatible_type_costs_no_read(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More", "element_type": "TextField"})
        controller.get_ui_elements.assert_not_called()

    async def test_a_disconnect_during_the_read_before_stops_the_tap(
        self, app, controller, monkeypatch,
    ):
        from fastapi import HTTPException

        from server.api import device_ui

        app.state.landmark_registry.load("probe", _registry_screens())
        real = device_ui._run_until_client_leaves

        async def gone(request, coro, *, what, poll_s=2.0):
            if what == "tap_element navigation check":
                coro.close()
                raise HTTPException(status_code=499, detail="client disconnected")
            return await real(request, coro, what=what, poll_s=poll_s)

        monkeypatch.setattr(device_ui, "_run_until_client_leaves", gone)
        code, _ = await _tap(app, {"label": "More"})
        assert code == 499
        controller.tap_element.assert_not_called()

    async def test_a_disconnect_during_the_check_stops_its_reads(
        self, app, controller, monkeypatch,
    ):
        """Through the real guard: the read before the tap goes ahead, the
        caller leaves, and no read is made after the tap. A route that called
        the check without the guard would read on for the whole wait."""
        from fastapi import HTTPException

        from server.api import device_ui

        app.state.landmark_registry.load("probe", _registry_screens())
        real = device_ui._run_until_client_leaves
        guarded = {"n": 0}

        async def leaves_after_the_tap(request, coro, *, what, poll_s=2.0):
            if what == "tap_element navigation check":
                guarded["n"] += 1
                if guarded["n"] > 1:   # the check after the tap
                    coro.close()
                    raise HTTPException(status_code=499, detail="client disconnected")
            return await real(request, coro, what=what, poll_s=poll_s)

        monkeypatch.setattr(device_ui, "_run_until_client_leaves", leaves_after_the_tap)
        code, _ = await _tap(app, {"label": "More", "navigation_timeout": 0.5})
        assert code == 499
        controller.tap_element.assert_awaited_once()
        # The read before the tap, and none after it.
        assert controller.get_ui_elements.call_count == 1

    async def test_an_exception_inside_the_check_does_not_fail_the_tap(
        self, app, controller, monkeypatch,
    ):
        app.state.landmark_registry.load("probe", _registry_screens())

        async def boom(*a, **k):
            raise RuntimeError("bug in the check")

        monkeypatch.setattr(navigation, "verify", boom)
        code, body = await _tap(app, {"label": "More"})
        assert code == 200 and body["status"] == "ok" and "navigation" not in body

    async def test_ok_without_a_tapped_element_is_not_checked(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.tap_element.return_value = {"status": "ok"}
        await _tap(app, {"label": "More"})
        assert controller.get_ui_elements.call_count == 1

    async def test_a_tapped_element_without_ok_is_not_checked(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.tap_element.return_value = {
            "status": "already_set", "tapped": {"label": "More", "type": "RadioButton"}}
        await _tap(app, {"label": "More"})
        assert controller.get_ui_elements.call_count == 1

    async def test_navigation_timeout_reaches_the_check(self, app, controller, monkeypatch):
        app.state.landmark_registry.load("probe", _registry_screens())
        seen = {}
        real = navigation.verify

        async def spy(*a, **k):
            seen["timeout"] = k.get("timeout")
            return await real(*a, **k)

        monkeypatch.setattr(navigation, "verify", spy)
        await _tap(app, {"label": "More", "navigation_timeout": 0.2})
        assert seen["timeout"] == 0.2

    async def test_no_knowledge_base_costs_no_read(self, app, controller):
        code, body = await _tap(app, {"label": "More"})
        assert code == 200 and "navigation" not in body
        controller.get_ui_elements.assert_not_called()

    async def test_a_tap_no_edge_describes_costs_no_read(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        code, body = await _tap(app, {"label": "Settings"})
        assert "navigation" not in body
        controller.get_ui_elements.assert_not_called()

    async def test_opted_out(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        code, body = await _tap(app, {"label": "More", "verify_navigation": False})
        assert "navigation" not in body
        controller.get_ui_elements.assert_not_called()

    async def test_a_long_press_is_not_checked(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        code, body = await _tap(app, {"label": "More", "duration": 1.0})
        assert "navigation" not in body
        controller.get_ui_elements.assert_not_called()

    async def test_an_unidentified_start_makes_no_claim(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.get_ui_elements.return_value = ([UIElement(type="Heading", label="?")],
                                                   "AAAA-1111")
        code, body = await _tap(app, {"label": "More"})
        assert "navigation" not in body
        # One read: the start was identified, and nothing after.
        assert controller.get_ui_elements.call_count == 1

    @pytest.mark.parametrize("result, code", [
        ({"status": "ambiguous", "matches": []}, 200),
        ({"status": "already_set", "value": "1"}, 200),
    ])
    async def test_no_claim_for_a_tap_that_did_not_happen(self, app, controller, result, code):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.tap_element.return_value = result
        status, body = await _tap(app, {"label": "More"})
        assert status == code and "navigation" not in body
        assert controller.get_ui_elements.call_count == 1

    async def test_an_obstructed_tap_makes_no_claim(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.tap_element.return_value = {"status": "obstructed", "reason": "covered"}
        status, body = await _tap(app, {"label": "More"})
        assert status == 409 and "navigation" not in body["detail"]
        assert controller.get_ui_elements.call_count == 1

    async def test_a_failed_before_read_does_not_fail_the_tap(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.get_ui_elements.side_effect = OSError("WDA went away")
        code, body = await _tap(app, {"label": "More"})
        assert code == 200 and body["status"] == "ok" and "navigation" not in body

    async def test_another_screen_with_the_same_edge_name_elsewhere(self, app, controller):
        # The edge is documented from "more" only; tapping from "text" is not checked.
        screens = _registry_screens(edges_from_text=False)
        screens[1].leads_to = [NavEdge(target="text", label="More")]
        app.state.landmark_registry.load("probe", screens)
        code, body = await _tap(app, {"label": "More"})
        assert "navigation" not in body
        assert controller.get_ui_elements.call_count == 1


class TestIdentifiedApp:
    async def test_identify_for_context_names_the_app(self):
        registry = LandmarkRegistry()
        registry.load("one", [ScreenLandmarks(
            screen="Home", landmarks=[Landmark(element="Heading", label="Welcome")])])
        registry.load("two", [ScreenLandmarks(
            screen="Home", landmarks=[Landmark(element="Heading", label="Dashboard")])])

        async def no_urls():
            return None

        found = await registry.identify_for_context(
            [UIElement(type="Heading", label="Dashboard")], no_urls)
        assert found == {"identified_as": "Home", "confidence": "exact",
                         "identified_app": "two"}


class TestRegistry:
    def _screen(self, name, heading, leads_to=()):
        return ScreenLandmarks(screen=name, landmarks=[Landmark(element="Heading", label=heading)],
                               leads_to=list(leads_to))

    def test_identify_exact_names_the_app_whose_screen_matched(self):
        registry = LandmarkRegistry()
        registry.load("one", [self._screen("Home", "Welcome")])
        registry.load("two", [self._screen("Home", "Dashboard")])
        found = registry.identify_exact([UIElement(type="Heading", label="Dashboard")])
        assert (found.confidence, found.screen, found.app) == ("exact", "Home", "two")

    def test_unload_drops_edge_issues(self):
        registry = LandmarkRegistry()
        registry.load("a", [self._screen("x", "X")], edge_issues=[{"reason": "r"}])
        registry.unload("a")
        assert registry.navigation_report("a") == {"edges": 0}

    def test_a_reload_replaces_edge_issues(self):
        registry = LandmarkRegistry()
        registry.load("a", [self._screen("x", "X")], edge_issues=[{"reason": "old"}])
        registry.load("a", [self._screen("x", "X")], edge_issues=[{"reason": "new"}])
        assert registry.navigation_report("a")["unusable"] == [{"reason": "new"}]

    def test_an_edge_to_a_screen_with_no_landmarks_is_not_counted(self):
        from server.knowledge.landmarks import resolve_targets

        screens = [
            ScreenLandmarks(screen="a", landmarks=[Landmark(element="Heading", label="A")],
                            leads_to=[NavEdge(target="stub", label="Go")]),
            ScreenLandmarks(screen="stub", landmarks=[]),
        ]
        issues = resolve_targets(screens)
        assert screens[0].leads_to == []
        assert "no landmarks" in issues[0]["reason"]


class TestTimeoutReport:
    def test_a_bad_timeout_is_not_reported_as_unchecked(self, tmp_path):
        screens = tmp_path / "screens"
        screens.mkdir()
        _screen_file(screens / "b.md", "b", '  - { element: "Heading", label: "B" }')
        _screen_file(
            screens / "a.md", "a", '  - { element: "Heading", label: "A" }',
            'leads_to:\n  - screen: "[[screens/b]]"\n'
            "    action: 'tap_element label=\"Go\"'\n    timeout: slow\n",
        )
        registry = LandmarkRegistry()
        registry.load_from_path("app", str(tmp_path))
        report = registry.navigation_report("app")
        assert report["edges"] == 1
        assert "unusable" not in report and "note" not in report
        assert "default" in report["timeouts_ignored"][0]["reason"]
        assert "edge_kept" not in report["timeouts_ignored"][0]


class TestPreviousTap:
    """The default: no read at the tap, and the next miss says whether a
    documented tap before it seems not to have registered."""

    NOT_FOUND = {
        "status": "not_found", "detail": "No element found matching 'Toggle'",
        "screen_context": {"identified_as": "text", "confidence": "exact"},
    }

    @pytest.fixture(autouse=True)
    def no_grace(self, monkeypatch):
        """These misses come straight after the tap; the grace before a tap
        can be blamed is tested on its own below."""
        monkeypatch.setattr(navigation, "MISS_MIN_AGE", 0.0)

    async def _miss(self, app, controller, screen_context=None):
        result = dict(self.NOT_FOUND)
        if screen_context is not None:
            result["screen_context"] = screen_context
        controller.tap_element.return_value = result
        return await _tap(app, {"label": "Toggle"}, opt_in=False)

    async def test_the_default_costs_no_read(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        code, body = await _tap(app, {"label": "More"}, opt_in=False)
        assert code == 200 and "navigation" not in body
        controller.get_ui_elements.assert_not_called()

    async def test_a_miss_on_the_screen_the_tap_should_have_left(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        code, body = await self._miss(app, controller)
        assert code == 404
        prev = body["detail"]["previous_tap"]
        assert prev["expected"] == "more" and prev["on"] == "text"
        assert prev["tapped"]["label"] == "More"
        assert "probably did not register" in prev["note"]
        assert "not blindly for a submit" in prev["note"]
        controller.get_ui_elements.assert_not_called()

    async def test_a_toggle_that_arrived_is_not_blamed(self, app, controller):
        # "More" leads text -> more, and the same "More" leads more -> text.
        screens = _registry_screens()
        screens[1].leads_to = [NavEdge(target="text", label="More", element_type="RadioButton")]
        app.state.landmark_registry.load("probe", screens)
        await _tap(app, {"label": "More"}, opt_in=False)   # from text, arrives on more
        _, body = await self._miss(app, controller,
                                   {"identified_as": "more", "confidence": "exact"})
        assert "previous_tap" not in body["detail"]

    async def test_the_identified_app_decides_whose_edges_count(self, app, controller):
        # Both apps have a "text"; the miss identified probe's.
        app.state.landmark_registry.load("probe", _registry_screens())
        app.state.landmark_registry.load("other", [
            ScreenLandmarks(screen="hub", landmarks=[Landmark(element="Heading", label="Hub")],
                            leads_to=[NavEdge(target="text", label="More")]),
            ScreenLandmarks(screen="text", landmarks=[Landmark(element="Heading", label="T2")]),
        ])
        await _tap(app, {"label": "More"}, opt_in=False)
        _, body = await self._miss(app, controller, {
            "identified_as": "text", "confidence": "exact", "identified_app": "probe"})
        assert body["detail"]["previous_tap"]["expected"] == "more"
        controller.tap_element.return_value = {   # _miss replaced the tap result
            "status": "ok", "tapped": {"label": "More", "type": "RadioButton",
                                       "identifier": "tab_more", "x": 1, "y": 2}}
        await _tap(app, {"label": "More"}, opt_in=False)
        _, body = await self._miss(app, controller, {
            "identified_as": "text", "confidence": "exact", "identified_app": "other"})
        assert "previous_tap" not in body["detail"]

    async def test_another_apps_edges_do_not_silence_it(self, app, controller):
        # A second app whose own "More" leads to its own screen named "text".
        app.state.landmark_registry.load("probe", _registry_screens())
        app.state.landmark_registry.load("other", [
            ScreenLandmarks(screen="hub", landmarks=[Landmark(element="Heading", label="Hub")],
                            leads_to=[NavEdge(target="text", label="More")]),
            ScreenLandmarks(screen="text", landmarks=[Landmark(element="Heading", label="T2")]),
        ])
        await _tap(app, {"label": "More"}, opt_in=False)
        _, body = await self._miss(app, controller, {
            "identified_as": "text", "confidence": "exact", "identified_app": "probe"})
        assert body["detail"]["previous_tap"]["expected"] == "more"

    async def test_it_is_said_once(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        await self._miss(app, controller)
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    @pytest.mark.parametrize("context", [
        {"identified_as": "more", "confidence": "exact"},       # it arrived
        {"identified_as": "links", "confidence": "exact"},      # not a screen it leaves
        {"identified_as": None, "confidence": "none"},
        {"identified_as": "text", "confidence": "ambiguous", "candidates": ["text", "x"]},
        {},
    ])
    async def test_no_note_without_an_exact_screen_it_leaves(self, app, controller, context):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        _, body = await self._miss(app, controller, context)
        assert "previous_tap" not in body["detail"]

    async def test_an_undocumented_tap_in_between_clears_it(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        controller.tap_element.return_value = {
            "status": "ok", "tapped": {"label": "Bold", "type": "Button"}}
        await _tap(app, {"label": "Bold"}, opt_in=False)
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_a_conditional_edge_is_not_remembered(self, app, controller):
        screens = _registry_screens()
        screens[0].leads_to = [NavEdge(target="more", label="More", condition="signed in")]
        app.state.landmark_registry.load("probe", screens)
        await _tap(app, {"label": "More"}, opt_in=False)
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_too_long_ago(self, app, controller, monkeypatch):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        monkeypatch.setattr(navigation, "PREVIOUS_TAP_TTL", -1.0)
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_per_device(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        controller.resolve_udid.return_value = "BBBB-2222"
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_a_long_press_is_not_remembered(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More", "duration": 1.0}, opt_in=False)
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def _wait(self, app, controller, matched):
        controller.wait_for_element = AsyncMock(return_value=(
            {"matched": matched, "screen_context": self.NOT_FOUND["screen_context"]},
            "AAAA-1111"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/device/ui/wait-for-element",
                                     json={"identifier": "toggle", "condition": "exists",
                                           "timeout": 1},
                                     headers={"Authorization": "Bearer k"})
        return resp.json()

    async def test_a_wait_that_times_out_carries_it(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        body = await self._wait(app, controller, matched=False)
        assert body["previous_tap"]["expected"] == "more"

    async def test_a_wait_that_matched_does_not_clear_it(self, app, controller):
        # It may have matched something of the screen the tap should have
        # left: a spinner gone, a field enabled.
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        await self._wait(app, controller, matched=True)
        _, body = await self._miss(app, controller)
        assert body["detail"]["previous_tap"]["expected"] == "more"

    async def test_any_screen_change_since_silences_it(self, app, controller):
        # A back tap by coordinates, a swipe, a button: they all stamp it.
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        controller._last_ui_change["AAAA-1111"] = 12345.0
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_a_verified_tap_is_not_remembered(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        controller.get_ui_elements.side_effect = [
            (TEXT_TREE, "AAAA-1111"), (MORE_TREE, "AAAA-1111")]
        _, body = await _tap(app, {"label": "More"})   # verify_navigation: true
        assert body["navigation"]["arrived"] is True
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_a_name_shared_with_another_app_says_nothing(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        app.state.landmark_registry.load("other", [
            ScreenLandmarks(screen="text", landmarks=[Landmark(element="Heading", label="T2")])])
        await _tap(app, {"label": "More"}, opt_in=False)
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_an_arrival_through_a_conditional_edge_is_not_blamed(self, app, controller):
        screens = _registry_screens()
        screens.append(ScreenLandmarks(
            screen="hub", landmarks=[Landmark(element="Heading", label="Hub")],
            leads_to=[NavEdge(target="text", label="More", condition="signed in")]))
        app.state.landmark_registry.load("probe", screens)
        await _tap(app, {"label": "More"}, opt_in=False)   # from hub, lands on text
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_a_miss_with_no_identification_leaves_it(self, app, controller):
        # A stale web element: not_found with no screen context.
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        controller.tap_element.return_value = {
            "status": "not_found", "reason": "stale_web_content", "message": "gone"}
        code, body = await _tap(app, {"label": "Toggle"}, opt_in=False)
        assert code == 404 and "previous_tap" not in body["detail"]
        _, body = await self._miss(app, controller)
        assert body["detail"]["previous_tap"]["expected"] == "more"

    async def test_a_result_that_tapped_nothing_keeps_it(self, app, controller):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)
        controller.tap_element.return_value = {"status": "ambiguous", "matches": []}
        await _tap(app, {"label": "Row"}, opt_in=False)
        _, body = await self._miss(app, controller)
        assert body["detail"]["previous_tap"]["expected"] == "more"

    async def test_a_failing_lookup_does_not_fail_the_tap(self, app, controller, monkeypatch):
        app.state.landmark_registry.load("probe", _registry_screens())

        def boom(tapped):
            raise RuntimeError("bad knowledge base")

        monkeypatch.setattr(app.state.landmark_registry, "edges_for_tapped", boom)
        code, body = await _tap(app, {"label": "More"}, opt_in=False)
        assert code == 200 and body["status"] == "ok"
        _, body = await self._miss(app, controller)
        assert "previous_tap" not in body["detail"]

    async def test_a_failing_note_does_not_fail_the_miss(self, app, controller, monkeypatch):
        app.state.landmark_registry.load("probe", _registry_screens())
        await _tap(app, {"label": "More"}, opt_in=False)

        def boom(*a, **k):
            raise RuntimeError("bug")

        monkeypatch.setattr(navigation, "previous_tap_note", boom)
        code, body = await self._miss(app, controller)
        assert code == 404 and body["detail"]["status"] == "not_found"


class TestPreviousTapNote:
    """`previous_tap_note` itself, with the clock in hand."""

    def _tap(self, *edges, at=100.0, stamp=7.0):
        return navigation.DocumentedTap(
            tapped={"label": "Next", "type": "Button"},
            edges=list(edges), at=at, stamp=stamp)

    CTX = {"identified_as": "a", "confidence": "exact"}

    def test_the_real_ttl(self):
        tap = self._tap(("app", "a", _edge(target="b", label="Next")))
        assert previous_tap_note(tap, self.CTX, 100.0 + 119, stamp=7.0) is not None
        assert previous_tap_note(tap, self.CTX, 100.0 + 121, stamp=7.0) is None

    def test_the_grace_before_blame(self):
        tap = self._tap(("app", "a", _edge(target="b", label="Next")))
        assert previous_tap_note(tap, self.CTX, 100.5, stamp=7.0) is None
        assert previous_tap_note(tap, self.CTX, 101.5, stamp=7.0) is not None

    def test_an_edge_timeout_extends_the_grace(self):
        tap = self._tap(("app", "a", _edge(target="b", label="Next", timeout=30.0)))
        assert previous_tap_note(tap, self.CTX, 110.0, stamp=7.0) is None
        assert previous_tap_note(tap, self.CTX, 131.0, stamp=7.0) is not None

    def test_a_changed_screen(self):
        tap = self._tap(("app", "a", _edge(target="b", label="Next")))
        assert previous_tap_note(tap, self.CTX, 105.0, stamp=8.0) is None

    def test_expected_is_only_where_it_leads_from_here(self):
        # "Next" leads a -> b and b -> c; the miss is on a.
        tap = self._tap(("app", "a", _edge(target="b", label="Next")),
                        ("app", "b", _edge(target="c", label="Next")))
        note = previous_tap_note(tap, self.CTX, 105.0, stamp=7.0)
        assert note["expected"] == "b"

    def test_the_note_names_the_tap_and_its_age(self):
        tap = self._tap(("app", "a", _edge(target="b", label="Next")))
        note = previous_tap_note(tap, self.CTX, 104.26, stamp=7.0)
        assert "'Next'" in note["note"] and note["seconds_ago"] == 4.3
        assert note["tapped"] == {"label": "Next", "identifier": None, "type": "Button"}


class TestInlineLoad:
    async def test_leads_to_inline(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/landmarks/load", json={
                "app": "probe",
                "landmarks": {
                    "text": {"landmarks": [{"element": "Heading", "label": "Text"}],
                             "leads_to": [
                                 {"screen": "[[screens/more]]",
                                  "action": 'tap_element label="More"'},
                                 {"screen": "nowhere", "action": 'tap_element label="X"'},
                             ]},
                    "more": {"landmarks": [{"element": "Heading", "label": "More"}]},
                },
            }, headers={"Authorization": "Bearer k"})
        body = resp.json()
        assert body["navigation"]["edges"] == 1
        assert body["navigation"]["unusable"][0]["reason"] == "names no loaded screen"
        edges = app.state.landmark_registry.edges_from("probe", "text")
        assert [(e.target, e.label) for e in edges] == [("more", "More")]


# ---------------------------------------------------------------------------
# The probe apps' knowledge bases
# ---------------------------------------------------------------------------


class TestProbeKnowledgeBases:
    """The fixtures the live tests load. Their landmarks are the conformance
    contract's, kept in step here rather than by editing `probe.py`."""

    @pytest.mark.parametrize("platform, path", [
        ("ios", "tools/probe-app/knowledge"),
        ("android", "tools/probe-app-android/knowledge"),
    ])
    def test_loads_whole_and_matches_the_contract(self, platform, path):
        from pathlib import Path

        from tests.conformance.probe import CONTRACTS

        root = Path(__file__).resolve().parent.parent / path
        scan = scan_knowledge_base(root)
        assert scan.skipped == [] and scan.edge_issues == []
        contract = CONTRACTS[platform].screens
        for screen in scan.screens:
            assert screen.leads_to, f"{screen.screen} documents no navigation"
            if screen.screen in contract:
                expected = [Landmark(**lm) for lm in contract[screen.screen]["landmarks"]]
                assert screen.landmarks == expected, screen.screen
        assert set(contract) <= {s.screen for s in scan.screens}
        # The tabs are declared once, in app.md (#503), not on every screen.
        assert any(e.from_global for s in scan.screens for e in s.leads_to)
