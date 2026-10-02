"""A mock can be scoped to one simulator, and a mocked request is recorded once (#374).

The case this exists for: a CI Mac captures every simulator's traffic for later
analysis, while an agent mocks one route on one other simulator. The CI
simulators may hit the same endpoint and must get the real response.

mitmproxy's filter language cannot express "this simulator": `~src` sees
127.0.0.1 for all of them, since they share the Mac's network stack (measured).
The addon already attributes each connection to its simulator through the
process that opened it, so a scoped rule decides on that -- and fails closed:
a request whose simulator cannot be told is never mocked by a scoped rule.
"""

from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.main import create_app
from server.models import DeviceInfo, DeviceState, DeviceType
from server.proxy import addon as addon_mod
from server.proxy.addon import IOSDebugAddon

ALPHA = "32593887-01F3-47B8-AF34-B2D444DB37FA"
BRAVO = "C50EEEA6-F45D-4E9E-9DC6-F804F6BA0146"


# ---------------------------------------------------------------------------
# Addon
# ---------------------------------------------------------------------------


class Out:
    def __init__(self):
        self.lines: list[dict] = []

    def __enter__(self):
        self._orig = sys.stdout.buffer.write

        def write(data: bytes) -> int:
            text = data.decode("utf-8").strip()
            if text:
                self.lines.append(json.loads(text))
            return len(data)

        sys.stdout.buffer.write = write
        return self

    def __exit__(self, *exc):
        sys.stdout.buffer.write = self._orig


def _flow(client_id: str = "c1", host: str = "api.example.com") -> MagicMock:
    flow = MagicMock()
    flow.request.pretty_host = host
    flow.request.pretty_url = f"https://{host}/v1/route"
    flow.request.method = "GET"
    flow.request.path = "/v1/route"
    flow.request.scheme = "https"
    flow.request.raw_content = b""
    flow.request.headers.items.return_value = []
    flow.request.timestamp_start = time.time()
    flow.response = None
    flow.error = None
    flow.client_conn.id = client_id
    flow.client_conn.peername = ("127.0.0.1", 50000)
    flow.metadata = {}
    return flow


def _addon(*rules: tuple[str, str | None]) -> IOSDebugAddon:
    """An addon holding rules (rule_id, scope), each matching any request."""
    a = IOSDebugAddon()
    a._running = True
    for rule_id, scope in rules:
        with patch("server.proxy.addon.flowfilter.parse", return_value=lambda f: True):
            with Out():
                a._handle_set_mock({
                    "rule_id": rule_id, "pattern": "~d api.example.com",
                    "response": {"status_code": 200, "body": rule_id},
                    "simulator_udid": scope,
                })
    return a


def _attributed_to(udid_by_client: dict[str, str | None]):
    """Each client id's connection, as the addon records it, in a simulator.

    The connection's process info goes into the addon's real table, so
    `_settled_process_info` is exercised; only the walk of the live process
    tree is patched. Returns two context managers, entered together.
    """
    pids = {cid: 1000 + i for i, cid in enumerate(udid_by_client)}
    udids = {pids[cid]: udid for cid, udid in udid_by_client.items()}
    table = {cid: {"pid": pid, "process_name": "App"} for cid, pid in pids.items()}
    return (
        patch.dict(addon_mod._client_process_info, table),
        patch.object(addon_mod, "_simulator_instance_for_pid",
                     side_effect=lambda pid: (udids.get(pid), 50 if udids.get(pid) else None)),
    )


def _mocked_by(flow: MagicMock) -> str | None:
    mark = flow.metadata.get("quern_mock")
    return mark["rule_id"] if mark else None


def _run(a: IOSDebugAddon, flow: MagicMock) -> None:
    with patch("server.proxy.addon.http.Response.make", return_value=MagicMock()):
        a.request(flow)


