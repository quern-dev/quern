"""Local capture decrypts only simulators known to trust the CA (#354).

A simulator that does not trust the mitmproxy CA fails every HTTPS request the
proxy terminates, and local capture spans every simulator on the Mac. So TLS
from a simulator is decrypted only when the server has confirmed that
simulator -- in its current boot -- trusts the CA, and passed through
untouched otherwise.

The property all of this protects is directional. Trust changes under a running
proxy -- a simulator boots, is created, or is erased -- so the answer is often
stale, and stale has to mean "passed through", never "decrypted". Most of these
tests are about that: nothing that cannot confirm trust may produce
decryption, whether it is an unchecked simulator, a failed check, an unknown
UDID, a rebooted one, a broken lookup, a previous set, or an exception.

An independent review of the first version ran mutants against this file and
found nine that survived the whole suite -- among them an empty trusted set
handed over as "decrypt everything", and refresh calls made dead code that a
source-grepping test still passed. Each now has a behavioural test below.
"""

from __future__ import annotations

import asyncio
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


def _local_client(cid="c1", sni="api.example.com"):
    from mitmproxy.proxy.mode_specs import ProxyMode

    return SimpleNamespace(id=cid, sni=sni, proxy_mode=ProxyMode.parse("local"))


def _regular_client(cid="c1", sni="api.example.com"):
    from mitmproxy.proxy.mode_specs import ProxyMode

    return SimpleNamespace(id=cid, sni=sni, proxy_mode=ProxyMode.parse("regular"))


# ---------------------------------------------------------------------------
# The addon: what it starts with
# ---------------------------------------------------------------------------


class TestTheHandoverDefaultsToTrustingNobody:
    def test_the_server_and_addon_agree_on_the_variable(self):
        """Spelled out on both sides, because importing the addon into the
        server would run its monkey-patch there too."""
        from server.sources.proxy import TRUSTED_SIMULATORS_ENV

        assert TRUSTED_SIMULATORS_ENV == addon_mod.TRUSTED_SIMULATORS_ENV

    def test_the_parser(self):
        parse = addon_mod._parse_trusted_simulators
        assert parse(None) == frozenset()
        assert parse("") == frozenset()
        assert parse("*") is None
        assert parse(f" {A.lower()}, ,{B}") == {A, B}

    def test_an_addon_started_without_being_told_trusts_nobody(self, monkeypatch):
        """The constructor, not just the parser: a mutant that read a missing
        variable as "*" in `__init__` passed a parser-only test."""
        monkeypatch.delenv(addon_mod.TRUSTED_SIMULATORS_ENV, raising=False)
        assert IOSDebugAddon()._trusted_simulators == frozenset()

    def test_star_in_the_environment_decrypts_everything(self, monkeypatch):
        monkeypatch.setenv(addon_mod.TRUSTED_SIMULATORS_ENV, "*")
        assert IOSDebugAddon()._trusted_simulators is None

    def test_nothing_is_decrypted_before_the_set_is_bound(self, monkeypatch):
        """The set from the environment is bound to running instances at
        `load`, off the event loop. Until then no instance matches."""
        monkeypatch.setenv(addon_mod.TRUSTED_SIMULATORS_ENV, A)
        assert IOSDebugAddon()._trusted_instances == {}


# ---------------------------------------------------------------------------
# The addon: deciding per connection
# ---------------------------------------------------------------------------


