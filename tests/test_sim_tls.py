"""Local capture decrypts only simulators known to trust the CA (#354).

A simulator that does not trust the mitmproxy CA fails every HTTPS request the
proxy terminates, and local capture spans every simulator on the Mac. So TLS
from a simulator is decrypted only when the server has confirmed that
simulator trusts the CA, and passed through untouched otherwise.

The property all of this protects is directional. Trust changes under a running
proxy -- a simulator boots, is created, or is erased -- so the answer is often
stale, and stale has to mean "passed through", never "decrypted". The tests
below are mostly about that: nothing that cannot confirm trust may produce
decryption, whether it is an unchecked simulator, a failed check, an unknown
UDID, or a previous set.
"""

from __future__ import annotations

import ctypes
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.proxy import addon as addon_mod
from server.proxy.addon import IOSDebugAddon
from tests.test_addon_intercept import CapturedOutput

A = "AAAAAAAA-0000-0000-0000-000000000001"
B = "BBBBBBBB-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# The addon
# ---------------------------------------------------------------------------


class TestTheHandoverDefaultsToTrustingNobody:
    def test_the_server_and_addon_agree_on_the_variable(self):
        """Spelled out on both sides, because importing the addon into the
        server would run its monkey-patch there too."""
        from server.sources.proxy import TRUSTED_SIMULATORS_ENV

        assert TRUSTED_SIMULATORS_ENV == addon_mod.TRUSTED_SIMULATORS_ENV

    def test_unset_trusts_nobody(self):
        """An addon started without being told must pass every simulator
        through. Defaulting to "decrypt" would break every untrusting one."""
        assert addon_mod._parse_trusted_simulators(None) == frozenset()

    def test_star_decrypts_everything(self):
        assert addon_mod._parse_trusted_simulators("*") is None

    def test_a_list_is_normalised(self):
        assert addon_mod._parse_trusted_simulators(f" {A.lower()}, ,{B}") == {A, B}

    def test_an_empty_list_trusts_nobody_rather_than_everybody(self):
        assert addon_mod._parse_trusted_simulators("") == frozenset()


class TestWhichConnectionsArePassedThrough:
    @pytest.fixture
    def addon(self, monkeypatch):
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset({A})
        monkeypatch.setitem(addon_mod._client_process_info, "c1", {"pid": 4242})
        return a

    def _client(self, cid="c1", sni="api.example.com"):
        return SimpleNamespace(id=cid, sni=sni)

    def _from(self, monkeypatch, udid):
        monkeypatch.setattr(addon_mod, "_simulator_for_pid_fast", lambda pid: udid)

    def test_a_trusted_simulator_is_decrypted(self, addon, monkeypatch):
        self._from(monkeypatch, A)
        assert addon._untrusted_simulator(self._client()) is None

    def test_an_untrusted_simulator_is_passed_through(self, addon, monkeypatch):
        self._from(monkeypatch, B)
        assert addon._untrusted_simulator(self._client()) == B

    def test_an_unidentified_simulator_is_passed_through(self, addon, monkeypatch):
        """Known to be a simulator, UDID not cached yet. Decrypting on a guess
        is exactly the mistake the trusted list exists to rule out."""
        self._from(monkeypatch, addon_mod.UNKNOWN_SIMULATOR)
        assert addon._untrusted_simulator(self._client()) == addon_mod.UNKNOWN_SIMULATOR

    def test_a_mac_process_is_left_alone(self, addon, monkeypatch):
        self._from(monkeypatch, None)
        assert addon._untrusted_simulator(self._client()) is None

    def test_a_connection_with_no_pid_keeps_todays_behaviour(self, addon, monkeypatch):
        """A device using the proxy, or a socket lookup still pending. The
        system-proxy gate still covers those; this rule does not apply."""
        called = []
        monkeypatch.setattr(
            addon_mod, "_simulator_for_pid_fast", lambda pid: called.append(pid) or B,
        )
        monkeypatch.setitem(addon_mod._client_process_info, "c1", {"future": object()})
        assert addon._untrusted_simulator(self._client()) is None
        assert called == [], "nothing may block inside a handshake on a pending lookup"

    def test_decrypt_all_decrypts_an_untrusted_simulator(self, addon, monkeypatch):
        self._from(monkeypatch, B)
        addon._trusted_simulators = None
        assert addon._untrusted_simulator(self._client()) is None


