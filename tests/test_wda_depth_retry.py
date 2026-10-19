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


def _png(shade: int = 128) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("L", (390, 844), shade).save(out, format="PNG")
    return out.getvalue()


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
    # The sweep's frames (#475). Unstubbed, the real one opens a usbmux forward.
    ctrl.wda_client.screenshot = AsyncMock(return_value=_png())
    # The sweep's element scroll (#475): no container unless a test gives one.
    ctrl.wda_client.find_scroll_container = AsyncMock(return_value=None)
    ctrl.wda_client.scroll_element = AsyncMock()
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

    async def test_an_off_screen_row_found_only_deep_is_looked_for_deep(self, sweeping):
        """Found at 25 and not on screen: the sweep looks for it there, by bounded
        query, and reads no tree (#475)."""
        ctrl, wda, swipes = sweeping
        await ctrl.scroll_to_element(identifier=OFF_SCREEN)
        assert swipes, "the sweep never swiped, so this test checks nothing"
        deep = wda.deep_queries()
        assert len(deep) > 1, "the sweep's reads did not look deep"
        assert all(q.get("no_resend") is True for q in deep)
        assert [q["depth"] for q in wda.queries[1:]].count(ACTION_SNAPSHOT_DEPTH) <= 2, (
            "a target found only deep was also looked for shallow on every swipe")
        # The viewport read, before anything else; none in the sweep.
        assert ctrl.wda_client.describe_all.await_count == 1
        assert wda.depth == ACTION_SNAPSHOT_DEPTH, "the sweep left the session deep"

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

    async def test_tap_elements_sweep_looks_where_its_lookup_found_the_target(self, sweeping):
        """tap_element's own lookup found it only deep (#470); its sweep looks
        there too, from what that lookup learned (#475)."""
        ctrl, wda, swipes = sweeping
        ctrl._wda_reachable_only = AsyncMock(
            side_effect=lambda r, m: ([], [{"why": "off screen", "label": "Privacy"}]))
        await ctrl.tap_element(identifier=OFF_SCREEN, scroll_to_find=True)
        assert swipes, "the sweep never ran, so this test checks nothing"
        assert len(wda.deep_queries()) > 1
        assert wda.depth == ACTION_SNAPSHOT_DEPTH

    async def test_a_chosen_depth_keeps_tap_elements_sweep_at_it(self, sweeping):
        ctrl, wda, swipes = sweeping
        await ctrl.tap_element(identifier="absent", scroll_to_find=True, snapshot_depth=12)
        assert swipes, "the sweep never ran, so this test checks nothing"
        assert wda.deep_queries() == []