class TestWhichConnectionsArePassedThrough:
    @pytest.fixture
    def addon(self, monkeypatch):
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset({A})
        a._trusted_instances = {A: 100}
        monkeypatch.setitem(addon_mod._client_process_info, "c1", {"pid": 4242})
        return a

    def _from(self, monkeypatch, udid, instance):
        monkeypatch.setattr(
            addon_mod, "_simulator_instance_for_pid", lambda pid: (udid, instance),
        )

    def test_the_trusted_boot_is_decrypted(self, addon, monkeypatch):
        self._from(monkeypatch, A, 100)
        assert addon._untrusted_simulator(_local_client()) is None

    def test_the_same_simulator_rebooted_is_passed_through(self, addon, monkeypatch):
        """An erase keeps the UDID and empties the TrustStore, and needs a
        reboot to be used. A new launchd_sim is unconfirmed until re-checked --
        which is what makes an erase from outside quern safe."""
        self._from(monkeypatch, A, 200)
        assert addon._untrusted_simulator(_local_client()) == A

    def test_a_trusted_udid_never_bound_is_passed_through(self, addon, monkeypatch):
        addon._trusted_instances = {}
        self._from(monkeypatch, A, 100)
        assert addon._untrusted_simulator(_local_client()) == A

    def test_an_untrusted_simulator_is_passed_through(self, addon, monkeypatch):
        self._from(monkeypatch, B, 300)
        assert addon._untrusted_simulator(_local_client()) == B

    def test_an_unidentified_simulator_is_passed_through(self, addon, monkeypatch):
        self._from(monkeypatch, addon_mod.UNKNOWN_SIMULATOR, 100)
        assert addon._untrusted_simulator(_local_client()) == addon_mod.UNKNOWN_SIMULATOR

    def test_a_failed_lookup_is_passed_through(self, addon, monkeypatch):
        self._from(monkeypatch, addon_mod.UNKNOWN_SIMULATOR, None)
        assert addon._untrusted_simulator(_local_client()) == addon_mod.UNKNOWN_SIMULATOR

    def test_a_mac_process_is_left_alone(self, addon, monkeypatch):
        self._from(monkeypatch, None, None)
        assert addon._untrusted_simulator(_local_client()) is None

    def test_a_redirected_connection_without_a_pid_is_passed_through(
        self, addon, monkeypatch,
    ):
        """The redirector always supplies a pid. Without one, the attribution
        patch did not install, and treating that as "not a simulator" would
        decrypt every simulator silently."""
        monkeypatch.setitem(addon_mod._client_process_info, "c1", {})
        assert addon._untrusted_simulator(_local_client()) == addon_mod.UNKNOWN_SIMULATOR

    def test_a_proxied_device_without_a_pid_keeps_todays_behaviour(
        self, addon, monkeypatch,
    ):
        """A device *using* the proxy arrives with no pid, through the system
        proxy, which still refuses over the CA. Nothing here may block on its
        pending socket lookup."""
        called = []
        monkeypatch.setattr(
            addon_mod, "_simulator_instance_for_pid",
            lambda pid: called.append(pid) or (B, 1),
        )
        monkeypatch.setitem(addon_mod._client_process_info, "c1", {"future": object()})
        assert addon._untrusted_simulator(_regular_client()) is None
        assert called == []

    def test_decrypt_all_decrypts_an_untrusted_simulator(self, addon, monkeypatch):
        self._from(monkeypatch, B, 300)
        addon._trusted_simulators = None
        assert addon._untrusted_simulator(_local_client()) is None


class TestTheHookItself:
    def _data(self, client):
        return SimpleNamespace(context=SimpleNamespace(client=client), ignore_connection=False)

    @pytest.fixture
    def addon(self, monkeypatch):
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset({A})
        a._trusted_instances = {A: 100}
        monkeypatch.setitem(
            addon_mod._client_process_info, "c1",
            {"pid": 4242, "process_name": "/x/com.apple.WebKit.Networking"},
        )
        return a

    def _run(self, addon, data):
        out = CapturedOutput().install()
        try:
            addon.tls_clienthello(data)
        finally:
            out.restore()
        return out

    def test_untrusted_is_ignored_and_reported(self, addon, monkeypatch):
        monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid", lambda pid: (B, 300))
        data = self._data(_local_client())
        events = self._run(addon, data).of_type("tls_passthrough")

        assert data.ignore_connection is True
        assert len(events) == 1
        assert events[0]["simulator_udid"] == B
        assert events[0]["sni"] == "api.example.com"

    def test_trusted_is_decrypted_and_not_reported(self, addon, monkeypatch):
        monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid", lambda pid: (A, 100))
        data = self._data(_local_client())
        out = self._run(addon, data)

        assert data.ignore_connection is False
        assert not out.of_type("tls_passthrough")

    def test_an_exception_on_a_redirected_connection_passes_it_through(
        self, addon, monkeypatch,
    ):
        """mitmproxy swallows a hook's exception and carries on with the
        handshake -- which would decrypt."""
        def boom(pid):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid", boom)
        data = self._data(_local_client())
        self._run(addon, data)
        assert data.ignore_connection is True

    def test_a_filtered_host_is_counted_but_not_named(self, addon, monkeypatch):
        monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid", lambda pid: (B, 300))
        addon._host_filter = "other.example.com"
        events = self._run(addon, self._data(_local_client())).of_type("tls_passthrough")
        assert len(events) == 1 and events[0]["sni"] is None