class TestAScopedRule:
    def test_mocks_its_simulator(self):
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup, resolve:
            _run(a, flow)
        assert _mocked_by(flow) == "only_alpha"

    def test_leaves_another_simulator_alone(self):
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("bravo")
        lookup, resolve = _attributed_to({"bravo": BRAVO})
        with lookup, resolve:
            _run(a, flow)
        assert flow.response is None and _mocked_by(flow) is None

    @pytest.mark.parametrize("why", [
        "no process info", "no pid", "not a simulator", "udid not cached yet",
        "walk unfinished", "raises",
    ])
    def test_fails_closed_when_the_simulator_cannot_be_told(self, why):
        """Each case alone. The walk answers ALPHA unless the case is about the
        walk, so only the guard named can be what stops the mock."""
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        info = {} if why == "no process info" else {
            "alpha": {"pid": None if why == "no pid" else 4242, "process_name": "App"},
        }
        walk = {
            "not a simulator": (None, None),
            "udid not cached yet": (addon_mod.UNKNOWN_SIMULATOR, 50),
            "walk unfinished": (addon_mod.UNKNOWN_SIMULATOR, None),
        }.get(why, (ALPHA, 50))
        walker = (MagicMock(side_effect=OSError("libproc failed")) if why == "raises"
                  else MagicMock(return_value=walk))
        with patch.dict(addon_mod._client_process_info, info), \
             patch.object(addon_mod, "_simulator_instance_for_pid", walker):
            _run(a, flow)
        assert flow.response is None and _mocked_by(flow) is None

    def test_a_lookup_still_running_is_not_waited_for(self):
        """The system proxy's socket lookup may still be running when the
        request arrives. Waiting blocked mitmproxy's event loop for up to 0.5s
        per request, every connection with it; now it is "cannot tell"."""
        from concurrent.futures import Future

        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        pending: Future = Future()
        with patch.dict(addon_mod._client_process_info, {"alpha": {"future": pending}}), \
             patch.object(addon_mod, "_simulator_instance_for_pid", return_value=(ALPHA, 50)):
            started = time.monotonic()
            _run(a, flow)
            waited = time.monotonic() - started
        assert waited < 0.1
        assert _mocked_by(flow) is None

    def test_a_finished_lookup_is_used(self):
        from concurrent.futures import Future

        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        done: Future = Future()
        done.set_result((4242, "App"))
        with patch.dict(addon_mod._client_process_info, {"alpha": {"future": done}}), \
             patch.object(addon_mod, "_simulator_instance_for_pid", return_value=(ALPHA, 50)):
            _run(a, flow)
        assert _mocked_by(flow) == "only_alpha"

    def test_the_decision_never_reads_the_pid_cache(self):
        """A reused pid: the cache says ALPHA (the pid's previous owner), the
        live tree says BRAVO. ALPHA's mock must not answer BRAVO (#374 review)."""
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("bravo")
        lookup, walk = _attributed_to({"bravo": BRAVO})
        with lookup, walk, patch.object(addon_mod, "_resolve_simulator_udid",
                                        return_value=ALPHA) as cached:
            _run(a, flow)
        assert _mocked_by(flow) is None
        cached.assert_not_called()

    def test_the_requesters_udid_is_compared_without_case(self):
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        with patch.dict(addon_mod._client_process_info, {"alpha": {"pid": 7}}), \
             patch.object(addon_mod, "_simulator_instance_for_pid",
                          return_value=(ALPHA.lower(), 50)):
            _run(a, flow)
        assert _mocked_by(flow) == "only_alpha"

    def test_scope_is_compared_without_case(self):
        a = _addon(("only_alpha", ALPHA.lower()))
        flow = _flow("alpha")
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup, resolve:
            _run(a, flow)
        assert _mocked_by(flow) == "only_alpha"

    def test_a_scoped_rule_beats_an_earlier_catch_all(self):
        """A catch-all set first used to shadow the scoped rule for good."""
        a = _addon(("everyone", None), ("only_alpha", ALPHA))
        flow = _flow("alpha")
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup, resolve:
            _run(a, flow)
        assert _mocked_by(flow) == "only_alpha"

    def test_re_setting_a_rule_replaces_it_in_place(self):
        a = _addon(("only_alpha", ALPHA), ("also_alpha", ALPHA))
        with patch("server.proxy.addon.flowfilter.parse", return_value=lambda f: True), Out():
            a._handle_set_mock({"rule_id": "only_alpha", "pattern": "~d x",
                                "response": {"body": "new"}, "simulator_udid": ALPHA})
        assert [r["rule_id"] for r in a._mock_rules] == ["only_alpha", "also_alpha"]
        assert a._mock_rules[0]["response"] == {"body": "new"}

    def test_a_scoped_miss_falls_through_to_a_later_rule(self):
        a = _addon(("only_alpha", ALPHA), ("everyone", None))
        flow = _flow("bravo")
        lookup, resolve = _attributed_to({"bravo": BRAVO})
        with lookup, resolve:
            _run(a, flow)
        assert _mocked_by(flow) == "everyone"

    def test_the_simulator_is_looked_up_once_per_request(self):
        a = _addon(("r1", BRAVO), ("r2", BRAVO), ("r3", ALPHA))
        flow = _flow("alpha")
        lookup, walk = _attributed_to({"alpha": ALPHA})
        with lookup, walk as walked:
            _run(a, flow)
        assert _mocked_by(flow) == "r3"
        assert walked.call_count == 1