class TestTheHookItself:
    def _data(self, sni="api.example.com"):
        client = SimpleNamespace(id="c1", sni=sni)
        return SimpleNamespace(
            context=SimpleNamespace(client=client), ignore_connection=False,
        )

    @pytest.fixture
    def addon(self, monkeypatch):
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset({A})
        monkeypatch.setitem(
            addon_mod._client_process_info, "c1",
            {"pid": 4242, "process_name": "/x/com.apple.WebKit.Networking"},
        )
        return a

    def test_untrusted_is_ignored_and_reported(self, addon, monkeypatch):
        monkeypatch.setattr(addon_mod, "_simulator_for_pid_fast", lambda pid: B)
        data = self._data()
        out = CapturedOutput().install()
        try:
            addon.tls_clienthello(data)
        finally:
            out.restore()

        assert data.ignore_connection is True
        events = out.of_type("tls_passthrough")
        assert len(events) == 1
        assert events[0]["simulator_udid"] == B
        assert events[0]["sni"] == "api.example.com"

    def test_trusted_is_decrypted_and_not_reported(self, addon, monkeypatch):
        monkeypatch.setattr(addon_mod, "_simulator_for_pid_fast", lambda pid: A)
        data = self._data()
        out = CapturedOutput().install()
        try:
            addon.tls_clienthello(data)
        finally:
            out.restore()

        assert data.ignore_connection is False
        assert not out.of_type("tls_passthrough")

    def test_a_filtered_host_is_counted_but_not_named(self, addon, monkeypatch):
        """The count is what says a simulator's traffic is invisible, so it is
        kept; the name of a host outside the filter is not published."""
        monkeypatch.setattr(addon_mod, "_simulator_for_pid_fast", lambda pid: B)
        addon._host_filter = "other.example.com"
        out = CapturedOutput().install()
        try:
            addon.tls_clienthello(self._data())
        finally:
            out.restore()

        events = out.of_type("tls_passthrough")
        assert len(events) == 1 and events[0]["sni"] is None


class TestTheCommandFailsSafe:
    def test_a_list_replaces_the_set(self):
        a = IOSDebugAddon()
        a._handle_set_trusted_simulators({"udids": [A.lower()]})
        assert a._trusted_simulators == {A}

    def test_null_decrypts_everything(self):
        a = IOSDebugAddon()
        a._handle_set_trusted_simulators({"udids": None})
        assert a._trusted_simulators is None

    def test_anything_malformed_trusts_nobody(self):
        """A garbled command must cost visibility, never break a simulator."""
        a = IOSDebugAddon()
        a._trusted_simulators = None
        a._handle_set_trusted_simulators({"udids": "everyone"})
        assert a._trusted_simulators == frozenset()
        a._trusted_simulators = None
        a._handle_set_trusted_simulators({})
        assert a._trusted_simulators == frozenset()


class TestFindingTheSimulatorWithoutASubprocess:
    """The lookup runs inside every TLS handshake, on mitmproxy's event loop,
    so it walks parents with libproc and never forks `ps`."""

    def _tree(self, monkeypatch, parents, names, cache):
        monkeypatch.setattr(addon_mod, "_ppid_fast", parents.get)
        monkeypatch.setattr(addon_mod, "_proc_name_fast", names.get)
        monkeypatch.setattr(addon_mod, "_launchd_sim_cache", dict(cache))
        refreshed = []
        monkeypatch.setattr(
            addon_mod, "_refresh_for_unknown_simulator", lambda: refreshed.append(1),
        )
        monkeypatch.setattr(addon_mod.subprocess, "run", MagicMock(
            side_effect=AssertionError("forked a subprocess inside the handshake"),
        ))
        return refreshed

    def test_a_cached_launchd_sim_ancestor_gives_its_udid(self, monkeypatch):
        self._tree(monkeypatch, {300: 200, 200: 100, 100: 1},
                   {300: "WebKit", 200: "launchd_sim"}, {200: A})
        assert addon_mod._simulator_for_pid_fast(300) == A

    def test_an_uncached_launchd_sim_is_unknown_and_starts_a_refresh(self, monkeypatch):
        refreshed = self._tree(
            monkeypatch, {300: 200, 200: 100, 100: 1},
            {300: "WebKit", 200: "launchd_sim"}, {},
        )
        assert addon_mod._simulator_for_pid_fast(300) == addon_mod.UNKNOWN_SIMULATOR
        assert refreshed, "nothing will ever learn this simulator's UDID"

    def test_no_launchd_sim_ancestor_is_not_a_simulator(self, monkeypatch):
        self._tree(monkeypatch, {300: 200, 200: 1}, {300: "curl", 200: "zsh"}, {})
        assert addon_mod._simulator_for_pid_fast(300) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