class LazyDeepList:
    """A long list the way iOS 26 draws one, as #475 measured it live: lazy (only
    rows on screen exist) and deep (only from depth 18). Swipes move it, and its
    screenshot changes exactly when its content does.

    Rows are 50pt from y=100; the viewport shows 700pt."""

    ROW_H, TOP, VIEW = 50, 100, 700

    def __init__(self, wda: FakeWda, rows: int = 200, lazy: bool = True,
                 min_depth: int = 18):
        self.wda = wda
        self.ROWS = rows
        self.lazy = lazy
        self.min_depth = min_depth
        self.offset = 0.0
        self.animated = False
        self.frames = 0
        self.timeout_from_swipe: int | None = None
        self.swipes = 0
        #: Swipes (1-based) that WDA accepts and the app ignores, as live.
        self.dropped: set[int] = set()
        self.scrolls: list[tuple[str, float, int | None]] = []
        self.has_container = True
        self.scroll_fails = False
        #: A pull at the top that redraws a strip without moving the list, as
        #: Settings' search field does live: 0.5% of the screen, toggling.
        self.top_pull_artifact = False
        self._pulled = False
        #: A status-bar clock that changes on every frame.
        self.clock = False
        #: Rows drawn as short dashes: a sparse list, ~1% change per step.
        self.sparse = False
        self.screenshot_fails = False
        #: The scroll is performed, and then the answer is lost (#478 review).
        self.scroll_runs_then_fails = False
        self.events: list[str] = []

    @property
    def max_offset(self):
        return self.ROWS * self.ROW_H - self.VIEW

    async def query(self, udid, using, value, **kw):
        self.wda.queries.append(
            {"depth": self.wda.depth, "value": value, "swipes": self.swipes, **kw})
        deep = self.wda.depth == SNAPSHOT_MAX_DEPTH
        if (deep and kw.get("no_resend") and self.timeout_from_swipe is not None
                and self.swipes >= self.timeout_from_swipe):
            from server.device.ios.wda_client import QueryTimedOut
            raise QueryTimedOut(value)
        if self.wda.depth is None or self.wda.depth < self.min_depth:
            return []
        for i in range(self.ROWS):
            if f"'lazy_{i}'" in value or value == f"lazy_{i}":
                y = self.TOP + i * self.ROW_H - self.offset
                if not self.lazy or self.TOP - self.ROW_H < y < self.TOP + self.VIEW:
                    return [{"type": "Cell", "AXUniqueId": f"lazy_{i}", "AXLabel": f"Row {i}",
                             "frame": {"x": 0, "y": y, "width": 390, "height": self.ROW_H}}]
        return []

    async def swipe(self, udid, x1, y1, x2, y2, *a, **k):
        self.events.append("drag")
        self.swipes += 1
        if self.swipes in self.dropped:
            return
        if self.top_pull_artifact and self.offset == 0 and y2 > y1:
            self._pulled = not self._pulled
        self.offset = min(max(self.offset + (y1 - y2), 0), self.max_offset)

    async def find_scroll_container(self, udid, depths, point=None):
        self.lookups = getattr(self, "lookups", 0) + 1
        self.lookup_point = point
        if not self.has_container:
            return None
        await self.wda.set_depth(udid, depths[-1])
        return "cv-1", depths[-1]

    async def scroll_element(self, udid, element_id, direction, distance):
        """Never dropped, as measured; travels its share of the viewport."""
        self.scrolls.append((direction, distance, self.wda.depth))
        self.events.append("scroll")
        if self.scroll_fails and not self.scroll_runs_then_fails:
            raise DeviceError("WDA did not answer the scroll", tool="wda")
        delta = distance * self.VIEW / 0.75 * 0.9
        self.offset = min(max(self.offset + (delta if direction == "down" else -delta), 0),
                          self.max_offset)
        if self.scroll_runs_then_fails:
            raise DeviceError("WDA did not answer the scroll; it may have run", tool="wda")

    async def screenshot(self, udid, **k):
        import io
        import random

        from PIL import Image, ImageDraw

        self.frames += 1
        self.events.append("frame")
        if self.screenshot_fails:
            raise DeviceError("WDA screenshot timed out", tool="wda")
        image = Image.new("L", (390, 852), 255)
        draw = ImageDraw.Draw(image)
        first = int(self.offset // self.ROW_H)
        for i in range(first, min(self.ROWS, first + self.VIEW // self.ROW_H + 2)):
            y = self.TOP + i * self.ROW_H - self.offset
            if self.sparse:
                draw.rectangle((12, y + 20, 12 + 5 * (1 + i % 2), y + 22), fill=0)
            else:
                draw.rectangle((0, y, 390, y + self.ROW_H - 2), fill=(i * 37) % 200)
        if self.clock:
            draw.rectangle((40, 4, 350, 44), fill=self.frames * 53 % 256)
        if self.animated:
            draw.rectangle((0, 400, 390, 440), fill=random.randrange(256))
        if self._pulled:
            draw.rectangle((0, 120, 190, 121), fill=255)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()


class TestTheSweepOnALazyDeepList:
    """End to end through the sweep: the case that failed live on main, where
    the shallow sweep saw neither the row nor the list move (#475)."""

    @staticmethod
    def _install(ctrl, wda, **kw):
        world = LazyDeepList(wda, **kw)
        ctrl.wda_client.find_elements_by_query = world.query
        ctrl.wda_client.swipe = world.swipe
        ctrl.wda_client.screenshot = world.screenshot
        ctrl.wda_client.find_scroll_container = world.find_scroll_container
        ctrl.wda_client.scroll_element = world.scroll_element
        world.sources_at = []

        async def describe_all(*a, **k):
            world.sources_at.append(world.swipes)
            return []

        ctrl.wda_client.describe_all = describe_all
        return world

    @pytest.fixture
    def lazy(self, phone):
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        return ctrl, wda, self._install(ctrl, wda)

    async def test_it_scrolls_to_an_off_screen_row(self, lazy):
        ctrl, wda, world = lazy
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok", result
        assert result["element"]["identifier"] == "lazy_150"
        assert 0 < world.swipes < 20
        assert all(q.get("no_resend") for q in wda.deep_queries())
        # The viewport read and the cold lookup's own fallback, both at 12 and
        # both before the first swipe; none in the sweep.
        assert world.sources_at and set(world.sources_at) == {0}, world.sources_at
        assert wda.depth == ACTION_SNAPSHOT_DEPTH

    async def test_an_absent_row_ends_at_both_ends_of_the_list(self, phone):
        """Frames that stop changing are the ends; without them the sweep spent
        its whole budget, or on main stopped after two swipes."""
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda, rows=60)
        result = await ctrl.scroll_to_element(identifier="absent")
        assert result["status"] == "not_found"
        down = -(-world.max_offset // (0.88 * 852 - 0.13 * 852))  # swipes to the end
        assert world.offset == 0, "it did not come back up to the top"
        assert down < world.swipes <= 2 * down + 4, world.swipes

    async def test_an_animated_screen_costs_swipes_not_a_wrong_answer(self, lazy):
        ctrl, wda, world = lazy
        world.animated = True
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok"
        world.offset = 0
        result = await ctrl.scroll_to_element(identifier="absent", max_swipes=4)
        assert result["status"] == "not_found"
        assert world.swipes <= 12 + 20, "the budget did not bound it"

    async def test_a_deep_query_that_times_out_is_dropped_not_re_sent(self, lazy):
        ctrl, wda, world = lazy
        world.timeout_from_swipe = 2
        await ctrl.scroll_to_element(identifier="lazy_150")
        deep = wda.deep_queries()
        assert any(q["swipes"] < 2 for q in deep), "never looked deep before the timeout"
        late = [q for q in deep if q["swipes"] >= 2]
        assert len(late) == 1, f"it went on querying deep after a timeout: {late}"
        assert all(q.get("no_resend") for q in deep)
        assert wda.depth == ACTION_SNAPSHOT_DEPTH

    async def test_a_target_only_deep_and_a_timeout_gives_up_and_says_why(
        self, phone, caplog,
    ):
        """Located off screen at 25 only, so the sweep looks only there; when that
        query times out there is nowhere left to look."""
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda, lazy=False)
        world.timeout_from_swipe = 1
        caplog.set_level("INFO")
        result = await ctrl.scroll_to_element(identifier="lazy_15")
        assert result["status"] == "not_found"
        assert "that query timed out" in caplog.text
        assert world.swipes <= 2

    async def test_a_dropped_drag_is_redone_as_an_element_scroll(self, lazy):
        """Live, WDA accepted drags the app then ignored; one frame unchanged
        turned the sweep round part way down the list (#475)."""
        ctrl, wda, world = lazy
        world.dropped = {4}
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok", result
        assert world.swipes > 4, "the drop never happened, so this checks nothing"
        assert [d for d, _, _ in world.scrolls] == ["down"], world.scrolls

    async def test_the_scroll_runs_at_the_depth_its_container_was_found_at(self, lazy):
        ctrl, wda, world = lazy
        world.dropped = {2}
        await ctrl.scroll_to_element(identifier="lazy_150")
        assert world.scrolls and world.scrolls[0][2] == SNAPSHOT_MAX_DEPTH
        assert wda.depth == ACTION_SNAPSHOT_DEPTH, "the sweep left the session deep"

    async def test_the_container_is_looked_up_once_and_only_when_needed(self, phone):
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda)
        await ctrl.scroll_to_element(identifier="lazy_100")
        assert getattr(world, "lookups", 0) == 0, "looked up with nothing dropped"
        world.offset, world.swipes, world.dropped = 0, 0, {2, 5, 7}
        await ctrl.scroll_to_element(identifier="lazy_150")
        assert world.lookups == 1

    async def test_an_element_scroll_that_also_moves_nothing_is_believed(self, phone):
        """At the end of the list: one scroll confirms it, not a second drag."""
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda, rows=30)
        result = await ctrl.scroll_to_element(identifier="absent")
        assert result["status"] == "not_found"
        assert len(world.scrolls) == 2, world.scrolls  # one at each end
        assert [d for d, _, _ in world.scrolls] == ["down", "up"]

    async def test_without_a_container_a_second_drag_settles_it(self, phone):
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda)
        world.has_container, world.dropped = False, {4}
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok", result
        assert world.scrolls == []

    async def test_a_failing_element_scroll_falls_back_to_drags(self, phone):
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda)
        world.scroll_fails, world.dropped = True, {4, 9}
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok", result
        assert len(world.scrolls) == 1, "kept trying a scroll that had failed"

    async def test_a_pull_at_the_top_that_moves_nothing_is_not_movement(self, phone):
        """Live, a pull at the top of Settings redrew a strip (0.5% of the
        frame) without the list moving, and the sweep read it as movement on
        every pull until its budget ran out (#475)."""
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda, rows=30)
        world.top_pull_artifact = True
        result = await ctrl.scroll_to_element(identifier="absent")
        assert result["status"] == "not_found"
        assert world._pulled or world.swipes, "never pulled at the top"
        assert world.swipes < 12, f"{world.swipes} swipes: the pull read as movement"

    async def test_every_scroll_runs_at_the_containers_depth(self, phone):
        """A container found at 25 (Settings' is) while the sweep looks for its
        target at 12: the second redo, with the container cached, came with the
        session at 12, where WDA could not find the container again (#475)."""
        ctrl, wda = phone
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        world = self._install(ctrl, wda, lazy=False, min_depth=12)
        world.dropped = {2, 6}
        await ctrl.scroll_to_element(identifier="lazy_150")
        assert len(world.scrolls) == 2, world.scrolls
        assert [d for _, _, d in world.scrolls] == [SNAPSHOT_MAX_DEPTH] * 2, world.scrolls


