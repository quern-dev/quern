"""Induced conditions on a physical iPhone (#245, phase 6).

The fixture is a real listing from an iPhone 11 (iOS 26.7). The fake
inducer models what was measured on it: a network profile ends when the
connection that set it closes, a thermal state does not, and a clear lands
only if the connection is held open after it. A first version modelled a
network profile outliving its connection, and passed every test while the
real phone dropped each condition a second after it was set.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import config
from server.device.ios import conditions as cond
from server.models import DeviceError

UDID = "00008030-000C59623A69802E"
FIXTURE = Path(__file__).parent / "fixtures" / "conditions" / "iphone11-ios26.7-list.json"


class Phone:
    """The device's state, which outlives every connection to it."""

    def __init__(self):
        self.groups = json.loads(FIXTURE.read_text())
        self.clear_after = 2        # reads on the same connection before a clear lands
        self.clears_never = False
        self.set_never = False
        self.set_error: Exception | None = None
        self.connections = 0
        self.order: list[str] = []
        self.set_on = None          # the connection that set what is active
        self.list_error: Exception | None = None

    def active(self):
        return next(((g["identifier"], g["activeProfile"]) for g in self.groups
                     if g["isActive"]), None)

    def enable(self, profile):
        for g in self.groups:
            g["isActive"], g["activeProfile"] = False, ""
        for g in self.groups:
            if any(p["identifier"] == profile for p in g["profiles"]):
                g["isActive"], g["activeProfile"] = True, profile

    def disable(self):
        for g in self.groups:
            g["isActive"], g["activeProfile"] = False, ""


class FakeInducer:
    def __init__(self, phone: Phone):
        self.phone = phone
        self.pending_clear: int | None = None

    async def __aenter__(self):
        self.phone.connections += 1
        return self

    async def __aexit__(self, *exc):
        # Closed before the clear landed: it is dropped, as measured.
        self.pending_clear = None
        # A network profile ends with the connection that set it (measured).
        active = self.phone.active()
        if self.phone.set_on is self and active and active[0] == "SlowNetworkCondition":
            self.phone.disable()
        return False

    async def list(self):
        if self.phone.list_error:
            raise self.phone.list_error
        if self.pending_clear is not None:
            self.pending_clear -= 1
            if self.pending_clear <= 0 and not self.phone.clears_never:
                self.phone.disable()
                self.pending_clear = None
        return json.loads(json.dumps(self.phone.groups))

    async def set(self, profile):
        self.phone.order.append("set")
        if self.phone.set_error:
            raise self.phone.set_error
        if not self.phone.set_never:
            self.phone.enable(profile)
            self.phone.set_on = self

    async def clear(self):
        self.pending_clear = self.phone.clear_after


class FakeService:
    def __init__(self, *_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeRsd:
    def __init__(self, address):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass


@pytest.fixture
def phone(monkeypatch, tmp_path):
    p = Phone()
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cond, "_POLL_S", 0.0)
    monkeypatch.setattr(cond, "CONFIRM_S", 0.5)
    from server.device import performance
    monkeypatch.setattr(performance, "_device_address", AsyncMock(return_value=("fd::1", 1)))
    monkeypatch.setattr(cond, "_pmd3", lambda: SimpleNamespace(
        RemoteServiceDiscoveryService=FakeRsd, DvtProvider=FakeService,
        ConditionInducer=lambda dvt: FakeInducer(p)))
    return p


def keeper() -> cond.ConditionKeeper:
    return cond.ConditionKeeper()


# -- reading -------------------------------------------------------------------

class TestParse:
    def test_the_real_listing(self):
        offered, active = cond.parse_groups(json.loads(FIXTURE.read_text()))
        assert active is None
        kinds = {c.kind for c in offered}
        assert kinds == {"network", "thermal", "gpu"}
        assert len([c for c in offered if c.kind == "network"]) == 16
        three_g = next(c for c in offered if c.profile == "SlowNetwork3G")
        assert three_g.group == "SlowNetworkCondition" and "0.78 Mbps" in three_g.description

    def test_the_active_one(self):
        groups = json.loads(FIXTURE.read_text())
        groups[1].update(isActive=True, activeProfile="ThermalFair")
        _, active = cond.parse_groups(groups)
        assert (active.profile, active.kind) == ("ThermalFair", "thermal")

    def test_active_with_a_profile_it_does_not_list_is_still_active(self):
        """Never read as none: that is the phone silently throttled."""
        groups = json.loads(FIXTURE.read_text())
        groups[0].update(isActive=True, activeProfile="SlowNetworkSomethingNew")
        _, active = cond.parse_groups(groups)
        assert active is not None and active.profile == "SlowNetworkSomethingNew"