class TestTheLibprocStructIsTheRealSize:
    """`proc_pidinfo` returns 0 for a buffer smaller than `struct proc_bsdinfo`.

    A five-field version of the struct sat in `_get_ppid` for a long time, so
    its libproc path never once succeeded and every parent lookup silently
    forked `ps` -- under a docstring promising no subprocess.
    """

    def test_it_is_the_whole_struct(self):
        assert ctypes.sizeof(addon_mod._ProcBsdInfo) == 136

    def test_it_reads_a_real_parent_without_a_subprocess(self, monkeypatch):
        monkeypatch.setattr(addon_mod.subprocess, "run", MagicMock(
            side_effect=AssertionError("fell back to ps"),
        ))
        assert addon_mod._get_ppid(os.getpid()) == os.getppid()


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def _device(udid, booted=True, sim=True):
    from server.models import DeviceState, DeviceType

    return SimpleNamespace(
        udid=udid, name=f"sim-{udid[:4]}",
        device_type=DeviceType.SIMULATOR if sim else DeviceType.DEVICE,
        state=DeviceState.BOOTED if booted else DeviceState.SHUTDOWN,
    )


class TestTheTrustCheckNeverFailsOpen:
    """The opposite policy to the capture gate's preflight, which lets an
    unreadable device through so a bug cannot block capture. Here "could not
    tell" decides decryption, so it must never read as trusted."""

    async def test_a_device_that_cannot_be_asked_is_not_trusted(self, monkeypatch):
        from server.proxy.cert_preflight import simulator_trust

        async def installed(_c, udid, device_name=None):
            if udid == B:
                raise OSError("TrustStore unreadable")
            return True

        monkeypatch.setattr("server.proxy.cert_manager.is_cert_installed", installed)
        controller = SimpleNamespace(list_devices=AsyncMock(
            return_value=[_device(A), _device(B), _device(A[:-1] + "9", booted=False)],
        ))

        result = {t["udid"]: t["trusted"] for t in await simulator_trust(controller)}
        assert result == {A: True, B: None}, "shut-down simulators are not listed"

    async def test_an_unreadable_device_list_is_none_not_empty(self):
        from server.proxy.cert_preflight import simulator_trust

        controller = SimpleNamespace(list_devices=AsyncMock(side_effect=OSError("simctl")))
        assert await simulator_trust(controller) is None


class TestComputingTheSet:
    def _app(self, **state):
        return SimpleNamespace(state=SimpleNamespace(
            device_controller=object(), decrypt_all_simulators=False, **state,
        ))

    async def test_only_confirmed_trust_is_trusted(self, monkeypatch):
        from server.proxy import sim_tls

        async def trust(_c):
            return [
                {"udid": A, "name": "a", "trusted": True},
                {"udid": B, "name": "b", "trusted": False},
                {"udid": "C", "name": "c", "trusted": None},
            ]

        monkeypatch.setattr(sim_tls, "simulator_trust", trust)
        assert await sim_tls.compute_trusted(self._app()) == [A]

    async def test_a_failed_lookup_trusts_nobody_not_the_previous_set(self, monkeypatch):
        """The previous set may name a simulator erased since."""
        from server.proxy import sim_tls

        async def failed(_c):
            return None

        monkeypatch.setattr(sim_tls, "simulator_trust", failed)
        app = self._app(simulator_trust=[{"udid": A, "name": "a", "trusted": True}])
        assert await sim_tls.compute_trusted(app) == []
        assert app.state.simulator_trust == []
        assert app.state.simulator_trust_failed is True

    async def test_skip_cert_check_decrypts_everything(self, monkeypatch):
        from server.proxy import sim_tls

        async def trust(_c):
            return [{"udid": B, "name": "b", "trusted": False}]

        monkeypatch.setattr(sim_tls, "simulator_trust", trust)
        app = self._app()
        app.state.decrypt_all_simulators = True
        assert await sim_tls.compute_trusted(app) is None