class TestTheReviewFindings:
    """The #475 adversarial review."""

    @staticmethod
    def _world(ctrl, wda, **kw):
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        return TestTheSweepOnALazyDeepList._install(ctrl, wda, **kw)

    async def test_a_deep_sweep_hit_is_used_at_its_depth_then_put_back(self, phone):
        """Finding 1: the sweep put the shallow depth back before tap_element's
        reachability, value and stability checks looked the row up again."""
        ctrl, wda = phone
        self._world(ctrl, wda)
        depth_at_check: list[int | None] = []

        async def reachable(r, matches):
            depth_at_check.append(wda.depth)
            return matches, []

        ctrl._wda_reachable_only = reachable
        ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
        result = await ctrl.tap_element(identifier="lazy_40", scroll_to_find=True)
        assert result["status"] == "ok", result
        assert depth_at_check == [SNAPSHOT_MAX_DEPTH], depth_at_check
        assert result.get("snapshot_depth") == SNAPSHOT_MAX_DEPTH, "the deep hit was not reported"
        assert wda.depth == ACTION_SNAPSHOT_DEPTH, "the tap left the session deep"

    async def test_scroll_to_element_puts_a_deep_hit_back_at_once(self, phone):
        ctrl, wda = phone
        self._world(ctrl, wda)
        result = await ctrl.scroll_to_element(identifier="lazy_40")
        assert result["status"] == "ok"
        assert wda.depth == ACTION_SNAPSHOT_DEPTH

    async def test_a_slow_deep_hit_is_kept(self, phone, monkeypatch):
        """Finding 2: a slow deep query that *found* the target was dropped like
        a slow miss, and the sweep gave up on a row it had located."""
        ctrl, wda = phone
        monkeypatch.setattr(controller_ui, "SLOW_DEEP_MISS_SECONDS", 0.0)
        world = self._world(ctrl, wda, lazy=False)
        result = await ctrl.scroll_to_element(identifier="lazy_40")
        assert result["status"] == "ok", result
        assert world.swipes > 0

    async def test_a_failing_restore_does_not_replace_the_sweeps_answer(self, phone):
        """Finding 3: the restore in the finally raised over the sweep's result."""
        ctrl, wda = phone
        world = self._world(ctrl, wda)
        real_set, real_query = wda.set_depth, world.query
        seen = {"found": False}

        async def query(udid, using, value, **kw):
            found = await real_query(udid, using, value, **kw)
            seen["found"] = seen["found"] or bool(found)
            return found

        async def flaky(udid, depth):
            # Only the set after the row is found: the sweep's finally.
            if seen["found"]:
                raise DeviceError("Failed to create WDA session", tool="wda")
            await real_set(udid, depth)

        ctrl.wda_client.find_elements_by_query = query
        ctrl.wda_client._set_snapshot_depth = flaky
        found = await ctrl._ios_scroll_to_element(
            PHONE, None, "lazy_150", 10, snapshot_depth=ACTION_SNAPSHOT_DEPTH, deepen=True,
        )
        assert found is not None and found.identifier == "lazy_150"

    async def test_a_failing_restore_does_not_replace_the_sweeps_error(self, phone):
        """The restore must fail only after the swipe has: the opening lookup and
        the first read set the depth too (CodeRabbit, #478)."""
        ctrl, wda = phone
        self._world(ctrl, wda)
        swiped = {"yes": False}
        real_set = wda.set_depth

        async def swipe_dies(*a, **k):
            swiped["yes"] = True
            raise DeviceError("WDA did not answer the swipe; it may already have been performed",
                              tool="wda")

        async def no_session(udid, depth):
            if swiped["yes"]:
                raise DeviceError("Failed to create WDA session", tool="wda")
            await real_set(udid, depth)

        ctrl.wda_client.swipe = swipe_dies
        ctrl.wda_client._set_snapshot_depth = no_session
        with pytest.raises(DeviceError, match="may already have been performed"):
            await ctrl._ios_scroll_to_element(
                PHONE, None, "lazy_150", 10, snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            )

    async def test_two_failed_frames_end_the_sweep(self, phone, caplog):
        """Finding 6: blind, a failed frame counted as movement on every swipe,
        to the end of the budget -- pulls at the top included."""
        ctrl, wda = phone
        world = self._world(ctrl, wda, rows=30)
        world.screenshot_fails = True
        caplog.set_level("INFO")
        result = await ctrl.scroll_to_element(identifier="absent")
        assert result["status"] == "not_found"
        assert world.swipes <= 2, world.swipes
        assert "two screenshots in a row failed" in caplog.text

    async def test_the_container_is_looked_for_under_the_swipe(self, phone):
        """Finding 7."""
        ctrl, wda = phone
        world = self._world(ctrl, wda)
        world.dropped = {2}
        await ctrl.scroll_to_element(identifier="lazy_150")
        x, y = world.lookup_point
        assert 150 < x < 250 and 140 < y < 750, world.lookup_point

    async def test_a_redo_stays_inside_the_budget(self, phone):
        """Finding 8: the redo ran after the last budgeted swipe."""
        ctrl, wda = phone
        world = self._world(ctrl, wda)
        world.dropped = set(range(1, 100))
        report: dict = {}
        await ctrl._ios_scroll_to_element(
            PHONE, None, "absent", 1, snapshot_depth=ACTION_SNAPSHOT_DEPTH, report=report,
        )
        assert world.swipes + len(world.scrolls) <= 3, (world.swipes, world.scrolls)

    async def test_the_report_counts_element_scrolls_as_swipes(self, phone):
        """Finding 9: a real gesture the caller must hear about (#274)."""
        ctrl, wda = phone
        world = self._world(ctrl, wda)
        world.dropped = {3, 7}
        report: dict = {}
        await ctrl._ios_scroll_to_element(
            PHONE, None, "lazy_150", 10, snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen=True, report=report,
        )
        assert world.scrolls, "no scroll ran, so this checks nothing"
        assert report["swipes"] == world.swipes + len(world.scrolls), (report, world.swipes)

    async def test_a_ticking_clock_is_not_movement(self, phone):
        """Finding 9: the frame is cropped to the content area."""
        ctrl, wda = phone
        world = self._world(ctrl, wda, rows=30)
        world.clock = True
        result = await ctrl.scroll_to_element(identifier="absent")
        assert result["status"] == "not_found"
        assert world.swipes < 12, f"{world.swipes} swipes: the clock read as movement"

    async def test_a_sparse_list_still_reads_as_moving(self, phone):
        """Finding 5: text-only rows change ~1% of the frame per step; read as
        unmoved, the sweep turned round mid-list."""
        ctrl, wda = phone
        world = self._world(ctrl, wda)
        world.sparse = True
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok", result

    async def test_a_redo_toward_a_located_row_scrolls_proportionally(self, phone):
        """Finding 9: a short move toward a row above is not a full page."""
        ctrl, wda = phone
        world = self._world(ctrl, wda, lazy=False)
        world.offset, world.dropped = 300, {1}
        await ctrl.scroll_to_element(identifier="lazy_3")
        assert world.scrolls and world.scrolls[0][0] == "up", world.scrolls
        assert world.scrolls[0][1] < 0.5, world.scrolls

    async def test_a_scroll_that_failed_but_ran_is_looked_at_before_any_drag(self, phone):
        """#478 review: a failed element scroll may still have scrolled; the
        sweep dragged again without looking, and the two together could carry a
        row past between reads."""
        ctrl, wda = phone
        world = self._world(ctrl, wda)
        world.dropped, world.scroll_runs_then_fails = {2}, True
        result = await ctrl.scroll_to_element(identifier="lazy_150")
        assert result["status"] == "ok", result
        i = world.events.index("scroll")
        assert world.events[i + 1] == "frame", world.events[i - 2:i + 4]