class TestTheCommandFailsSafe:
    @pytest.fixture(autouse=True)
    def _no_ps(self, monkeypatch):
        monkeypatch.setattr(
            addon_mod, "_bind_to_running_instances",
            lambda udids: {u: 100 for u in udids},
        )

    def test_a_list_replaces_the_set_and_binds_it(self):
        a = IOSDebugAddon()
        a._handle_set_trusted_simulators({"udids": [A.lower()]})
        assert a._trusted_simulators == {A}
        assert a._trusted_instances == {A: 100}

    def test_null_decrypts_everything(self):
        a = IOSDebugAddon()
        a._handle_set_trusted_simulators({"udids": None})
        assert a._trusted_simulators is None

    def test_anything_malformed_trusts_nobody(self):
        a = IOSDebugAddon()
        a._trusted_simulators = None
        a._handle_set_trusted_simulators({"udids": "everyone"})
        assert a._trusted_simulators == frozenset()
        a._trusted_simulators = None
        a._handle_set_trusted_simulators({})
        assert a._trusted_simulators == frozenset()

    def test_a_simulator_dropped_from_the_set_loses_its_binding(self):
        a = IOSDebugAddon()
        a._handle_set_trusted_simulators({"udids": [A]})
        a._handle_set_trusted_simulators({"udids": [B]})
        assert A not in a._trusted_instances

    def test_emptying_the_set_stops_decrypting(self, monkeypatch):
        """[A] then []: the rebind only runs for a non-empty set, so a binding
        left behind would keep A decrypted after it was removed. Asserted on
        the decision, not the dict."""
        a = IOSDebugAddon()
        a._handle_set_trusted_simulators({"udids": [A]})
        a._handle_set_trusted_simulators({"udids": []})
        monkeypatch.setitem(addon_mod._client_process_info, "c1", {"pid": 4242})
        monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid", lambda pid: (A, 100))
        assert a._untrusted_simulator(_local_client()) == A