# -- setting -------------------------------------------------------------------

class TestSet:
    async def test_a_network_condition_confirmed_by_the_device(self, phone):
        out = await keeper().set(UDID, "SlowNetwork3G", allow_performance=False,
                                 principal="client:a", session="s1")
        assert phone.active() == ("SlowNetworkCondition", "SlowNetwork3G")
        assert (out.active.profile, out.active.set_by) == ("SlowNetwork3G", "quern")
        record = cond.ConditionRecords().get(UDID)
        assert (record["profile"], record["principal"], record["session"]) == (
            "SlowNetwork3G", "client:a", "s1")

    async def test_recorded_before_the_device_is_asked(self, phone):
        """A server killed between the two still leaves a record to clear."""
        seen = []
        orig = FakeInducer.set

        async def spy(self, profile):
            seen.append(cond.ConditionRecords().get(UDID))
            await orig(self, profile)
        FakeInducer.set = spy
        try:
            await keeper().set(UDID, "SlowNetworkEdge", allow_performance=False,
                               principal=None, session=None)
        finally:
            FakeInducer.set = orig
        assert seen and seen[0]["profile"] == "SlowNetworkEdge"

    async def test_thermal_needs_allow_performance(self, phone):
        with pytest.raises(ValueError, match="allow_performance"):
            await keeper().set(UDID, "ThermalSerious", allow_performance=False,
                               principal=None, session=None)
        assert phone.active() is None and phone.order == []
        assert cond.ConditionRecords().get(UDID) is None
        await keeper().set(UDID, "ThermalSerious", allow_performance=True,
                           principal=None, session=None)
        assert phone.active() == ("ThermalCondition", "ThermalSerious")

    async def test_an_unknown_profile_names_the_real_ones(self, phone):
        with pytest.raises(ValueError, match="SlowNetwork3G"):
            await keeper().set(UDID, "SlowNetwork5G", allow_performance=False,
                               principal=None, session=None)
        assert phone.order == []

    async def test_replacing_one_set_elsewhere_says_so(self, phone):
        phone.enable("SlowNetworkLTE")              # set in Xcode, say
        out = await keeper().set(UDID, "SlowNetwork3G", allow_performance=False,
                                 principal=None, session=None)
        assert (out.replaced.profile, out.replaced.set_by) == ("SlowNetworkLTE", "other")

    async def test_a_set_the_device_never_shows_fails(self, phone):
        """Reported as the outcome, not the request (CONTRIBUTING)."""
        phone.set_never = True
        with pytest.raises(DeviceError, match="did not report SlowNetwork3G active"):
            await keeper().set(UDID, "SlowNetwork3G", allow_performance=False,
                               principal=None, session=None)
        # Nothing is active, so nothing of quern's is left to clear.
        assert cond.ConditionRecords().get(UDID) is None

    async def test_a_set_that_fails_after_landing_is_undone(self, phone):
        """On the connection that set it, which this call opened: nothing of
        a failed set is left on the phone, or in the records."""
        phone.set_error = RuntimeError("the channel closed")
        phone.enable("ThermalFair")                 # it landed before failing
        with pytest.raises(DeviceError, match="channel closed"):
            await keeper().set(UDID, "ThermalFair", allow_performance=True,
                               principal=None, session=None)
        assert phone.active() is None
        assert cond.ConditionRecords().get(UDID) is None

    async def test_a_failed_set_whose_undo_fails_keeps_the_record(self, phone):
        phone.set_error = RuntimeError("the channel closed")
        phone.enable("ThermalFair")
        phone.clears_never = True
        with pytest.raises(DeviceError):
            await keeper().set(UDID, "ThermalFair", allow_performance=True,
                               principal=None, session=None)
        assert cond.ConditionRecords().get(UDID)["profile"] == "ThermalFair"


# -- holding -------------------------------------------------------------------