class TestAnUnscopedRule:
    def test_mocks_every_simulator_and_looks_nothing_up(self):
        a = _addon(("everyone", None))
        for cid in ("alpha", "bravo"):
            flow = _flow(cid)
            with patch.object(addon_mod, "_lookup_process_info") as looked:
                _run(a, flow)
            assert _mocked_by(flow) == "everyone"
            looked.assert_not_called()


class TestAMockedRequestIsRecordedOnce:
    def test_the_request_hook_writes_nothing_and_the_response_hook_writes_one_flow(self):
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup, resolve, Out() as out, \
             patch.object(addon_mod, "_resolve_simulator_udid", return_value=ALPHA):
            _run(a, flow)
            assert out.lines == []
            with patch.object(addon_mod, "_serialize_response", return_value={}), \
                 patch.object(addon_mod, "_compute_timing", return_value={}), \
                 patch.object(addon_mod, "_get_tls_info", return_value=None):
                a.response(flow)
        assert [line["type"] for line in out.lines] == ["flow"]
        assert out.lines[0]["mock_rule_id"] == "only_alpha"
        assert out.lines[0]["simulator_udid"] == ALPHA

    def test_an_unmocked_flow_carries_no_marker(self):
        a = _addon()
        flow = _flow("alpha")
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup, resolve, Out() as out, \
             patch.object(addon_mod, "_serialize_response", return_value={}), \
             patch.object(addon_mod, "_compute_timing", return_value={}), \
             patch.object(addon_mod, "_get_tls_info", return_value=None):
            a.response(flow)
        assert "mock_rule_id" not in out.lines[0]

    @pytest.mark.parametrize("metadata", [MagicMock(), None, "x"])
    def test_a_non_dict_metadata_is_no_marker(self, metadata):
        """None too: reading it unchecked raised inside the response hook,
        which would lose the flow's record altogether."""
        flow = _flow()
        flow.metadata = metadata
        assert addon_mod._mock_marker(flow) is None


# ---------------------------------------------------------------------------
# API and adapter
# ---------------------------------------------------------------------------

KEY = "k"
AUTH = {"Authorization": f"Bearer {KEY}"}


def _sim(udid: str, state: DeviceState, name: str = "iPhone") -> DeviceInfo:
    return DeviceInfo(udid=udid, name=name, state=state,
                      device_type=DeviceType.SIMULATOR, os_version="iOS 18.6")


@pytest.fixture
def adapter():
    from server.sources.proxy import ProxyAdapter

    a = ProxyAdapter.__new__(ProxyAdapter)
    a._mock_rules = []
    a.send_command = AsyncMock()
    a._running = True
    a.local_capture_processes = ["MockProbe"]
    return a


@pytest.fixture
def sims():
    return [_sim(ALPHA, DeviceState.BOOTED, "alpha"), _sim(BRAVO, DeviceState.SHUTDOWN, "bravo")]


