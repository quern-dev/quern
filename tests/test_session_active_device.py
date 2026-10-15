"""A per-session active device (#254).

The active device was one value for the whole server, so an agent omitting
`udid` got whichever device any agent resolved last. Now a named session reads
only its own slot; the shared slot keeps its old meaning -- the device anyone
selected last -- for callers with no session and for the menu bar.
"""

from __future__ import annotations

import contextlib
import json
import time
from unittest.mock import AsyncMock

import pytest

from server import session as session_mod
from server.device import controller as controller_mod
from server.device.controller import DeviceController
from server.lifecycle import state
from server.models import DeviceError, DeviceInfo, DeviceState, DeviceType


@contextlib.contextmanager
def as_session(name: str):
    token = session_mod._SESSION.set(name)
    try:
        yield
    finally:
        session_mod._SESSION.reset(token)


@pytest.fixture(autouse=True)
def _fresh_sidecar():
    state.ACTIVE_DEVICE_FILE.unlink(missing_ok=True)
    yield
    state.ACTIVE_DEVICE_FILE.unlink(missing_ok=True)


def _device(udid: str) -> DeviceInfo:
    return DeviceInfo(udid=udid, name=f"Sim {udid}", state=DeviceState.BOOTED,
                      device_type=DeviceType.SIMULATOR, os_version="iOS 18.6",
                      runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-6")


def _controller(booted=("SIM-X", "SIM-Y")) -> DeviceController:
    ctrl = DeviceController()
    ctrl.simctl.list_devices = AsyncMock(return_value=[_device(u) for u in booted])
    ctrl.devicectl.list_devices = AsyncMock(return_value=[])
    ctrl.usbmux.list_devices = AsyncMock(return_value=[])
    ctrl.adb.list_devices = AsyncMock(return_value=[])
    return ctrl


class TestIsolation:
    def test_each_session_reads_its_own_device(self):
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        with as_session("mcp-bbbb"):
            ctrl._active_udid = "SIM-Y"
        with as_session("mcp-aaaa"):
            assert ctrl._active_udid == "SIM-X", "B's selection redirected A"
        with as_session("mcp-bbbb"):
            assert ctrl._active_udid == "SIM-Y"
        # The shared slot keeps its old meaning: the last selection by anyone.
        assert ctrl._active_udid == "SIM-Y"

    def test_a_session_with_no_slot_never_inherits_the_shared_device(self):
        ctrl = _controller()
        ctrl._active_udid = "SIM-X"          # an unattributed caller selects
        with as_session("mcp-new"):
            assert ctrl._active_udid is None

    def test_using_a_device_does_not_move_the_shared_slot(self):
        """`_resolve_udid` re-assigns the active device on every unqualified
        call. If that moved the shared slot, it -- and the menu bar -- would
        flip between agents' devices on every call."""
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        with as_session("mcp-bbbb"):
            ctrl._active_udid = "SIM-Y"
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"      # A keeps using X
        assert ctrl._active_udid == "SIM-Y"

    def test_clearing_follows_the_device_not_the_caller(self):
        """A clear means the device went away -- shut down, erased. The shared
        slot follows when it pointed at that device (otherwise the menu bar
        showed a dead simulator), and is left alone when it pointed elsewhere."""
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"          # shared slot = X too
            ctrl._active_udid = None
            assert ctrl._active_udid is None
        assert ctrl._active_udid is None, "the shared slot kept a device that went away"

        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        with as_session("mcp-bbbb"):
            ctrl._active_udid = "SIM-Y"          # shared slot = Y
        with as_session("mcp-aaaa"):
            ctrl._active_udid = None
        assert ctrl._active_udid == "SIM-Y", "A's clear wiped another agent's device"


class TestResolution:
    async def test_two_agents_two_simulators_no_udid(self):
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            assert await ctrl.resolve_udid("SIM-X") == "SIM-X"
        with as_session("mcp-bbbb"):
            assert await ctrl.resolve_udid("SIM-Y") == "SIM-Y"
        with as_session("mcp-aaaa"):
            assert await ctrl.resolve_udid() == "SIM-X"
        with as_session("mcp-bbbb"):
            assert await ctrl.resolve_udid() == "SIM-Y"

    async def test_a_fresh_session_is_asked_rather_than_handed_a_device(self):
        """Two simulators booted and no slot: what a fresh server does --
        'specify udid' -- not whichever device someone else chose."""
        ctrl = _controller()
        await ctrl.resolve_udid("SIM-X")      # shared slot = X
        with as_session("mcp-new"), pytest.raises(DeviceError, match="specify udid"):
            await ctrl.resolve_udid()

    async def test_unattributed_callers_still_share_one_device(self):
        ctrl = _controller()
        await ctrl.resolve_udid("SIM-X")
        assert await ctrl.resolve_udid() == "SIM-X"


class TestPersistence:
    def test_slots_survive_a_restart(self):
        ctrl = _controller()
        with as_session("claude-1256bb48"):
            ctrl._active_udid = "SIM-X"
        restarted = _controller()
        with as_session("claude-1256bb48"):
            assert restarted._active_udid == "SIM-X"

    def test_the_menu_bar_still_sees_the_shared_device(self):
        """It reads the top-level udid/name/type and ignores `sessions`."""
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        data = json.loads(state.ACTIVE_DEVICE_FILE.read_text())
        assert data["udid"] == "SIM-X"
        assert data["sessions"]["mcp-aaaa"]["udid"] == "SIM-X"

    def test_expired_slots_are_not_restored(self, monkeypatch):
        old = time.time() - controller_mod.SESSION_SLOT_TTL - 60
        state.write_active_udid("SIM-X", sessions={
            "mcp-old": {"udid": "SIM-X", "last_used": old},
            "mcp-live": {"udid": "SIM-Y", "last_used": time.time()},
        })
        ctrl = _controller()
        # Not restored at all -- not merely hidden by the getter's own expiry
        # check, which a read alone can't tell apart. Kept, an expired slot
        # would hold a cap place and be written back to the file.
        assert "mcp-old" not in ctrl._DeviceController__session_slots
        with as_session("mcp-old"):
            assert ctrl._active_udid is None
        with as_session("mcp-live"):
            assert ctrl._active_udid == "SIM-Y"

    def test_malformed_slots_are_dropped(self):
        state.write_active_udid("SIM-X", sessions={
            "bad name": {"udid": "SIM-X", "last_used": time.time()},
            "mcp-nodict": "SIM-X",
            "mcp-noudid": {"last_used": time.time()},
            "mcp-ok": {"udid": "SIM-Y", "last_used": time.time()},
        })
        ctrl = _controller()
        assert list(ctrl._DeviceController__session_slots) == ["mcp-ok"]

    def test_the_cap_keeps_the_most_recently_used(self):
        now = time.time()
        cap = controller_mod.SESSION_SLOT_CAP
        state.write_active_udid("SIM-X", sessions={
            f"mcp-{i:04d}": {"udid": "SIM-X", "last_used": now - i} for i in range(cap + 10)
        })
        ctrl = _controller()
        slots = ctrl._DeviceController__session_slots
        assert len(slots) == cap
        assert "mcp-0000" in slots and f"mcp-{cap + 9:04d}" not in slots


class TestExpiryInMemory:
    def test_an_unused_slot_lapses(self, monkeypatch):
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        later = time.time() + controller_mod.SESSION_SLOT_TTL + 60
        monkeypatch.setattr(controller_mod.time, "time", lambda: later)
        with as_session("mcp-aaaa"):
            assert ctrl._active_udid is None


class TestThroughHTTP:
    def test_set_active_is_per_session_over_http(self):
        """Through the session middleware and the real routes, not the property
        alone: a direct call can't show a header failing to reach it."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from server.api.device import router
        from server.session import SessionMiddleware

        app = FastAPI()
        app.add_middleware(SessionMiddleware)
        app.include_router(router)
        app.state.device_controller = _controller()

        def as_(name):
            return {"X-Quern-Session": name} if name else {}

        with TestClient(app) as client:
            assert client.post("/api/v1/device/active", json={"udid": "SIM-X"},
                               headers=as_("mcp-aaaa")).json() == {"active_udid": "SIM-X"}
            assert client.post("/api/v1/device/active", json={"udid": "SIM-Y"},
                               headers=as_("mcp-bbbb")).json() == {"active_udid": "SIM-Y"}
            got = {name: client.get("/api/v1/device/list", headers=as_(name)).json()["active_udid"]
                   for name in ("mcp-aaaa", "mcp-bbbb", "mcp-cccc", "")}
        assert got == {"mcp-aaaa": "SIM-X", "mcp-bbbb": "SIM-Y",
                       "mcp-cccc": None, "": "SIM-Y"}


class TestTheRealServerHasAPool:
    """The real server always attaches a DevicePool, which the tests above
    don't. With no criteria the pool silently picks a ranked device -- live, it
    picked a USB iPhone over two simulators -- so a fresh session must not
    reach it, or "never handed someone else's device" becomes "handed an
    arbitrary one", possibly real hardware."""

    @staticmethod
    def _with_pool(ctrl):
        class _Pool:
            calls = 0

            async def resolve_device(self, **_kw):
                _Pool.calls += 1
                return "PHYSICAL-PHONE"

        ctrl._pool = _Pool()
        return ctrl._pool

    async def test_a_fresh_session_is_asked_even_with_a_pool(self):
        ctrl = _controller()
        pool = self._with_pool(ctrl)
        with as_session("mcp-new"), pytest.raises(DeviceError, match="specify udid"):
            await ctrl.resolve_udid()
        assert pool.calls == 0

    async def test_a_fresh_session_with_one_device_booted_uses_it(self):
        ctrl = _controller(booted=("SIM-X",))
        self._with_pool(ctrl)
        with as_session("mcp-new"):
            assert await ctrl.resolve_udid() == "SIM-X"

    async def test_an_unattributed_caller_still_gets_the_pools_choice(self):
        ctrl = _controller()
        pool = self._with_pool(ctrl)
        assert await ctrl.resolve_udid() == "PHYSICAL-PHONE"
        assert pool.calls == 1


class TestPersistenceEdges:
    """Found by review: paths where a session's choice, or the shared device,
    failed to reach the file correctly."""

    def test_a_session_write_before_the_refresh_keeps_the_shared_device(self):
        """After a restart the shared device is restored but the setter's
        name cache was not, so a session's write -- before the startup refresh,
        which skips devices the caches don't know -- wrote no shared device."""
        two_hours_ago = time.time() - 2 * 3600
        state.write_active_udid("SIM-X", "Sim X", "simulator", sessions={
            "mcp-aaaa": {"udid": "SIM-Y", "last_used": two_hours_ago}})
        ctrl = _controller()                 # no refresh_active_device()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-Y"      # due its hourly rewrite
        data = json.loads(state.ACTIVE_DEVICE_FILE.read_text())
        assert (data.get("udid"), data.get("name"), data.get("type")) == (
            "SIM-X", "Sim X", "simulator"), data
        assert data["sessions"]["mcp-aaaa"]["last_used"] > two_hours_ago

    def test_switching_to_the_shared_device_is_saved(self):
        """The shared slot's dedup guard saw nothing new and skipped the write,
        so after a restart the session acted on its old device."""
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-Y"
        with as_session("mcp-bbbb"):
            ctrl._active_udid = "SIM-X"      # shared = X
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"      # A switches to the shared device
        with as_session("mcp-aaaa"):
            assert _controller()._active_udid == "SIM-X"

    def test_a_fresh_session_choosing_the_shared_device_is_saved(self):
        ctrl = _controller()
        ctrl._active_udid = "SIM-X"          # an unattributed caller
        with as_session("mcp-new"):
            ctrl._active_udid = "SIM-X"
        with as_session("mcp-new"):
            assert _controller()._active_udid == "SIM-X"

    def test_a_slot_in_use_is_rewritten_hourly(self, monkeypatch):
        clock = {"t": time.time()}
        monkeypatch.setattr(controller_mod.time, "time", lambda: clock["t"])
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        def on_disk():
            return json.loads(state.ACTIVE_DEVICE_FILE.read_text())["sessions"]["mcp-aaaa"]

        first = on_disk()["last_used"]
        clock["t"] += controller_mod.SESSION_SLOT_REWRITE + 60
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"      # same device, just in use
        assert on_disk()["last_used"] > first

    def test_reading_a_slot_keeps_it_alive(self, monkeypatch):
        clock = {"t": time.time()}
        monkeypatch.setattr(controller_mod.time, "time", lambda: clock["t"])
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
        step = controller_mod.SESSION_SLOT_TTL * 0.8
        for _ in range(3):                   # 2.4 TTLs in all, read each time
            clock["t"] += step
            with as_session("mcp-aaaa"):
                assert ctrl._active_udid == "SIM-X"

    def test_a_non_finite_timestamp_is_dropped(self):
        # Written by hand: json.dumps would refuse Infinity, which is the point.
        now = time.time()
        state.ACTIVE_DEVICE_FILE.write_text(
            '{"udid": "SIM-X", "sessions": {"mcp-inf": {"udid": "SIM-X", "last_used": Infinity},'
            f' "mcp-ok": {{"udid": "SIM-Y", "last_used": {now}}}}}}}')
        assert list(_controller()._DeviceController__session_slots) == ["mcp-ok"]