class TestHold:
    async def test_a_network_condition_lasts_while_held(self, phone):
        """The bug a set-and-close had: confirmed active, gone a second later."""
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        await asyncio.sleep(0.05)
        assert phone.active() == ("SlowNetworkCondition", "SlowNetwork3G")
        assert (await k.describe(UDID)).active.profile == "SlowNetwork3G"
        await k.clear(UDID)
        assert phone.active() is None and k._live(UDID) is None

    async def test_one_connection_for_set_read_and_clear(self, phone):
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        held = phone.connections
        await k.describe(UDID)
        await k.set(UDID, "SlowNetworkEdge", allow_performance=False, principal=None,
                    session=None)
        assert phone.connections == held
        assert phone.active() == ("SlowNetworkCondition", "SlowNetworkEdge")

    async def test_cleared_elsewhere_is_noticed_and_said(self, phone, monkeypatch):
        monkeypatch.setattr(cond, "WATCH_S", 0.02)
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        phone.disable()                              # Xcode, say
        for _ in range(50):
            if k._live(UDID) is None:
                break
            await asyncio.sleep(0.02)
        out = await k.describe(UDID)
        assert out.active is None and "cleared elsewhere" in out.note
        assert cond.ConditionRecords().get(UDID) is None

    async def test_a_held_connection_that_fails_is_said(self, phone, monkeypatch):
        monkeypatch.setattr(cond, "WATCH_S", 0.02)
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        phone.list_error = ConnectionResetError("tunnel gone")
        for _ in range(50):
            if k._live(UDID) is None:
                break
            await asyncio.sleep(0.02)
        assert k._live(UDID) is None
        phone.list_error = None
        phone.disable()
        assert "tunnel gone" in (await k.describe(UDID)).note

    async def test_shutdown_clears_what_is_held(self, phone):
        k = keeper()
        await k.set(UDID, "ThermalFair", allow_performance=True, principal=None, session=None)
        results = await k.clear_all()
        assert [r["ok"] for r in results] == [True] and phone.active() is None


class TestLeftovers:
    async def test_only_what_a_previous_server_left(self, phone):
        """Start-up clears in the background, after requests are served: a
        record this server wrote is its own, not a leftover (measured: the
        first version dropped a condition set a second after start)."""
        k = keeper()                                  # this server, starting
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        assert await k.clear_leftovers() == []
        assert phone.active() == ("SlowNetworkCondition", "SlowNetwork3G")
        assert cond.ConditionRecords().get(UDID) is not None
        await k.clear(UDID)

    async def test_a_previous_servers_record(self, phone):
        cond.ConditionRecords().put(UDID, {"profile": "ThermalFair", "session": None,
                                           "principal": "owner"})
        phone.enable("ThermalFair")                  # an OS that kept it
        results = await keeper().clear_leftovers()
        assert [r["ok"] for r in results] == [True] and phone.active() is None


# -- clearing ------------------------------------------------------------------

class TestClear:
    async def test_held_open_until_the_device_says_so(self, phone):
        """A clear lands only while the connection stays open (measured)."""
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        cleared = await k.clear(UDID)
        assert cleared.profile == "SlowNetwork3G" and phone.active() is None
        assert cond.ConditionRecords().get(UDID) is None

    async def test_a_clear_that_never_lands_fails_and_keeps_the_record(self, phone):
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None,
                    session=None)
        phone.clears_never = True
        with pytest.raises(DeviceError, match="still reports SlowNetwork3G active"):
            await k.clear(UDID)
        assert cond.ConditionRecords().get(UDID) is not None

    async def test_closing_at_once_would_drop_it(self, phone):
        """The fake's own guard: a clear not read back is not applied, so a
        test above passing means the code held the connection open."""
        phone.enable("SlowNetwork3G")

        async def clear_and_close(inducer):
            await inducer.clear()
        await cond._with_inducer(UDID, "clearing", clear_and_close)
        assert phone.active() is not None

    async def test_nothing_active_is_nothing_cleared(self, phone):
        assert await keeper().clear(UDID) is None