@pytest.fixture
def client(adapter, sims):
    app = create_app(config=ServerConfig(api_key=KEY), enable_oslog=False,
                     enable_crash=False, enable_proxy=False)
    app.state.proxy_adapter = adapter
    app.state.flow_store = None
    # The listing the scope warning reads, and nothing else of a controller.
    app.state.device_controller = SimpleNamespace(
        simctl=SimpleNamespace(list_devices=AsyncMock(return_value=sims)),
    )
    return TestClient(app)


def _set(client, **extra):
    return client.post("/api/v1/proxy/mocks", headers=AUTH,
                       json={"pattern": "~d example.com", "body": "x", **extra})


class TestSettingAScope:
    def test_the_scope_reaches_the_addon_upper_cased(self, client, adapter):
        r = _set(client, simulator_udid=ALPHA.lower())
        assert r.status_code == 200 and r.json()["simulator_udid"] == ALPHA
        assert "warning" not in r.json()
        assert adapter.send_command.await_args.args[0]["simulator_udid"] == ALPHA

    def test_unscoped_sends_none(self, client, adapter):
        r = _set(client)
        assert r.json()["simulator_udid"] is None
        assert adapter.send_command.await_args.args[0]["simulator_udid"] is None

    @pytest.mark.parametrize("bad", ["", "alpha", "32593887-01F3-47B8-AF34", 42])
    def test_a_malformed_udid_is_refused(self, client, adapter, bad):
        r = _set(client, simulator_udid=bad)
        assert r.status_code == 422
        adapter.send_command.assert_not_awaited()

    def test_a_shut_down_simulator_is_accepted_with_a_warning(self, client):
        r = _set(client, simulator_udid=BRAVO)
        assert r.status_code == 200 and "not booted" in r.json()["warning"]

    def test_an_unknown_simulator_is_accepted_with_a_warning(self, client):
        r = _set(client, simulator_udid="00000000-0000-0000-0000-000000000000")
        assert "No simulator with UDID" in r.json()["warning"]

    def test_an_empty_listing_is_could_not_check_not_unknown(self, client, sims):
        sims.clear()
        r = _set(client, simulator_udid=ALPHA)
        assert r.json()["warning"].startswith("Could not list")

    def test_list_mocks_shows_the_scope(self, client):
        _set(client, simulator_udid=ALPHA)
        _set(client)
        rules = client.get("/api/v1/proxy/mocks", headers=AUTH).json()["rules"]
        assert [r["simulator_udid"] for r in rules] == [ALPHA, None]


class TestUpdatingAScope:
    def _rule(self, client):
        return _set(client, simulator_udid=ALPHA).json()["rule_id"]

    def test_omitting_the_scope_keeps_it(self, client, adapter):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json={"body": "new"})
        assert r.json()["simulator_udid"] == ALPHA
        assert adapter.send_command.await_args.args[0]["simulator_udid"] == ALPHA

    def test_null_clears_it(self, client, adapter):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH,
                         json={"simulator_udid": None})
        assert r.status_code == 200 and r.json()["simulator_udid"] is None
        assert adapter.send_command.await_args.args[0]["simulator_udid"] is None

    def test_a_new_udid_replaces_it(self, client):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH,
                         json={"simulator_udid": BRAVO})
        assert r.json()["simulator_udid"] == BRAVO and "not booted" in r.json()["warning"]

    def test_naming_nothing_is_still_refused(self, client):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json={})
        assert r.status_code == 400