class TestTheLoadTimeBindNeverRestoresAnOlderSet:
    """CodeRabbit on #357. `load` binds the set handed over at spawn on its own
    thread, while the stdin thread may already be applying a newer one. Calling
    `_set_trusted` with the spawn-time snapshot re-published it over the newer
    set -- an older, wider answer landing last, one layer below the lock that
    stops the same thing on the server."""

    def test_a_newer_set_survives_the_load_time_bind(self, monkeypatch):
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset({A})  # handed over at spawn
        landed = []

        def bind(udids):
            # The stdin thread applies a newer set mid-bind -- once.
            if not landed:
                landed.append(1)
                a._handle_set_trusted_simulators({"udids": [B]})
            return {u: 100 for u in udids}

        monkeypatch.setattr(addon_mod, "_bind_to_running_instances", bind)
        monkeypatch.setattr(addon_mod, "_refresh_launchd_sim_cache", lambda: None)
        a._bind_trusted()

        assert a._trusted_simulators == {B}, "the spawn-time set was put back"
        assert A not in a._trusted_instances

    def test_it_never_publishes_a_set(self, monkeypatch):
        """The race itself sits between reading the snapshot and publishing it,
        which no test can interleave deterministically. So assert the operation
        that makes the race possible never happens: binding at load reads the
        set and binds it, and does not write it."""
        a = IOSDebugAddon()
        snapshot = frozenset({A})
        a._trusted_simulators = snapshot
        published = []
        monkeypatch.setattr(a, "_set_trusted", lambda t: published.append(t))
        monkeypatch.setattr(
            addon_mod, "_bind_to_running_instances", lambda udids: {u: 100 for u in udids},
        )
        a._bind_trusted()
        assert published == [], "the load-time bind re-published the spawn-time set"
        assert a._trusted_simulators is snapshot

    def test_it_still_binds_when_nothing_raced(self, monkeypatch):
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset({A})
        monkeypatch.setattr(
            addon_mod, "_bind_to_running_instances", lambda udids: {u: 100 for u in udids},
        )
        a._bind_trusted()
        assert a._trusted_instances == {A: 100}

    def test_an_empty_set_still_warms_the_cache(self, monkeypatch):
        """The load thread was also what first filled the launchd_sim cache."""
        warmed = []
        monkeypatch.setattr(addon_mod, "_refresh_launchd_sim_cache", lambda: warmed.append(1))
        a = IOSDebugAddon()
        a._trusted_simulators = frozenset()
        a._bind_trusted()
        assert warmed


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

    def test_a_cached_launchd_sim_gives_its_udid_and_instance(self, monkeypatch):
        self._tree(monkeypatch, {300: 200, 200: 100, 100: 1},
                   {300: "WebKit", 200: "launchd_sim"}, {200: A.lower()})
        assert addon_mod._simulator_instance_for_pid(300) == (A, 200)

    def test_an_uncached_launchd_sim_is_unknown_and_starts_a_refresh(self, monkeypatch):
        refreshed = self._tree(
            monkeypatch, {300: 200, 200: 100, 100: 1},
            {300: "WebKit", 200: "launchd_sim"}, {},
        )
        assert addon_mod._simulator_instance_for_pid(300) == (addon_mod.UNKNOWN_SIMULATOR, 200)
        assert refreshed, "nothing will ever learn this simulator's UDID"

    def test_reaching_launchd_is_not_a_simulator(self, monkeypatch):
        self._tree(monkeypatch, {300: 200, 200: 1}, {300: "curl", 200: "zsh"}, {})
        assert addon_mod._simulator_instance_for_pid(300) == (None, None)

    def test_a_parent_lookup_that_fails_is_not_read_as_a_mac_process(self, monkeypatch):
        """"Could not tell" must never become "not a simulator" -- that
        decrypts whatever it was."""
        self._tree(monkeypatch, {300: 200}, {300: "WebKit", 200: "x"}, {})
        assert addon_mod._simulator_instance_for_pid(300) == (addon_mod.UNKNOWN_SIMULATOR, None)

    def test_a_cycle_is_not_read_as_a_mac_process(self, monkeypatch):
        self._tree(monkeypatch, {300: 200, 200: 300}, {}, {})
        assert addon_mod._simulator_instance_for_pid(300) == (addon_mod.UNKNOWN_SIMULATOR, None)


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
# The server: checking trust
# ---------------------------------------------------------------------------


def _device(udid, booted=True, sim=True):
    from server.models import DeviceState, DeviceType

    return SimpleNamespace(
        udid=udid, name=f"sim-{udid[:4]}",
        device_type=DeviceType.SIMULATOR if sim else DeviceType.DEVICE,
        state=DeviceState.BOOTED if booted else DeviceState.SHUTDOWN,
    )


class TestTheTrustCheckNeverFailsOpen:
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


def _app(**state):
    return SimpleNamespace(state=SimpleNamespace(
        device_controller=object(), decrypt_all_simulators=False, **state,
    ))


class TestComputingTheSet:
    async def test_only_confirmed_trust_is_trusted(self, monkeypatch):
        from server.proxy import sim_tls

        async def trust(_c):
            return [
                {"udid": A, "name": "a", "trusted": True},
                {"udid": B, "name": "b", "trusted": False},
                {"udid": "C", "name": "c", "trusted": None},
            ]

        monkeypatch.setattr(sim_tls, "simulator_trust", trust)
        assert await sim_tls.compute_trusted(_app()) == [A]

    async def test_a_failed_lookup_trusts_nobody_not_the_previous_set(self, monkeypatch):
        from server.proxy import sim_tls

        monkeypatch.setattr(sim_tls, "simulator_trust", AsyncMock(return_value=None))
        app = _app(simulator_trust=[{"udid": A, "name": "a", "trusted": True}])
        assert await sim_tls.compute_trusted(app) == []
        assert app.state.simulator_trust == []
        assert app.state.simulator_trust_failed is True

    async def test_skip_cert_check_never_decrypts_a_simulator_known_not_to_trust(
        self, monkeypatch,
    ):
        """It used to answer None -- decrypt everyone -- with B just found not to
        trust the CA, so every HTTPS request from B failed while the capture
        reported started (#414). It now widens to what the check could not
        rule out, which is what the flag is for."""
        from server.proxy import sim_tls

        monkeypatch.setattr(sim_tls, "simulator_trust", AsyncMock(return_value=[
            {"udid": A, "name": "a", "trusted": True},
            {"udid": B, "name": "b", "trusted": False},
            {"udid": "C", "name": "c", "trusted": None},
        ]))
        app = _app()
        app.state.decrypt_all_simulators = True
        assert await sim_tls.compute_trusted(app) == [A, "C"]

    async def test_skip_cert_check_with_a_failed_lookup_decrypts_nobody(self, monkeypatch):
        """No list, no knowledge: the previous answer, None, decrypted every
        simulator on a lookup that had failed."""
        from server.proxy import sim_tls

        monkeypatch.setattr(sim_tls, "simulator_trust", AsyncMock(return_value=None))
        app = _app()
        app.state.decrypt_all_simulators = True
        assert await sim_tls.compute_trusted(app) == []


