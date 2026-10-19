"""A WDA lookup that misses at the shallow action depth looks once more, deeper (#470).

Reads made for an action use snapshot depth 12 (F35): deep enough for a long
list's rows, and nine times faster than the full walk there. iOS 26's Settings
root nests its rows at 20, so `tap_element(identifier="com.apple.settings.wifi")`
and `get_element` answered not-found for an element on screen. Now a miss at
the action depth queries once more at the full depth.

The review of the first version found the guards this file pins:
- only after a *quick* shallow look -- a dense screen is the one to leave alone;
- a query, never /source, with its own timeout and never re-sent;
- a hit changes what is reported, never the depth of the reads after it;
- per request, so two requests on one device cannot read each other's result.

The fake WDA answers by a session-wide depth, which is how WDA behaves:
`snapshotMaxDepth` governs queries as well as /source.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.device import controller_ui
from server.device.controller import DeviceController
from server.device.ios.wda_client import ACTION_SNAPSHOT_DEPTH, SNAPSHOT_MAX_DEPTH
from server.models import DeviceError, DeviceType, WaitCondition

PHONE = "PHYS-0001"
WIFI = "com.apple.settings.wifi"
WIFI_ROW = {
    "type": "Cell", "AXUniqueId": WIFI, "AXLabel": "Wi-Fi",
    "frame": {"x": 0, "y": 200, "width": 390, "height": 44},
}
SHALLOW_ROW = {
    "type": "Button", "AXUniqueId": "shallow_btn", "AXLabel": "Edit",
    "frame": {"x": 300, "y": 50, "width": 60, "height": 44},
}


class FakeWda:
    """A session-wide depth, as WDA has it. The Wi-Fi row exists only from depth
    20; `shallow_btn` at any depth. `delay` slows every query."""

    def __init__(self):
        self.depth: int | None = None
        self.depths_set: list[int] = []
        self.queries: list[dict] = []
        self.delay = 0.0
        self.deep_delay = 0.0
        #: Bounded deep queries whose using-strategy times out, e.g. {"predicate string"}.
        self.deep_timeouts: set[str] = set()

    async def set_depth(self, udid, depth):
        self.depth = depth
        self.depths_set.append(depth)

    async def query(self, udid, using, value, **kw):
        self.queries.append({"depth": self.depth, "value": value, **kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.deep_delay and self.depth == SNAPSHOT_MAX_DEPTH:
            await asyncio.sleep(self.deep_delay)
        if (self.depth == SNAPSHOT_MAX_DEPTH and kw.get("no_resend")
                and using in self.deep_timeouts):
            from server.device.ios.wda_client import QueryTimedOut
            raise QueryTimedOut(value)
        if "shallow_btn" in value:
            return [dict(SHALLOW_ROW)]
        if WIFI in value and self.depth is not None and self.depth >= 20:
            return [dict(WIFI_ROW)]
        return []

    def deep_queries(self):
        return [q for q in self.queries if q["depth"] == SNAPSHOT_MAX_DEPTH]


@pytest.fixture
def phone(monkeypatch):
    # A timed-out wait attaches a screenshot of the screen it gave up on.
    monkeypatch.setattr(
        "server.device.controller_ui._capture_screenshot", AsyncMock(return_value=None),
    )
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    wda = FakeWda()
    ctrl.wda_client._set_snapshot_depth = wda.set_depth
    ctrl.wda_client.find_elements_by_query = wda.query
    # The /source fallback: nothing at the shallow depth either.
    ctrl.wda_client.describe_all = AsyncMock(return_value=[])
    ctrl.wda_client.tap = AsyncMock()
    # On-screen marking asks the device for its viewport; not under test here.
    ctrl._mark_on_screen = AsyncMock()
    # A wait that times out describes the screen it gave up on.
    ctrl._identify_for_miss = AsyncMock(return_value={})
    # Every match is reachable; hit-testing is #435's, not this file's.
    ctrl._wda_reachable_only = AsyncMock(side_effect=lambda r, m: (m, []))
    return ctrl, wda


def _no_deep_source(ctrl):
    for call in ctrl.wda_client.describe_all.call_args_list:
        assert call.kwargs.get("snapshot_depth") != SNAPSHOT_MAX_DEPTH, (
            "a full /source read ran at the full depth -- the read that times out "
            "on a long list and restarts the runner")


class TestTheLookup:
    async def test_a_miss_at_the_action_depth_is_found_deeper(self, phone):
        ctrl, wda = phone
        controller_ui._begin_lookup()
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier=WIFI,
            snapshot_depth=ACTION_SNAPSHOT_DEPTH, deepen_on_miss=True,
        )
        assert [e.identifier for e in elements] == [WIFI]
        assert controller_ui._DEEPENED.get() == SNAPSHOT_MAX_DEPTH

    async def test_the_deep_query_is_bounded_and_never_re_sent(self, phone):
        ctrl, wda = phone
        await ctrl.get_ui_elements(
            PHONE, filter_identifier=WIFI, snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        deep = wda.deep_queries()
        assert deep, "no deeper query ran"
        assert all(q.get("no_resend") is True for q in deep)
        assert all(q.get("timeout") == controller_ui.DEEP_QUERY_TIMEOUT for q in deep)
        _no_deep_source(ctrl)

    async def test_a_dense_screen_is_not_queried_deeper(self, phone, monkeypatch):
        """A slow shallow query means a long list; a deep walk there is slower
        still (34.5s on F35's table) and the first version fired it anyway."""
        ctrl, wda = phone
        monkeypatch.setattr(controller_ui, "DEEPEN_MAX_SHALLOW_SECONDS", 0.05)
        wda.delay = 0.08
        controller_ui._begin_lookup()
        await ctrl.get_ui_elements(
            PHONE, filter_identifier=WIFI, snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        assert wda.deep_queries() == []
        assert controller_ui._DEEPEN_TRIED.get() is False

    async def test_a_deep_miss_puts_the_shallow_depth_back(self, phone):
        ctrl, wda = phone
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier="absent", snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        assert elements == []
        assert wda.depths_set[-1] == ACTION_SNAPSHOT_DEPTH

    async def test_without_deepen_on_miss_it_stays_shallow(self, phone):
        ctrl, wda = phone
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier=WIFI, snapshot_depth=ACTION_SNAPSHOT_DEPTH,
        )
        assert elements == []
        assert wda.deep_queries() == []


class TestTapElement:
    async def test_it_taps_a_deeply_nested_element_and_says_how(self, phone):
        ctrl, wda = phone
        result = await ctrl.tap_element(identifier=WIFI, scroll_to_find=False)
        assert result["status"] == "ok", result
        assert result["snapshot_depth"] == SNAPSHOT_MAX_DEPTH
        assert f"snapshot_depth={SNAPSHOT_MAX_DEPTH}" in result["depth_note"]
        ctrl.wda_client.tap.assert_awaited()

    async def test_a_deep_hit_does_not_escalate_the_reads_after_it(self, phone):
        """The first version set the tap's read depth to 25 after a deep hit, so
        its context read and sweep became an unfiltered deep /source."""
        ctrl, wda = phone
        # Found deep but unreachable: the path that reads the whole screen.
        ctrl._wda_reachable_only = AsyncMock(
            side_effect=lambda r, m: ([], [{"why": "off screen", "label": "Wi-Fi"}]))
        await ctrl.tap_element(identifier=WIFI, scroll_to_find=False)
        assert ctrl.wda_client.describe_all.call_args_list, (
            "the whole-screen read never ran, so this test checks nothing")
        _no_deep_source(ctrl)

    async def test_a_depth_the_caller_chose_is_not_overridden(self, phone):
        ctrl, wda = phone
        result = await ctrl.tap_element(identifier=WIFI, snapshot_depth=12, scroll_to_find=False)
        assert result["status"] == "not_found"
        assert wda.deep_queries() == []
        assert "queried again" not in result["detail"]

    async def test_not_found_says_it_looked_deeper_only_when_it_did(self, phone):
        ctrl, wda = phone
        looked = await ctrl.tap_element(identifier="absent", scroll_to_find=False)
        assert looked["status"] == "not_found"
        assert f"queried again at {SNAPSHOT_MAX_DEPTH}" in looked["detail"]

        wda.queries.clear()
        # label_contains alone has no filter, so no query path and no deeper
        # look -- the first version claimed one anyway (#470 review).
        unfiltered = await ctrl.tap_element(label_contains="Wi-F", scroll_to_find=False)
        assert unfiltered["status"] == "not_found"
        assert wda.deep_queries() == []
        assert "queried again" not in unfiltered["detail"]


class TestTheOtherActions:
    async def test_get_element_finds_a_deeply_nested_element(self, phone):
        ctrl, wda = phone
        result, _ = await ctrl.get_element(identifier=WIFI)
        assert result["identifier"] == WIFI
        assert result["snapshot_depth"] == SNAPSHOT_MAX_DEPTH
        assert f"snapshot_depth={SNAPSHOT_MAX_DEPTH}" in result["depth_note"]
        _no_deep_source(ctrl)

    async def test_get_element_respects_a_chosen_depth(self, phone):
        ctrl, wda = phone
        with pytest.raises(DeviceError, match="No element found"):
            await ctrl.get_element(identifier=WIFI, snapshot_depth=12)
        assert wda.deep_queries() == []

    async def test_wait_finds_an_element_that_appears_after_the_first_poll(self, phone):
        """The common wait: tap, then wait for a row on the next screen. Looking
        deeper only on poll one left this case unfixed (#470 review)."""
        ctrl, wda = phone
        real_query = wda.query
        shallow_polls = {"n": 0}

        async def appearing(udid, using, value, **kw):
            if kw.get("timeout") != controller_ui.DEEP_QUERY_TIMEOUT:
                shallow_polls["n"] += 1
            if shallow_polls["n"] < 4:  # not there yet
                wda.queries.append({"depth": wda.depth, "value": value, **kw})
                return []
            return await real_query(udid, using, value, **kw)

        ctrl.wda_client.find_elements_by_query = appearing
        result, _ = await ctrl.wait_for_element(
            WaitCondition.EXISTS, identifier=WIFI, timeout=2, interval=0.02,
        )
        assert result["matched"] is True, result
        assert result["snapshot_depth"] == SNAPSHOT_MAX_DEPTH

    async def test_a_wait_on_a_dense_screen_never_looks_deeper(self, phone, monkeypatch):
        ctrl, wda = phone
        monkeypatch.setattr(controller_ui, "DEEPEN_MAX_SHALLOW_SECONDS", 0.01)
        wda.delay = 0.02
        result, _ = await ctrl.wait_for_element(
            WaitCondition.EXISTS, identifier="absent", timeout=0.3, interval=0.02,
        )
        assert result["matched"] is False
        assert wda.deep_queries() == []

    async def test_two_requests_on_one_device_keep_their_own_result(self, phone):
        """Per-device state let a concurrent request read another's deepening."""
        ctrl, wda = phone
        # The shallow read holds Edit and not Wi-Fi: one request finds its
        # element there, the other has to look deeper.
        ctrl.wda_client.describe_all = AsyncMock(return_value=[dict(SHALLOW_ROW)])
        deep, shallow = await asyncio.gather(
            ctrl.get_element(identifier=WIFI),
            ctrl.get_element(identifier="shallow_btn"),
        )
        assert deep[0].get("snapshot_depth") == SNAPSHOT_MAX_DEPTH
        assert "depth_note" not in shallow[0]


class TestTypeAndClearPassTheDepth:
    async def test_type_text_reads_at_the_depth_given(self, phone):
        ctrl, _ = phone
        seen = {}

        async def capture(*a, **k):
            seen.update(k)
            return [], PHONE

        ctrl.get_ui_elements = capture
        with pytest.raises(DeviceError):  # nothing on screen: no field to type into
            await ctrl._find_text_field(PHONE, label="Search", identifier=None, snapshot_depth=20)
        assert seen.get("snapshot_depth") == 20

    async def test_clear_text_reads_at_the_depth_given(self, phone):
        ctrl, _ = phone
        seen = {}

        async def capture(*a, **k):
            seen.update(k)
            return [], PHONE

        ctrl.get_ui_elements = capture
        with pytest.raises(DeviceError):
            await ctrl.clear_text(udid=PHONE, label="Search", snapshot_depth=20)
        assert seen.get("snapshot_depth") == 20


class TestTheBoundedQueryAtTheTransport:
    """`no_resend` at the layer that decides it. The tests above replace
    `find_elements_by_query` whole, so they cannot see whether a timed-out deep
    query was re-sent -- which on WDA's one-at-a-time runner queues a second
    walk behind the first and drops a phone's forward (#470 review)."""

    async def test_a_timed_out_bounded_query_is_sent_once(self, monkeypatch):
        import httpx

        from server.device.ios import wda_client as wda_mod
        from server.device.ios.wda_client import WdaBackend
        from tests.test_wda_writes_not_resent import SIM, FakeWDA, _req

        backend = WdaBackend()
        backend.register_simulator(SIM, 8200)
        fake = FakeWDA({"/elements": [httpx.ReadTimeout("slow", request=_req()) for _ in range(2)]})
        monkeypatch.setattr(wda_mod.httpx, "AsyncClient", fake)

        from server.device.ios.wda_client import QueryTimedOut
        with pytest.raises(QueryTimedOut):
            await backend.find_elements_by_query(
                SIM, "accessibility id", WIFI, timeout=5, no_resend=True)
        assert fake.reached("/elements") == 1, "a timed-out bounded query was re-sent"

    async def test_an_ordinary_query_keeps_its_retry(self, monkeypatch):
        import httpx

        from server.device.ios import wda_client as wda_mod
        from server.device.ios.wda_client import WdaBackend
        from tests.test_wda_writes_not_resent import SIM, FakeWDA, _req

        backend = WdaBackend()
        backend.register_simulator(SIM, 8200)
        fake = FakeWDA({"/elements": [httpx.ReadTimeout("slow", request=_req())]})
        monkeypatch.setattr(wda_mod.httpx, "AsyncClient", fake)

        await backend.find_elements_by_query(SIM, "accessibility id", WIFI)
        assert fake.reached("/elements") == 2, "the default read retry was lost"


class TestTheRoundTwoGuards:
    """The second review's findings: what happens after a deep hit and after a
    deep query that ran out of time."""

    async def test_a_slow_deep_miss_does_not_fall_back_to_source(self, phone, monkeypatch):
        ctrl, wda = phone
        monkeypatch.setattr(controller_ui, "SLOW_DEEP_MISS_SECONDS", 0.05)
        wda.deep_delay = 0.08
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier="absent", snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        assert elements == []
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_every_deep_query_is_bounded_including_the_predicate_retry(self, phone):
        ctrl, wda = phone
        await ctrl.get_ui_elements(
            PHONE, filter_identifier="absent", snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        deep = wda.deep_queries()
        assert len(deep) == 2, "expected the id query and its predicate retry"
        assert all(q.get("no_resend") is True for q in deep)

    async def test_get_element_on_a_slow_screen_does_not_look_deeper(self, phone, monkeypatch):
        """The gate times a filtered query, as tap_element's does."""
        ctrl, wda = phone
        monkeypatch.setattr(controller_ui, "DEEPEN_MAX_SHALLOW_SECONDS", 0.05)
        wda.delay = 0.08
        with pytest.raises(DeviceError):
            await ctrl.get_element(identifier=WIFI)
        assert wda.deep_queries() == []

    async def test_a_slow_source_with_a_quick_query_still_looks_deeper(self, phone, monkeypatch):
        """A phone's unfiltered /source can take over a second where the query
        is quick; gating on the /source skipped the deep look (CodeRabbit)."""
        ctrl, wda = phone
        monkeypatch.setattr(controller_ui, "DEEPEN_MAX_SHALLOW_SECONDS", 0.05)

        async def slow_source(*a, **k):
            await asyncio.sleep(0.08)
            return []

        ctrl.wda_client.describe_all = slow_source
        result, _ = await ctrl.get_element(identifier=WIFI)
        assert result["snapshot_depth"] == SNAPSHOT_MAX_DEPTH

    async def test_a_deep_hit_is_seen_by_reachability_and_then_put_back(self, phone):
        """Reachability re-queries the element at the session depth, so it must
        run at 25; after it, the depth goes back to 12 -- left at 25, the next
        query that sets no depth walked the full tree."""
        ctrl, wda = phone
        depth_at_reachability = []

        async def reachable(resolved, matches):
            depth_at_reachability.append(wda.depth)
            return matches, []

        ctrl._wda_reachable_only = reachable
        result = await ctrl.tap_element(identifier=WIFI, scroll_to_find=False)
        assert result["status"] == "ok"
        assert depth_at_reachability == [SNAPSHOT_MAX_DEPTH]
        assert wda.depth == ACTION_SNAPSHOT_DEPTH, "the session was left at the full depth"

    async def test_get_element_puts_the_depth_back_after_a_hit(self, phone):
        ctrl, wda = phone
        await ctrl.get_element(identifier=WIFI)
        assert wda.depth == ACTION_SNAPSHOT_DEPTH

    async def test_a_timed_out_deep_query_skips_the_full_read_and_says_so(
        self, phone, monkeypatch,
    ):
        ctrl, wda = phone
        wda.deep_timeouts = {"accessibility id"}
        result = await ctrl.tap_element(identifier="absent", scroll_to_find=False)
        assert result["status"] == "not_found"
        assert "timed out" in result["detail"], result["detail"]
        assert "queried again" not in result["detail"]
        for call in ctrl.wda_client.describe_all.call_args_list:
            raise AssertionError(f"a full read queued behind the deep walk: {call}")


async def test_the_sweep_after_an_unreachable_deep_hit_starts_shallow(phone):
    """The second review's finding 1: found only at 25 but not reachable, with
    scroll_to_find=True, the sweep's first read is an Application query that
    sets no depth of its own -- it ran at 25, a full-tree walk on a long list."""
    ctrl, wda = phone
    ctrl._wda_reachable_only = AsyncMock(
        side_effect=lambda r, m: ([], [{"why": "off screen", "label": "Wi-Fi"}]))
    depth_at_sweep = []

    async def sweep(*a, **k):
        depth_at_sweep.append(wda.depth)
        return None

    ctrl._ios_scroll_to_element = sweep
    await ctrl.tap_element(identifier=WIFI, scroll_to_find=True)
    assert depth_at_sweep, "the sweep never ran, so this test checks nothing"
    assert depth_at_sweep == [ACTION_SNAPSHOT_DEPTH], depth_at_sweep


class TestTheCodeRabbitFindings:
    async def test_a_timed_out_predicate_retry_is_still_a_timeout(self, phone):
        """The id query misses quickly, the predicate retry times out: judged by
        elapsed time it looked like a fast miss, and a full read queued behind
        the walk (CodeRabbit, #474)."""
        ctrl, wda = phone
        wda.deep_timeouts = {"predicate string"}
        result = await ctrl.tap_element(identifier="absent", scroll_to_find=False)
        assert "timed out" in result["detail"], result["detail"]
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_the_value_check_sees_a_deep_element(self, phone):
        """value= reads the element's value by re-querying it at the session
        depth; restored to 12 first, it could not find a row found at 25."""
        ctrl, wda = phone
        depth_at_value_read = []

        async def element_value(*a, **k):
            depth_at_value_read.append(wda.depth)
            return "0"

        ctrl.wda_client.element_value = element_value
        await ctrl.tap_element(identifier=WIFI, value="1", scroll_to_find=False)
        assert depth_at_value_read == [SNAPSHOT_MAX_DEPTH]
        assert wda.depth == ACTION_SNAPSHOT_DEPTH, "not put back after the tap"

    async def test_the_stability_check_re_queries_deep_without_a_source_fallback(self, phone):
        ctrl, wda = phone
        before = len(wda.deep_queries())
        result = await ctrl.tap_element(identifier=WIFI, scroll_to_find=False)
        assert result["status"] == "ok"
        assert len(wda.deep_queries()) > before + 1, "the stability re-check did not query deep"
        ctrl.wda_client.describe_all.assert_not_called()
        assert wda.depth == ACTION_SNAPSHOT_DEPTH

    async def test_the_depth_is_put_back_even_when_the_tap_raises(self, phone):
        ctrl, wda = phone
        ctrl.wda_client.tap = AsyncMock(side_effect=DeviceError("boom", tool="wda"))
        with pytest.raises(DeviceError):
            await ctrl.tap_element(identifier=WIFI, scroll_to_find=False,
                                   skip_stability_check=True)
        assert wda.depth == ACTION_SNAPSHOT_DEPTH


class TestTheSecondCodeRabbitPass:
    async def test_get_element_uses_what_its_filtered_query_found(self, phone):
        """The unfiltered read missed; the filtered query at the shallow depth
        found it. It was timed and thrown away, then a deep query ran."""
        ctrl, wda = phone
        result, _ = await ctrl.get_element(identifier="shallow_btn")
        assert result["identifier"] == "shallow_btn"
        assert "depth_note" not in result
        assert wda.deep_queries() == [], "it went deeper for an element it already had"

    async def test_the_final_stability_re_check_also_queries_deep(self, phone):
        """When the row moved between reads, the final re-check still read at
        12 and fell back to an unfiltered /source (CodeRabbit, #474)."""
        ctrl, wda = phone
        moves = {"n": 0}
        real = wda.query

        async def moving(udid, using, value, **kw):
            found = await real(udid, using, value, **kw)
            if found and WIFI in value:
                moves["n"] += 1
                found[0]["frame"] = dict(found[0]["frame"], y=200 + moves["n"])
            return found

        ctrl.wda_client.find_elements_by_query = moving
        ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
        result = await ctrl.tap_element(identifier=WIFI, scroll_to_find=False)
        assert result["status"] == "ok"
        ctrl.wait_for_settle.assert_awaited()  # the row moved, so the final re-check ran
        assert moves["n"] >= 3, "the final re-check did not find it deep"
        ctrl.wda_client.describe_all.assert_not_called()


#: On a long list, below the viewport, and nested as deep as the Wi-Fi row.
OFF_SCREEN = "com.apple.settings.privacy"


class TestTheScrollSweepLooksDeeperFirst:
    """#475: scroll_to_element's opening lookup read at 12 with no deeper look,
    so a row on screen but nested at 20 read as absent and the sweep swiped."""

    @pytest.fixture
    def sweeping(self, phone, monkeypatch):
        ctrl, wda = phone
        real = wda.query

        async def query(udid, using, value, **kw):
            if OFF_SCREEN in value and wda.depth is not None and wda.depth >= 20:
                wda.queries.append({"depth": wda.depth, "value": value, **kw})
                return [dict(WIFI_ROW, AXUniqueId=OFF_SCREEN, AXLabel="Privacy",
                             frame=dict(WIFI_ROW["frame"], y=2000))]
            return await real(udid, using, value, **kw)

        ctrl.wda_client.find_elements_by_query = query
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        depth_at_swipe: list[int | None] = []

        async def swipe(*a, **k):
            depth_at_swipe.append(wda.depth)

        ctrl.wda_client.swipe = swipe
        return ctrl, wda, depth_at_swipe

    async def test_an_on_screen_row_nested_deep_is_found_without_a_swipe(self, sweeping):
        ctrl, wda, swipes = sweeping
        result = await ctrl.scroll_to_element(identifier=WIFI)
        assert result["status"] == "ok", result
        assert result["element"]["identifier"] == WIFI
        assert swipes == []
        assert len(wda.deep_queries()) == 1
        assert wda.depth == ACTION_SNAPSHOT_DEPTH, "the full depth was left on the session"

    async def test_an_off_screen_deep_row_sweeps_at_the_shallow_depth(self, sweeping):
        ctrl, wda, swipes = sweeping
        await ctrl.scroll_to_element(identifier=OFF_SCREEN)
        assert swipes, "the sweep never swiped, so this test checks nothing"
        assert all(d == ACTION_SNAPSHOT_DEPTH for d in swipes), swipes
        assert len(wda.deep_queries()) == 1, "the sweep's own reads went deep"
        _no_deep_source(ctrl)
        # The viewport read sets no depth of its own and inherits the session's,
        # which is why the session has to be shallow again before it.
        assert ctrl.wda_client.describe_all.call_args_list, "no sweep read ran"

    async def test_a_dense_screen_sweeps_without_looking_deeper(self, sweeping, monkeypatch):
        ctrl, wda, swipes = sweeping
        monkeypatch.setattr(controller_ui, "DEEPEN_MAX_SHALLOW_SECONDS", 0.05)
        wda.delay = 0.08
        await ctrl.scroll_to_element(identifier=WIFI)
        assert wda.deep_queries() == []
        assert swipes, "a miss with no deeper look still sweeps"

    async def test_a_depth_the_caller_chose_is_not_overridden(self, sweeping):
        ctrl, wda, swipes = sweeping
        result = await ctrl.scroll_to_element(identifier=WIFI, snapshot_depth=12)
        assert result["status"] == "not_found"
        assert wda.deep_queries() == []

    async def test_a_timed_out_deeper_look_is_not_re_sent_and_the_depth_goes_back(
        self, sweeping,
    ):
        ctrl, wda, swipes = sweeping
        wda.deep_timeouts = {"accessibility id", "predicate string"}
        await ctrl.scroll_to_element(identifier=WIFI)
        assert len(wda.deep_queries()) == 1
        assert wda.deep_queries()[0]["no_resend"] is True
        assert swipes and all(d == ACTION_SNAPSHOT_DEPTH for d in swipes), swipes
        _no_deep_source(ctrl)

    async def test_tap_elements_sweep_does_not_look_deeper_a_second_time(self, sweeping):
        """tap_element's own lookup already looked deeper (#470)."""
        ctrl, wda, swipes = sweeping
        ctrl._wda_reachable_only = AsyncMock(
            side_effect=lambda r, m: ([], [{"why": "off screen", "label": "Privacy"}]))
        await ctrl.tap_element(identifier=OFF_SCREEN, scroll_to_find=True)
        assert swipes, "the sweep never ran, so this test checks nothing"
        assert len(wda.deep_queries()) == 1