class TestTheWarningSaysWhyAScopedMockMayNotFire:
    def test_local_capture_off(self, client, adapter):
        adapter.local_capture_processes = []
        r = _set(client, simulator_udid=ALPHA)
        assert "Local capture is off" in r.json()["warning"]

    def test_https_passed_through_for_that_simulator(self, client):
        from server.models import SimulatorTls

        passed = [SimulatorTls(udid=ALPHA, name="alpha", tls="passed_through",
                               reason="does not trust the mitmproxy CA", fix=None,
                               connections_passed_through=0, last_host=None)]
        with patch("server.proxy.sim_tls.report", return_value=passed):
            r = _set(client, simulator_udid=ALPHA)
        assert "passed through" in r.json()["warning"]

    def test_a_decrypted_simulator_says_nothing_about_tls(self, client):
        from server.models import SimulatorTls

        ok = [SimulatorTls(udid=ALPHA, name="alpha", tls="decrypted", reason=None, fix=None,
                           connections_passed_through=0, last_host=None)]
        with patch("server.proxy.sim_tls.report", return_value=ok):
            r = _set(client, simulator_udid=ALPHA)
        assert "warning" not in r.json()

    def test_a_hung_listing_does_not_hang_the_response(self, client):
        import asyncio

        async def hang():
            await asyncio.sleep(60)

        client.app.state.device_controller.simctl.list_devices = hang
        with patch("server.api.proxy_intercept._SCOPE_LISTING_TIMEOUT_S", 0.05):
            started = time.monotonic()
            r = _set(client, simulator_udid=ALPHA)
        assert time.monotonic() - started < 5
        assert r.status_code == 200 and r.json()["warning"].startswith("Could not list")

    def test_a_failing_listing_is_could_not_check_not_a_500(self, client):
        from server.models import DeviceError

        client.app.state.device_controller.simctl.list_devices = AsyncMock(
            side_effect=DeviceError("simctl broke", tool="simctl"),
        )
        r = _set(client, simulator_udid=ALPHA)
        assert r.status_code == 200 and r.json()["warning"].startswith("Could not list")

    def test_an_unscoped_update_warns_about_nothing(self, client):
        """The warning is about the scope, so an update that leaves the scope
        alone says nothing even when that scope's simulator is shut down."""
        rid = _set(client, simulator_udid=BRAVO).json()["rule_id"]
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json={"body": "b"})
        assert "warning" not in r.json()


class TestAnUpdateKeepsItsPlace:
    async def test_the_adapter_replaces_in_place_and_sends_no_clear(self, adapter):
        await adapter.set_mock("~d a", {"body": "1"}, rule_id="only_alpha", simulator_udid=ALPHA)
        await adapter.set_mock("~d a", {"body": "2"}, rule_id="everyone")
        adapter.send_command.reset_mock()
        await adapter.update_mock("only_alpha", response={"body": "new"})
        assert [r["rule_id"] for r in adapter._mock_rules] == ["only_alpha", "everyone"]
        actions = [c.args[0]["action"] for c in adapter.send_command.await_args_list]
        assert actions == ["set_mock"]


class TestThePidCacheSurvivesPidReuse:
    """Flows are filed by `_resolve_simulator_udid`, whose cache was keyed by
    pid alone: a pid reused by another simulator's process inherited the
    first's UDID, for as long as the proxy ran."""

    def test_a_reused_pid_is_resolved_again(self):
        launchd = {11: ALPHA, 22: BRAVO}
        parent = {5000: 11}
        starts = iter([(100, 0), (100, 0), (200, 0)])
        with patch.dict(addon_mod._launchd_sim_cache, launchd, clear=True), \
             patch.dict(addon_mod._pid_to_udid_cache, clear=True), \
             patch.object(addon_mod, "_get_ppid", side_effect=lambda p: parent.get(p)), \
             patch.object(addon_mod, "_proc_start_fast", side_effect=lambda p: next(starts)), \
             patch.object(addon_mod, "_refresh_launchd_sim_cache"):
            assert addon_mod._resolve_simulator_udid(5000) == ALPHA
            assert addon_mod._resolve_simulator_udid(5000) == ALPHA  # cached, same process
            parent[5000] = 22  # the pid now belongs to a process in BRAVO
            assert addon_mod._resolve_simulator_udid(5000) == BRAVO

    def test_no_start_time_means_no_caching(self):
        with patch.dict(addon_mod._launchd_sim_cache, {11: ALPHA}, clear=True), \
             patch.dict(addon_mod._pid_to_udid_cache, clear=True), \
             patch.object(addon_mod, "_get_ppid", return_value=11), \
             patch.object(addon_mod, "_proc_start_fast", return_value=None):
            assert addon_mod._resolve_simulator_udid(5000) == ALPHA
            assert addon_mod._pid_to_udid_cache == {}