class TestTheAdapter:
    def _adapter(self):
        from server.sources.proxy import ProxyAdapter

        return ProxyAdapter(device_id="x", on_entry=lambda e: None, flow_store=None)

    def test_it_starts_out_trusting_nobody(self):
        assert self._adapter().trusted_simulators == []

    async def test_every_spawn_asks_first_and_passes_the_set(self, monkeypatch):
        """Not only the lifespan's first start: the endpoints and the watchdog
        start mitmdump too, and each must launch with a current set."""
        from server.sources import proxy as proxy_mod

        adapter = self._adapter()
        adapter.trust_provider = AsyncMock(return_value=[B, A])
        monkeypatch.setattr(adapter, "_find_mitmdump", lambda: "/bin/mitmdump")
        monkeypatch.setattr(adapter, "_kill_stale_mitmdump", lambda port: None)
        spawned = {}

        async def fake_exec(*cmd, **kw):
            spawned.update(kw)
            raise OSError("stop here")

        monkeypatch.setattr(proxy_mod.asyncio, "create_subprocess_exec", fake_exec)
        await adapter.start()

        adapter.trust_provider.assert_awaited_once()
        assert spawned["env"][proxy_mod.TRUSTED_SIMULATORS_ENV] == f"{B},{A}"

    async def test_a_failing_provider_does_not_widen_the_set(self, monkeypatch):
        from server.sources import proxy as proxy_mod

        adapter = self._adapter()
        adapter._trusted_simulators = [A]
        adapter.trust_provider = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(adapter, "_find_mitmdump", lambda: "/bin/mitmdump")
        monkeypatch.setattr(adapter, "_kill_stale_mitmdump", lambda port: None)
        spawned = {}

        async def fake_exec(*cmd, **kw):
            spawned.update(kw)
            raise OSError("stop here")

        monkeypatch.setattr(proxy_mod.asyncio, "create_subprocess_exec", fake_exec)
        await adapter.start()
        assert spawned["env"][proxy_mod.TRUSTED_SIMULATORS_ENV] == A

    async def test_the_set_survives_a_stop(self):
        """Bypass and mock state are about one run of mitmdump and are cleared;
        this is about the simulators, and must reach the next spawn."""
        adapter = self._adapter()
        await adapter.set_trusted_simulators([A])
        await adapter.stop()
        assert adapter.trusted_simulators == [A]

    def test_passthrough_is_counted_per_simulator(self):
        adapter = self._adapter()
        for host in ("a.example.com", None, "b.example.com"):
            adapter._handle_tls_passthrough({"simulator_udid": B, "sni": host})
        counts = adapter.passthrough_counts()
        assert counts[B]["connections"] == 3
        assert counts[B]["last_host"] == "b.example.com"