class TestAnOlderAnswerNeverLandsLast:
    """The review reproduced this: a slow periodic check that saw A trusted
    finished after an erase's check that did not, and the addon ended up with
    [A]. Refreshes are serialised, so each computes after the last has sent."""

    async def test_two_refreshes_land_in_order(self, monkeypatch):
        from server.proxy import sim_tls

        answers = [[{"udid": A, "name": "a", "trusted": True}], []]
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def trust(_c):
            answer = answers.pop(0)
            if answer:  # the first, slow, check
                first_started.set()
                await release_first.wait()
            return answer

        monkeypatch.setattr(sim_tls, "simulator_trust", trust)
        sent = []
        adapter = SimpleNamespace(set_trusted_simulators=AsyncMock(side_effect=sent.append))
        app = _app(proxy_adapter=adapter)

        slow = asyncio.create_task(sim_tls.refresh(app))
        await first_started.wait()
        fast = asyncio.create_task(sim_tls.refresh(app))  # the erase's check
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(slow, fast)

        assert sent == [[A], []], "the older, wider answer landed last"


class TestCheckingOnAPassedThroughConnection:
    async def test_a_known_untrusted_simulator_does_not_re_check(self, monkeypatch):
        from server.proxy import sim_tls

        called = AsyncMock()
        monkeypatch.setattr(sim_tls, "refresh_after", called)
        app = _app(simulator_trust=[{"udid": B, "name": "b", "trusted": False}])
        sim_tls.on_passthrough(app, B)
        await asyncio.sleep(0)
        called.assert_not_awaited()

    @pytest.mark.parametrize("udid, trust", [
        (A, [{"udid": A, "name": "a", "trusted": True}]),  # a trusted one, rebooted
        (B, []),                                            # booted since the check
        ("unknown-simulator", []),                          # not identified yet
    ])
    async def test_an_unconfirmed_simulator_is_checked_now(self, monkeypatch, udid, trust):
        from server.proxy import sim_tls

        called = AsyncMock()
        monkeypatch.setattr(sim_tls, "refresh_after", called)
        sim_tls.on_passthrough(_app(simulator_trust=trust), udid)
        await asyncio.sleep(0)
        called.assert_awaited_once()

    async def test_a_burst_costs_one_check(self, monkeypatch):
        from server.proxy import sim_tls

        called = AsyncMock()
        monkeypatch.setattr(sim_tls, "refresh_after", called)
        app = _app(simulator_trust=[])
        for _ in range(20):
            sim_tls.on_passthrough(app, B)
        await asyncio.sleep(0)
        assert called.await_count == 1


