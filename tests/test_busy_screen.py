"""A screen too busy to read over the push in time (#769).

Measured on an iPhone 12, a map: with ~119 pins the device sent its first
InitialDump 4.4 s after subscribing and its complete tree 7.9 s after the
newest, three of three. The per-element budget gave up at 5.6 s and
resubscribed, which started the phone over, so no read of it ever finished;
the fallback then started a WDA that could not read it either. Zoomed out to
415 elements, no complete tree came in 45 s.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.api import device as device_api
from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import (
    PUSH_SELECTOR,
    PushStalled,
    ScreenTooBusy,
    Tree,
    read_tree,
)
from server.models import DeviceError
from tests.test_coredevice_ax import PHONE, SETTINGS, _as, _phone_controller

INITIAL = _as("InitialDump", SETTINGS)


def _queue_later(*timed):
    """A queue each (delay, push) lands on that long after the call."""
    q: asyncio.Queue = asyncio.Queue()

    async def feed():
        for delay, push in timed:
            await asyncio.sleep(delay)
            q.put_nowait((PUSH_SELECTOR, [push]))

    asyncio.get_running_loop().create_task(feed())
    return q


@pytest.fixture
def fast_budget(monkeypatch):
    """The per-element budget and its floor made tiny, so the device's own
    pace is what the window comes from."""
    monkeypatch.setattr(coredevice_ax, "STALL_MIN_S", 0.1)
    monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.02)
    monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)


class TestTheDevicesOwnPace:
    async def test_a_slow_screens_tree_is_waited_for_at_its_pace(self, fast_budget):
        """Its InitialDump took 0.4 s to come, so its pair gets 2.5 x that."""
        loop = asyncio.get_running_loop()
        since = loop.time() - 0.4           # subscribed 0.4 s before this InitialDump
        q = _queue_later((0.0, INITIAL), (0.6, SETTINGS))
        tree, _, _ = await asyncio.wait_for(
            coredevice_ax._newest_tree(q, {}, since=since), 5)
        assert tree.dump_type == "AdditionalData"

    async def test_a_684_stall_is_still_given_up_on_quickly(self, fast_budget):
        """InitialDumps that come at once and then nothing: the window is the
        floor, as before, not stretched."""
        loop = asyncio.get_running_loop()
        q = _queue_later((0.0, INITIAL))
        started = loop.time()
        with pytest.raises(PushStalled):
            await asyncio.wait_for(
                coredevice_ax._newest_tree(q, {}, since=loop.time()), 5)
        assert loop.time() - started < 0.5

    async def test_the_pace_is_timed_from_the_last_initial_dump(self, fast_budget):
        """A slow first InitialDump, then a second at once and nothing: the
        quick one is the pace, so it stalls quickly, as #684's do. Timed from
        the subscription instead, the window would stay long."""
        loop = asyncio.get_running_loop()
        since = loop.time() - 0.4
        q = _queue_later((0.0, INITIAL), (0.05, INITIAL))
        started = loop.time()
        with pytest.raises(PushStalled):
            await asyncio.wait_for(coredevice_ax._newest_tree(q, {}, since=since), 5)
        assert loop.time() - started < 0.6


class TestTooBusyToRead:
    @staticmethod
    def _still_describing(elements: int):
        """`_settled` that has the screen's InitialDump in hand and never
        gets its complete tree."""
        big = Tree(dump_type="InitialDump", app_pid=711,
                   elements=[{"AXLabel": "Geocaching", "type": "Application"}]
                   + [{"AXLabel": f"pin {i}", "type": "Button"} for i in range(elements - 1)])

        async def settled(uuid, lib, address, conn, held):
            held.update(first=big, newest=big)
            await asyncio.sleep(60)

        return settled

    async def test_a_big_screen_still_building_at_the_deadline_says_so(
            self, monkeypatch, phone_address):
        monkeypatch.setattr(coredevice_ax, "_settled", self._still_describing(415))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: object())
        with pytest.raises(ScreenTooBusy, match="415 elements") as caught:
            await read_tree(PHONE, timeout=0.3)
        assert "zoom a map in" in str(caught.value)
        assert caught.value.elements == 415

    async def test_a_small_screen_that_times_out_is_not_called_busy(
            self, monkeypatch, phone_address):
        monkeypatch.setattr(coredevice_ax, "_settled", self._still_describing(20))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: object())
        with pytest.raises(DeviceError, match="sent no complete") as caught:
            await read_tree(PHONE, timeout=0.3)
        assert not isinstance(caught.value, ScreenTooBusy)


@pytest.fixture
def phone_address(monkeypatch):
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(return_value=("fd00::1", 51000)))


class TestNoWdaForIt:
    async def test_a_busy_screen_is_not_read_through_wda(self, monkeypatch):
        """WDA's /source timed out on the same map, and starting it began the
        auto-start loop (#769)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=ScreenTooBusy(PHONE, 415, 20))

        with pytest.raises(ScreenTooBusy, match="415 elements"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.wda_client.describe_all.assert_not_called()


class TestTheScreenContextIsBounded:
    async def test_one_that_runs_long_says_so_and_returns(self, monkeypatch):
        monkeypatch.setattr(device_api, "SCREEN_CONTEXT_S", 0.2)

        async def slow(**kwargs):
            await asyncio.sleep(60)

        controller = AsyncMock()
        controller.get_screen_summary = slow
        context = await asyncio.wait_for(
            device_api._capture_screen_context(controller, PHONE), 5)
        assert "within 0.2s" in context["error"]
        assert "the action itself was done" in context["error"]

    async def test_one_that_fails_says_why(self):
        controller = AsyncMock()
        controller.get_screen_summary = AsyncMock(side_effect=ScreenTooBusy(PHONE, 415, 20))
        context = await device_api._capture_screen_context(controller, PHONE)
        assert "415 elements" in context["error"]

    async def test_one_that_works_has_no_error(self):
        controller = AsyncMock()
        controller.get_screen_summary = AsyncMock(
            return_value=({"summary": "a screen", "element_count": 3}, [], None))
        context = await device_api._capture_screen_context(controller, PHONE)
        assert "error" not in context and context["summary"] == "a screen"
