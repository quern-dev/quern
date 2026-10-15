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

    def test_clearing_clears_only_the_callers_slot(self):
        ctrl = _controller()
        with as_session("mcp-aaaa"):
            ctrl._active_udid = "SIM-X"
            ctrl._active_udid = None
            assert ctrl._active_udid is None
        assert ctrl._active_udid == "SIM-X", "a session's clear wiped the shared slot"


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