class TestThePeriodicCheck:
    async def test_it_refreshes_while_local_capture_runs(self, monkeypatch):
        from server.proxy import sim_tls

        ran = asyncio.Event()
        monkeypatch.setattr(sim_tls, "refresh", AsyncMock(side_effect=lambda app: ran.set()))
        app = _app(proxy_adapter=SimpleNamespace(is_running=True, local_capture=True))
        task = asyncio.create_task(sim_tls.refresh_loop(app, interval=0.01))
        try:
            await asyncio.wait_for(ran.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_it_does_nothing_without_local_capture(self, monkeypatch):
        from server.proxy import sim_tls

        refresh = AsyncMock()
        monkeypatch.setattr(sim_tls, "refresh", refresh)
        app = _app(proxy_adapter=SimpleNamespace(is_running=True, local_capture=False))
        task = asyncio.create_task(sim_tls.refresh_loop(app, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        refresh.assert_not_awaited()


class TestInstallWiresEveryStart:
    def test_it_sets_the_provider_and_the_event_hook(self):
        from server.proxy import sim_tls

        adapter = SimpleNamespace(trust_provider=None, on_passthrough=None)
        app = SimpleNamespace(state=SimpleNamespace(decrypt_all_simulators=True))
        sim_tls.install(app, adapter)
        assert adapter.trust_provider is not None
        assert adapter.on_passthrough is not None
        assert app.state.decrypt_all_simulators is False

    def test_the_lifespan_calls_it(self):
        """Source, because running the lifespan reaches the real machine. The
        behaviour is pinned above; this only says it is wired."""
        import inspect

        from server import main

        assert "sim_tls.install(app, proxy)" in inspect.getsource(main)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class TestTheAdapter:
    def _adapter(self):
        from server.sources.proxy import ProxyAdapter

        return ProxyAdapter(device_id="x", on_entry=lambda e: None, flow_store=None)

    async def _spawn_env(self, monkeypatch, adapter):
        from server.sources import proxy as proxy_mod

        monkeypatch.setattr(adapter, "_find_mitmdump", lambda: "/bin/mitmdump")
        monkeypatch.setattr(adapter, "_kill_stale_mitmdump", lambda port: None)
        spawned = {}

        async def fake_exec(*cmd, **kw):
            spawned.update(kw)
            raise OSError("stop here")

        monkeypatch.setattr(proxy_mod.asyncio, "create_subprocess_exec", fake_exec)
        await adapter.start()
        return spawned["env"][proxy_mod.TRUSTED_SIMULATORS_ENV]

    def test_it_starts_out_trusting_nobody(self):
        assert self._adapter().trusted_simulators == []

    async def test_every_spawn_asks_first_and_passes_the_set(self, monkeypatch):
        adapter = self._adapter()
        adapter.trust_provider = AsyncMock(return_value=[B, A])
        assert await self._spawn_env(monkeypatch, adapter) == f"{B},{A}"
        adapter.trust_provider.assert_awaited_once()

    async def test_an_empty_set_is_handed_over_as_nobody_not_everybody(self, monkeypatch):
        """The review's most important surviving mutant: `"*" if not set`."""
        adapter = self._adapter()
        adapter.trust_provider = AsyncMock(return_value=[])
        assert await self._spawn_env(monkeypatch, adapter) == ""

    async def test_decrypt_all_is_handed_over_as_star(self, monkeypatch):
        adapter = self._adapter()
        adapter.trust_provider = AsyncMock(return_value=None)
        assert await self._spawn_env(monkeypatch, adapter) == "*"

    async def test_a_failing_provider_trusts_nobody(self, monkeypatch):
        """Not the previous set, which may name a simulator erased since."""
        adapter = self._adapter()
        adapter._trusted_simulators = [A]
        adapter.trust_provider = AsyncMock(side_effect=RuntimeError("boom"))
        assert await self._spawn_env(monkeypatch, adapter) == ""

    async def test_a_change_during_spawn_is_sent_once_running(self, monkeypatch):
        """A refresh that lands while mitmdump is being spawned updates the
        mirror but has no process to send to."""
        from server.sources import proxy as proxy_mod

        adapter = self._adapter()
        adapter.trust_provider = AsyncMock(return_value=[A])
        monkeypatch.setattr(adapter, "_find_mitmdump", lambda: "/bin/mitmdump")
        monkeypatch.setattr(adapter, "_kill_stale_mitmdump", lambda port: None)
        monkeypatch.setattr(adapter, "_read_loop", AsyncMock())
        monkeypatch.setattr(adapter, "_drain_stderr", AsyncMock())
        sent = []
        monkeypatch.setattr(adapter, "send_command", AsyncMock(side_effect=sent.append))

        async def fake_exec(*cmd, **kw):
            adapter._trusted_simulators = []  # the erase's refresh, mid-spawn
            return SimpleNamespace(stdin=None, stdout=None, stderr=None, returncode=None)

        monkeypatch.setattr(proxy_mod.asyncio, "create_subprocess_exec", fake_exec)
        await adapter.start()
        assert {"action": "set_trusted_simulators", "udids": []} in sent

    async def test_the_set_survives_a_stop(self):
        adapter = self._adapter()
        await adapter.set_trusted_simulators([A])
        await adapter.stop()
        assert adapter.trusted_simulators == [A]

    def test_passthrough_is_counted_and_announced(self):
        adapter = self._adapter()
        heard = []
        adapter.on_passthrough = heard.append
        for host in ("a.example.com", None, "b.example.com"):
            adapter._handle_tls_passthrough({"simulator_udid": B, "sni": host})
        counts = adapter.passthrough_counts()
        assert counts[B]["connections"] == 3
        assert counts[B]["last_host"] == "b.example.com"
        assert heard == [B, B, B]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestTheReport:
    def _app(self, trusted, trust, counts=None, failed=False):
        adapter = SimpleNamespace(
            local_capture=True,
            trusted_simulators=trusted,
            passthrough_counts=lambda: counts or {},
        )
        return SimpleNamespace(state=SimpleNamespace(
            proxy_adapter=adapter, simulator_trust=trust, simulator_trust_failed=failed,
        ))

    def test_each_simulator_says_which_and_why(self):
        from server.proxy import sim_tls

        app = self._app([A], [
            {"udid": A, "name": "a", "trusted": True},
            {"udid": B, "name": "b", "trusted": False},
        ], {B: {"connections": 4, "last_host": "x.example.com"}})
        by = {e.udid: e for e in sim_tls.report(app)}

        assert by[A].tls == "decrypted" and by[A].reason is None
        assert by[B].tls == "passed_through"
        assert "does not trust" in by[B].reason
        assert "install_proxy_cert" in by[B].fix
        assert by[B].connections_passed_through == 4

    def test_an_unchecked_simulator_is_passed_through(self):
        from server.proxy import sim_tls

        app = self._app([], [{"udid": B, "name": "b", "trusted": None}])
        entry = sim_tls.report(app)[0]
        assert entry.tls == "passed_through"
        assert "could not check" in entry.reason

    def test_a_decrypted_simulator_says_what_it_missed(self):
        """Connections opened before its boot was confirmed were passed through,
        and an open HTTP/2 connection stays that way. Without this the report
        says "decrypted" and nothing explains the missing traffic."""
        from server.proxy import sim_tls

        app = self._app([A], [{"udid": A, "name": "a", "trusted": True}],
                        {A: {"connections": 2, "last_host": None}})
        entry = sim_tls.report(app)[0]
        assert entry.tls == "decrypted"
        assert "before this boot was confirmed" in entry.reason

    def test_a_simulator_seen_only_by_the_addon_still_appears(self):
        from server.proxy import sim_tls

        app = self._app([A], [{"udid": A, "name": "a", "trusted": True}],
                        {B: {"connections": 1, "last_host": None}})
        entry = {e.udid: e for e in sim_tls.report(app)}[B]
        assert entry.tls == "passed_through"
        assert "since the last trust check" in entry.reason

    def test_a_failed_check_says_so_instead_of_reading_as_nothing_booted(self):
        from server.proxy import sim_tls

        app = self._app([], [], {B: {"connections": 1, "last_host": None}}, failed=True)
        entry = sim_tls.report(app)[0]
        assert "could not list simulators" in entry.reason

    def test_decrypting_an_untrusting_simulator_says_it_will_fail(self):
        from server.proxy import sim_tls

        app = self._app(None, [{"udid": B, "name": "b", "trusted": False}])
        entry = sim_tls.report(app)[0]
        assert entry.tls == "decrypted"
        assert "will fail" in entry.reason

    def test_decrypting_one_the_check_could_not_read_says_so(self):
        from server.proxy import sim_tls

        app = self._app([B], [{"udid": B, "name": "b", "trusted": None}])
        entry = sim_tls.report(app)[0]
        assert entry.tls == "decrypted"
        assert "could not tell" in entry.reason

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


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    from server.config import ServerConfig
    from server.main import create_app
    from server.proxy import sim_tls

    app = create_app(
        config=ServerConfig(api_key="test-key-12345"),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    app.state.device_controller = MagicMock()
    app.state.device_controller.resolve_udid = AsyncMock(side_effect=lambda udid=None: udid)
    app.state.proxy_adapter = None
    app.state.flow_store = None
    monkeypatch.setattr(
        sim_tls, "passthrough_note",
        lambda _app, udid: "passed through" if udid == B else None,
    )
    return SimpleNamespace(
        app=app, client=TestClient(app),
        headers={"Authorization": "Bearer test-key-12345"},
    )


class TestEveryFlowResultCarriesTheNote:
    """Zero flows from a passed-through simulator reads exactly like an app that
    made no requests, so the note has to be on the empty answer most of all --
    and on every endpoint that can be filtered to a simulator, not two of four."""

    @pytest.fixture
    def store(self, api):
        from server.proxy.capture_session import CaptureSessionManager
        from server.proxy.flow_store import FlowStore

        api.app.state.flow_store = FlowStore()
        api.app.state.capture_sessions = CaptureSessionManager()
        return api

    def test_query_flows(self, store):
        r = store.client.get(
            "/api/v1/proxy/flows", params={"simulator_udid": B}, headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0
        assert r.json()["simulator_tls_note"] == "passed through"

    def test_query_flows_with_no_flow_store(self, api):
        r = api.client.get(
            "/api/v1/proxy/flows", params={"simulator_udid": B}, headers=api.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] == "passed through"

    def test_flow_summary(self, store):
        r = store.client.get(
            "/api/v1/proxy/flows/summary", params={"simulator_udid": B},
            headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] == "passed through"

    def test_wait_for_flow_that_times_out(self, store):
        r = store.client.post(
            "/api/v1/proxy/flows/wait",
            json={"simulator_udid": B, "timeout": 0.2, "interval": 0.1},
            headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["matched"] is False
        assert r.json()["simulator_tls_note"] == "passed through"

    def test_start_capture_session_warns_before_anything_is_captured(self, store):
        """At start the note is worth most: the agent can install the CA before
        driving the app, instead of finding the capture empty at stop.
        (CodeRabbit's linked-issue check on #357; #354 listed this endpoint.)"""
        r = store.client.post(
            "/api/v1/proxy/capture/start", json={"simulator_udid": B},
            headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] == "passed through"

    def test_start_capture_session_is_silent_for_a_decrypted_simulator(self, store):
        r = store.client.post(
            "/api/v1/proxy/capture/start", json={"simulator_udid": A},
            headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] is None

    def test_start_capture_session_is_silent_without_a_simulator(self, store):
        r = store.client.post(
            "/api/v1/proxy/capture/start", json={}, headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] is None

    def test_stop_capture_session(self, store):
        r = store.client.post(
            "/api/v1/proxy/capture/start", json={"simulator_udid": B},
            headers=store.headers,
        )
        assert r.status_code == 200, r.text
        r = store.client.post(
            "/api/v1/proxy/capture/stop", json={"session_id": r.json()["session_id"]},
            headers=store.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["simulator_tls_note"] == "passed through"


class TestQuernsOwnEventsRefreshAtOnce:
    """Driven through the endpoints, not by reading their source: a test that
    grepped for the call passed with the call under `if False:`."""

    def test_erasing_refreshes(self, api, monkeypatch):
        from server.proxy import sim_tls

        refresh = AsyncMock()
        monkeypatch.setattr(sim_tls, "refresh_after", refresh)
        monkeypatch.setattr("server.api.device._invalidate_cert_record", lambda udid: None)
        api.app.state.device_controller.erase = AsyncMock()

        r = api.client.post("/api/v1/device/erase", json={"udid": A}, headers=api.headers)
        assert r.status_code == 200, r.text
        refresh.assert_awaited_once()

    def test_booting_refreshes(self, api, monkeypatch):
        from server.proxy import sim_tls

        refresh = AsyncMock()
        monkeypatch.setattr(sim_tls, "refresh_after", refresh)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        monkeypatch.setattr(
            "server.proxy.cert_manager.is_cert_installed", AsyncMock(return_value=False),
        )
        controller = api.app.state.device_controller
        controller.boot = AsyncMock(return_value=A)
        controller._is_android = MagicMock(return_value=False)

        r = api.client.post("/api/v1/device/boot", json={"udid": A}, headers=api.headers)
        assert r.status_code == 200, r.text
        refresh.assert_awaited_once()

    async def test_a_failed_refresh_does_not_fail_the_call(self, monkeypatch):
        from server.proxy import sim_tls

        monkeypatch.setattr(sim_tls, "refresh", AsyncMock(side_effect=RuntimeError("x")))
        app = SimpleNamespace(state=SimpleNamespace(
            proxy_adapter=SimpleNamespace(local_capture=True),
        ))
        await sim_tls.refresh_after(app, "a test")  # does not raise