class TestClearRecorded:
    async def _set(self, phone, session="s1", principal="client:a"):
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=principal,
                    session=session)
        return k

    async def test_the_session_that_set_it(self, phone):
        k = await self._set(phone)
        action = await k.clear_recorded(UDID, session="s1", principal="client:a")
        assert action == {"udid": UDID, "action": "condition_cleared",
                          "profile": "SlowNetwork3G", "ok": True}
        assert phone.active() is None

    async def test_the_same_key_through_another_session(self, phone):
        k = await self._set(phone)
        assert (await k.clear_recorded(UDID, session="s2", principal="client:a"))["ok"]

    async def test_someone_elses_is_left(self, phone):
        k = await self._set(phone)
        assert await k.clear_recorded(UDID, session="s9", principal="client:b") is None
        assert phone.active() is not None

    async def test_replaced_in_xcode_is_not_undone(self, phone):
        k = await self._set(phone)
        phone.enable("SlowNetworkLTE")
        action = await k.clear_recorded(UDID)
        assert action["ok"] and action["already"] == "replaced"
        assert phone.active() == ("SlowNetworkCondition", "SlowNetworkLTE")
        assert cond.ConditionRecords().get(UDID) is None

    async def test_cleared_by_hand_already(self, phone):
        k = await self._set(phone)
        phone.disable()
        assert (await k.clear_recorded(UDID))["already"] == "cleared"

    async def test_a_failure_is_reported_and_the_record_kept(self, phone):
        k = await self._set(phone)
        phone.clears_never = True
        action = await k.clear_recorded(UDID)
        assert action["ok"] is False and "still reports" in action["error"]
        assert cond.ConditionRecords().get(UDID) is not None

    async def test_clear_all_at_shutdown(self, phone):
        await self._set(phone)
        results = await cond.ConditionKeeper().clear_all()
        assert [r["ok"] for r in results] == [True] and phone.active() is None


class TestRecords:
    def test_an_unreadable_file_is_never_overwritten(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        (tmp_path / "device-conditions.json").write_text("{not json")
        records = cond.ConditionRecords()
        assert records.all() == {}
        with pytest.raises(cond._Unreadable):
            records.put(UDID, {"profile": "x"})
        assert (tmp_path / "device-conditions.json").read_text() == "{not json"

    def test_the_last_one_dropped_removes_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        records = cond.ConditionRecords()
        records.put(UDID, {"profile": "x"})
        records.drop(UDID)
        assert not (tmp_path / "device-conditions.json").exists()


# -- the hold ending -----------------------------------------------------------

class TestRelease:
    async def _release(self, monkeypatch, k, *, principal="client:a", held_by=None):
        from server import session_installs
        from server.api import sessions as sessions_api

        monkeypatch.setattr(sessions_api, "_end_device_proxy", AsyncMock())
        monkeypatch.setattr(session_installs, "end_for_release", AsyncMock())
        app = SimpleNamespace(state=SimpleNamespace(
            landmark_registry=None, proxy_adapter=None, recordings=None, mock_owners={},
            conditions=k))
        session = SimpleNamespace(id="s1", principal=principal, reservations={UDID: None},
                                  opened_at=None)
        registry = SimpleNamespace(holder_of=lambda u: held_by)
        return await sessions_api._end_client_work(app, registry, session)

    async def test_the_hold_ending_clears_it(self, phone, monkeypatch):
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal="client:a",
                    session="s1")
        actions = await self._release(monkeypatch, k)
        assert {"udid": UDID, "action": "condition_cleared", "profile": "SlowNetwork3G",
                "ok": True} in actions
        assert phone.active() is None

    async def test_a_failed_clear_keeps_the_device_back(self, phone, monkeypatch):
        """ok: false is what keeps the device unreservable and retried."""
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal="client:a",
                    session="s1")
        phone.clears_never = True
        actions = await self._release(monkeypatch, k)
        [a] = [a for a in actions if a.get("action") == "condition_cleared"]
        assert a["ok"] is False and a["udid"] == UDID

    async def test_still_held_by_the_same_key_it_stays(self, phone, monkeypatch):
        k = keeper()
        await k.set(UDID, "SlowNetwork3G", allow_performance=False, principal="client:a",
                    session="s1")
        other = SimpleNamespace(session=SimpleNamespace(principal="client:a"))
        actions = await self._release(monkeypatch, k, held_by=other)
        assert not [a for a in actions if a.get("action") == "condition_cleared"]
        assert phone.active() is not None


async def test_concurrent_sets_take_turns(phone):
    """One at a time per device: two interleaved sets would each confirm
    against the other's state."""
    k = keeper()
    await asyncio.gather(
        k.set(UDID, "SlowNetwork3G", allow_performance=False, principal=None, session=None),
        k.set(UDID, "SlowNetworkEdge", allow_performance=False, principal=None, session=None))
    assert phone.active()[1] in ("SlowNetwork3G", "SlowNetworkEdge")
    assert cond.ConditionRecords().get(UDID)["profile"] == phone.active()[1]