class TestTheReport:
    def _app(self, trusted, trust, counts=None):
        adapter = SimpleNamespace(
            local_capture=True,
            trusted_simulators=trusted,
            passthrough_counts=lambda: counts or {},
        )
        return SimpleNamespace(state=SimpleNamespace(
            proxy_adapter=adapter, simulator_trust=trust,
        ))

    def test_each_simulator_says_which_and_why(self):
        from server.proxy import sim_tls

        app = self._app([A], [
            {"udid": A, "name": "a", "trusted": True},
            {"udid": B, "name": "b", "trusted": False},
        ], {B: {"connections": 4, "last_host": "x.example.com"}})
        by = {e.udid: e for e in sim_tls.report(app)}

        assert by[A].tls == "decrypted" and by[A].fix is None
        assert by[B].tls == "passed_through"
        assert "does not trust" in by[B].reason
        assert "install_proxy_cert" in by[B].fix
        assert by[B].connections_passed_through == 4

    def test_a_simulator_seen_only_by_the_addon_still_appears(self):
        """Booted since the last check: it shows up the moment it connects."""
        from server.proxy import sim_tls

        app = self._app([A], [{"udid": A, "name": "a", "trusted": True}],
                        {B: {"connections": 1, "last_host": None}})
        entry = {e.udid: e for e in sim_tls.report(app)}[B]
        assert entry.tls == "passed_through"
        assert "since the last trust check" in entry.reason

    def test_decrypting_an_untrusting_simulator_says_it_will_fail(self):
        from server.proxy import sim_tls

        app = self._app(None, [{"udid": B, "name": "b", "trusted": False}])
        entry = sim_tls.report(app)[0]
        assert entry.tls == "decrypted"
        assert "will fail" in entry.reason

    def test_nothing_when_local_capture_is_off(self):
        from server.proxy import sim_tls

        app = self._app([], [])
        app.state.proxy_adapter.local_capture = False
        assert sim_tls.report(app) is None

    def test_the_flow_note_names_the_cause_and_the_fix(self):
        from server.proxy import sim_tls

        app = self._app([], [{"udid": B, "name": "b", "trusted": False}])
        note = sim_tls.passthrough_note(app, B.lower())
        assert note and "not decrypted" in note and "install_proxy_cert" in note
        assert sim_tls.passthrough_note(app, None) is None
        assert sim_tls.passthrough_note(app, A) is None


class TestAFlowQueryCarriesTheNoteEvenWhenEmpty:
    """Zero flows from a passed-through simulator reads exactly like an app
    that made no requests. The note has to be on the empty answer most of all."""

    @pytest.fixture
    def app(self):
        from server.config import ServerConfig
        from server.main import create_app

        app = create_app(
            config=ServerConfig(api_key="test-key-12345"),
            enable_oslog=False, enable_crash=False, enable_proxy=False,
        )
        app.state.device_controller = MagicMock()
        app.state.proxy_adapter = None
        return app

    @pytest.fixture
    def auth_headers(self):
        return {"Authorization": "Bearer test-key-12345"}

    @pytest.fixture
    def client(self, app):
        from fastapi.testclient import TestClient

        return TestClient(app)

    def test_query_flows(self, client, auth_headers, app, monkeypatch):
        from server.proxy import sim_tls
        from server.proxy.flow_store import FlowStore

        app.state.flow_store = FlowStore()
        monkeypatch.setattr(
            sim_tls, "passthrough_note",
            lambda _app, udid: "passed through" if udid == B else None,
        )
        r = client.get(
            "/api/v1/proxy/flows", params={"simulator_udid": B}, headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 0
        assert body["simulator_tls_note"] == "passed through"

    def test_flow_summary(self, client, auth_headers, app, monkeypatch):
        from server.proxy import sim_tls
        from server.proxy.flow_store import FlowStore

        app.state.flow_store = FlowStore()
        monkeypatch.setattr(
            sim_tls, "passthrough_note",
            lambda _app, udid: "passed through" if udid == B else None,
        )
        r = client.get(
            "/api/v1/proxy/flows/summary", params={"simulator_udid": B},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] == "passed through"


class TestQuernsOwnEventsRefreshTheSetAtOnce:
    """Waiting for the periodic check is fine for changes made outside quern.
    For the ones quern makes itself it is a needless window: decryption should
    start as soon as the CA is installed, and an erased simulator must stop
    being trusted before it can boot again."""

    @pytest.mark.parametrize("module, function", [
        ("server.api.proxy_certs", "install_cert"),
        ("server.api.device", "erase_device"),
        ("server.api.device", "boot_device"),
    ])
    def test_the_endpoint_refreshes(self, module, function):
        import importlib
        import inspect

        src = inspect.getsource(getattr(importlib.import_module(module), function))
        assert "sim_tls.refresh_after(" in src

    async def test_a_failed_refresh_does_not_fail_the_call(self, monkeypatch):
        from server.proxy import sim_tls

        monkeypatch.setattr(sim_tls, "refresh", AsyncMock(side_effect=RuntimeError("x")))
        app = SimpleNamespace(state=SimpleNamespace(
            proxy_adapter=SimpleNamespace(local_capture=True),
        ))
        await sim_tls.refresh_after(app, "a test")  # does not raise
