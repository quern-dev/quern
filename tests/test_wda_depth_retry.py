"""A WDA lookup that misses at the shallow action depth looks once more, deeper (#470).

Reads made for an action use snapshot depth 12 (F35): deep enough for a long
list's rows, and nine times faster than the full walk there. iOS 26's Settings
root nests its rows at 20, so `tap_element(identifier="com.apple.settings.wifi")`
and `get_element` answered not-found for an element on screen. Now a miss at
the action depth queries once more at the full depth -- a query, never a deep
/source, because a timed-out /source restarts the runner and a query does not.

The fake WDA below answers by the session's depth, which is how WDA behaves:
`snapshotMaxDepth` is a session setting that governs queries as well as /source.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.ios.wda_client import ACTION_SNAPSHOT_DEPTH, SNAPSHOT_MAX_DEPTH
from server.models import DeviceError, DeviceType, WaitCondition

PHONE = "PHYS-0001"
WIFI_ROW = {
    "type": "Cell", "AXUniqueId": "com.apple.settings.wifi", "AXLabel": "Wi-Fi",
    "frame": {"x": 0, "y": 200, "width": 390, "height": 44},
}


class FakeWda:
    """Sessions-wide depth, as WDA has it; the row exists only from depth 20."""

    def __init__(self, found_from: int = 20):
        self.depth: int | None = None
        self.depths_set: list[int] = []
        self.query_depths: list[int | None] = []
        self.found_from = found_from

    async def set_depth(self, udid, depth):
        self.depth = depth
        self.depths_set.append(depth)

    async def query(self, udid, using, value, **kw):
        self.query_depths.append(self.depth)
        # Only a query for the row finds it -- by accessibility id or by a
        # predicate naming it -- and only once the depth reaches it.
        asked_for_row = "com.apple.settings.wifi" in value
        if asked_for_row and self.depth is not None and self.depth >= self.found_from:
            return [dict(WIFI_ROW)]
        return []


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
    # On-screen marking asks the device for its viewport; not under test here.
    ctrl._mark_on_screen = AsyncMock()
    # A wait that times out describes the screen it gave up on; that reaches the
    # device's own tools, which no test here may touch.
    ctrl._identify_for_miss = AsyncMock(return_value={})
    return ctrl, wda


class TestTheLookup:
    async def test_a_miss_at_the_action_depth_is_found_deeper(self, phone):
        ctrl, wda = phone
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier="com.apple.settings.wifi",
            snapshot_depth=ACTION_SNAPSHOT_DEPTH, deepen_on_miss=True,
        )
        assert [e.identifier for e in elements] == ["com.apple.settings.wifi"]
        assert SNAPSHOT_MAX_DEPTH in wda.query_depths
        assert ctrl._deepened_to[PHONE] == SNAPSHOT_MAX_DEPTH

    async def test_it_is_a_query_never_a_deep_source(self, phone):
        """A deep /source on a 200-row table took 34.5s and its timeout restarts
        the runner; the retry must not be that read."""
        ctrl, wda = phone
        await ctrl.get_ui_elements(
            PHONE, filter_identifier="absent", snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        for call in ctrl.wda_client.describe_all.call_args_list:
            assert call.kwargs.get("snapshot_depth") != SNAPSHOT_MAX_DEPTH, (
                "the deeper look was a full /source read")

    async def test_a_deep_miss_puts_the_shallow_depth_back(self, phone):
        ctrl, wda = phone
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier="absent", snapshot_depth=ACTION_SNAPSHOT_DEPTH,
            deepen_on_miss=True,
        )
        assert elements == []
        assert wda.depths_set[-1] == ACTION_SNAPSHOT_DEPTH
        assert PHONE not in ctrl._deepened_to

    async def test_without_deepen_on_miss_it_stays_shallow(self, phone):
        ctrl, wda = phone
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_identifier="com.apple.settings.wifi",
            snapshot_depth=ACTION_SNAPSHOT_DEPTH,
        )
        assert elements == []
        assert SNAPSHOT_MAX_DEPTH not in wda.query_depths


class TestTheActions:
    async def test_get_element_finds_a_deeply_nested_element(self, phone):
        ctrl, wda = phone
        result, _ = await ctrl.get_element(identifier="com.apple.settings.wifi")
        assert result["identifier"] == "com.apple.settings.wifi"
        assert result["snapshot_depth"] == SNAPSHOT_MAX_DEPTH
        assert f"snapshot_depth={SNAPSHOT_MAX_DEPTH}" in result["depth_note"]

    async def test_a_depth_the_caller_chose_is_not_overridden(self, phone):
        ctrl, wda = phone
        with pytest.raises(DeviceError, match="No element found"):
            await ctrl.get_element(identifier="com.apple.settings.wifi", snapshot_depth=12)
        assert SNAPSHOT_MAX_DEPTH not in wda.query_depths

    async def test_wait_for_element_finds_it_and_says_how(self, phone):
        ctrl, wda = phone
        result, _ = await ctrl.wait_for_element(
            WaitCondition.EXISTS, identifier="com.apple.settings.wifi",
            timeout=1, interval=0.05,
        )
        assert result["matched"] is True
        assert result["snapshot_depth"] == SNAPSHOT_MAX_DEPTH

    async def test_a_wait_for_something_absent_deepens_once_not_every_poll(self, phone):
        """Each deep query can cost seconds on a long list; per poll, a wait
        would overrun its own timeout."""
        ctrl, wda = phone
        result, _ = await ctrl.wait_for_element(
            WaitCondition.EXISTS, identifier="absent", timeout=0.4, interval=0.05,
        )
        assert result["matched"] is False
        assert wda.query_depths.count(SNAPSHOT_MAX_DEPTH) <= 2  # one id query + its predicate retry
        assert wda.query_depths.count(ACTION_SNAPSHOT_DEPTH) > 2, "it stopped polling"
