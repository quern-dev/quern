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

import asyncio
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

    def test_an_uncached_simulator_never_matches_even_a_scope_spelt_like_one(self):
        """The addon takes its scope from a command and does not validate it,
        so the placeholder for "a simulator, UDID not cached yet" must not be
        able to match a rule whose scope happens to read the same."""
        a = _addon(("odd", addon_mod.UNKNOWN_SIMULATOR))
        flow = _flow("alpha")
        with patch.dict(addon_mod._client_process_info, {"alpha": {"pid": 7}}), \
             patch.object(addon_mod, "_simulator_instance_for_pid",
                          return_value=(addon_mod.UNKNOWN_SIMULATOR, 50)):
            _run(a, flow)
        assert _mocked_by(flow) is None

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
    a._rules_lock = asyncio.Lock()  # set by __init__, which this skips (#562)
    a.followers, a.leader = [], None  # set by __init__ too (#558)
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


class TestAnUpdateChangesOnlyWhatItNames:
    """#574: a body-only update reset the status code and headers to the
    defaults, though update_mock says only the provided fields change."""

    def _rule(self, client):
        return client.post("/api/v1/proxy/mocks", headers=AUTH, json={
            "pattern": "~d api.example.com",
            "response": {"status_code": 503, "headers": {"retry-after": "5"}, "body": "A"},
        }).json()["rule_id"]

    @pytest.mark.parametrize("payload", [
        {"response": {"body": "B"}},   # nested, as the MCP sends it
        {"body": "B"},                 # flat
    ])
    def test_a_body_only_update_keeps_status_and_headers(self, client, adapter, payload):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["response"] == {"status_code": 503, "headers": {"retry-after": "5"},
                                        "body": "B"}
        sent = adapter.send_command.await_args.args[0]
        assert sent["response"]["status_code"] == 503, "the addon gets the merged response too"

    def test_named_headers_replace_all_headers(self, client):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH,
                         json={"response": {"headers": {"x-new": "1"}}})
        assert r.json()["response"] == {"status_code": 503, "headers": {"x-new": "1"}, "body": "A"}

    def test_a_new_status_keeps_the_body(self, client):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json={"status_code": 200})
        assert r.json()["response"]["status_code"] == 200
        assert r.json()["response"]["body"] == "A"

    def test_a_named_null_body_clears_it_and_keeps_the_rest(self, client):
        # Naming overwrites, even with null (CodeRabbit): a flat body: null
        # beside another field cleared the body before #574's fix and must
        # still; the status it names changes, the headers stay.
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH,
                         json={"status_code": 201, "body": None})
        assert r.status_code == 200, r.text
        assert r.json()["response"] == {"status_code": 201, "headers": {"retry-after": "5"},
                                        "body": ""}

    def test_a_named_null_alone_is_still_an_update(self, client):
        rid = self._rule(client)
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json={"body": None})
        assert r.status_code == 200, r.text
        assert r.json()["response"]["body"] == "" and r.json()["response"]["status_code"] == 503


# ---------------------------------------------------------------------------
# Android devices (#582)
# ---------------------------------------------------------------------------

EMU = "emulator-5554"
PHONE = "8BAY0WCL7"
PHONE_IP = "192.168.1.244"


def _android_rule(a: IOSDebugAddon, rule_id: str, scope: str, client_ip: str | None = None):
    with patch("server.proxy.addon.flowfilter.parse", return_value=lambda f: True), Out():
        a._handle_set_mock({
            "rule_id": rule_id, "pattern": "~d api.example.com",
            "response": {"status_code": 200, "body": rule_id},
            "simulator_udid": scope, "client_ip": client_ip,
        })


def _emulator_conn(client_id: str, serial: str | None, pid: int = 4242):
    """A connection whose process is an emulator's QEMU, already identified
    for this process (pid and start time), so the request need not wait."""
    start = (1_700_000_000, 1)
    known = {(pid, start): serial} if serial else {}
    return (patch.dict(addon_mod._client_process_info,
                       {client_id: {"pid": pid, "process_name": "qemu-system-aarch64"}}),
            patch.multiple(addon_mod, _proc_name_fast=MagicMock(return_value="qemu-system-aarch64"),
                           _proc_start_fast=MagicMock(return_value=start)),
            patch.dict(addon_mod._scope_serials, known))


def _held(flow: MagicMock) -> MagicMock:
    """A mock flow whose intercept and resume behave like mitmproxy's."""
    flow.intercepted = False
    flow.intercept.side_effect = lambda: setattr(flow, "intercepted", True)
    flow.resume.side_effect = lambda: setattr(flow, "intercepted", False)
    return flow


def _until_resumed(flow, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while flow.intercepted and time.monotonic() < deadline:
        time.sleep(0.02)


def _emu_addon(scope: str = EMU, client_ip: str | None = None) -> IOSDebugAddon:
    a = IOSDebugAddon()
    a._running = True
    _android_rule(a, "only_emu" if client_ip is None else "only_phone", scope, client_ip)
    return a


class TestAnAndroidScopedRule:
    def test_mocks_its_emulator(self):
        a = _emu_addon()
        flow = _flow("c-emu")
        info, procs, known = _emulator_conn("c-emu", EMU)
        with info, procs, known:
            _run(a, flow)
        assert _mocked_by(flow) == "only_emu"

    def test_leaves_another_emulator_alone(self):
        a = _emu_addon()
        flow = _flow("c-other")
        info, procs, known = _emulator_conn("c-other", "emulator-5556")
        with info, procs, known:
            _run(a, flow)
        assert flow.response is None and _mocked_by(flow) is None

    def test_a_reused_pid_is_not_the_emulator_it_once_was(self):
        """Decided by pid *and* start time: a new process with an old pid is
        looked up again, not mocked as the emulator that pid belonged to."""
        a = _emu_addon()
        flow = _held(_flow("c-reused"))
        info, procs, known = _emulator_conn("c-reused", EMU)
        with info, procs, known, \
             patch.object(addon_mod, "_proc_start_fast", return_value=(1_800_000_000, 9)), \
             patch.object(addon_mod, "_emulator_serial_for_pid", return_value=None):
            a.request(flow)
            _until_resumed(flow)
        assert _mocked_by(flow) is None and not flow.intercepted

    def test_keeps_the_serials_case(self):
        a = _emu_addon("8bay0wcl7", client_ip=PHONE_IP)
        assert a._mock_rules[0]["simulator_udid"] == "8bay0wcl7"

    def test_mocks_its_phone_by_its_address(self):
        a = _emu_addon(PHONE, client_ip=PHONE_IP)
        mine, other = _flow("p1"), _flow("p2")
        mine.client_conn.peername = (PHONE_IP, 41000)
        other.client_conn.peername = ("192.168.1.250", 41000)
        _run(a, mine)
        _run(a, other)
        assert _mocked_by(mine) == "only_phone" and _mocked_by(other) is None

    @pytest.mark.parametrize("name", ["MyApp", "com.apple.WebKit.Networking", "curl"])
    def test_a_process_that_is_not_qemu_is_decided_at_once(self, name):
        """A simulator's app or a Mac process is no emulator: decided without
        holding it or running lsof (review: every matching request was held
        and paid an lsof while an emulator's rule was set). And it is never
        handed the emulator's mock, however it resolves."""
        a = _emu_addon()
        flow = _held(_flow("c-sim"))
        lsof = MagicMock(return_value=EMU)
        with patch.dict(addon_mod._client_process_info,
                        {"c-sim": {"pid": 5151, "process_name": name}}), \
             patch.object(addon_mod, "_proc_name_fast", return_value=name), \
             patch.object(addon_mod, "_emulator_serial_for_pid", lsof):
            _run(a, flow)
        flow.intercept.assert_not_called()
        lsof.assert_not_called()
        assert _mocked_by(flow) is None

    def test_an_unknown_emulator_is_resolved_off_the_loop_then_answered(self):
        """The serial needs an lsof not yet run for this process: the request
        is held, not waited on in the hook, and answered once it is known."""
        a = _emu_addon()
        flow = _held(_flow("c-emu"))
        lsof = MagicMock(side_effect=lambda pid, use_cache=True: (time.sleep(0.2), EMU)[1])
        info, procs, known = _emulator_conn("c-emu", None)
        with info, procs, known, patch.object(addon_mod, "_emulator_serial_for_pid", lsof), \
             patch("server.proxy.addon.http.Response.make", return_value=MagicMock()):
            started = time.monotonic()
            a.request(flow)
            assert time.monotonic() - started < 0.1, "the hook waited"
            assert flow.intercepted and _mocked_by(flow) is None
            _until_resumed(flow)
        assert _mocked_by(flow) == "only_emu" and not flow.intercepted
        assert lsof.call_args.kwargs == {"use_cache": False}, "decided fresh, not from a pid cache"

    def test_a_held_request_that_is_not_its_emulators_goes_on_and_is_reported(self):
        a = _emu_addon()
        flow = _held(_flow("c-x"))
        info, procs, known = _emulator_conn("c-x", None, pid=4243)
        with info, procs, known, \
             patch.object(addon_mod, "_emulator_serial_for_pid", return_value="emulator-5556"), \
             patch.object(a, "_report_start") as reported:
            a.request(flow)
            _until_resumed(flow)
        assert not flow.intercepted and _mocked_by(flow) is None and flow.response is None
        reported.assert_called_once_with(flow)

    def test_a_shut_down_pool_lets_the_request_through(self):
        a = _emu_addon()
        flow = _held(_flow("c-y"))
        info, procs, known = _emulator_conn("c-y", None)
        with info, procs, known, \
             patch.object(addon_mod._SCOPE_POOL, "submit", side_effect=RuntimeError("shut down")):
            a.request(flow)
        assert not flow.intercepted and flow.response is None

    def test_a_failure_while_answering_lets_it_through_unmocked(self):
        a = _emu_addon()
        flow = _held(_flow("c-z"))
        info, procs, known = _emulator_conn("c-z", None)
        real_answer = a._answer
        calls = []

        def answer(f, requester):
            calls.append(1)
            if len(calls) == 1:
                return real_answer(f, requester)      # the hook: defers
            f.response = MagicMock()                  # half-set, then fails
            raise RuntimeError("boom")

        with info, procs, known, patch.object(a, "_answer", side_effect=answer), \
             patch.object(addon_mod, "_emulator_serial_for_pid", return_value=EMU):
            a.request(flow)
            _until_resumed(flow)
        assert not flow.intercepted and flow.response is None


def test_a_simulator_rule_is_asked_again_once_the_lookup_settles():
    """A simulator's rule set before an emulator's: while the connection's
    lookup runs, the simulator reads as unknown and the emulator's rule holds
    the request. Answered after the lookup, the simulator is asked again,
    not taken from the stale first read (review)."""
    from concurrent.futures import Future

    a = IOSDebugAddon()
    a._running = True
    for rule_id, scope in (("only_alpha", ALPHA), ("only_emu", EMU)):
        with patch("server.proxy.addon.flowfilter.parse", return_value=lambda f: True), Out():
            a._handle_set_mock({"rule_id": rule_id, "pattern": "~d api.example.com",
                                "response": {"status_code": 200, "body": rule_id},
                                "simulator_udid": scope})
    flow = _held(_flow("c-alpha"))
    pending: Future = Future()
    with patch.dict(addon_mod._client_process_info, {"c-alpha": {"future": pending}}), \
         patch.object(addon_mod, "_simulator_instance_for_pid", return_value=(ALPHA, 50)), \
         patch.object(addon_mod, "_proc_name_fast", return_value="MobileSafari"), \
         patch("server.proxy.addon.http.Response.make", return_value=MagicMock()):
        a.request(flow)
        assert flow.intercepted
        pending.set_result((4242, "MobileSafari"))
        _until_resumed(flow)
    assert _mocked_by(flow) == "only_alpha"


class TestTheDeferredRequestOnARealEventLoop:
    """What mitmdump does: the hook runs on the event loop, and the proxy
    awaits the flow's resume. The other tests run the hook without a loop,
    which takes a fallback path (review: dropping call_soon_threadsafe left
    every deferred flow intercepted for ever, and no test noticed)."""

    async def test_it_is_answered_and_resumed_on_the_loop_thread(self):
        import threading

        from mitmproxy.test import tflow

        a = _emu_addon()
        flow = tflow.tflow()
        flow.request.host = "api.example.com"
        resumed_on: list[str] = []
        real_resume = flow.resume

        def resume():
            resumed_on.append(threading.current_thread().name)
            real_resume()

        flow.resume = resume
        info, procs, known = _emulator_conn(flow.client_conn.id, None)
        with info, procs, known, \
             patch.object(addon_mod, "_emulator_serial_for_pid", return_value=EMU), \
             patch.object(a, "_report_start"), Out():
            a.request(flow)
            assert flow.intercepted
            await asyncio.wait_for(flow.wait_for_resume(), 3)
        assert _mocked_by(flow) == "only_emu"
        assert flow.response is not None and flow.response.status_code == 200
        assert resumed_on == [threading.main_thread().name]


class TestScopingAMockToAnAndroidDevice:
    @pytest.fixture
    def android(self, client):
        kinds = {EMU: DeviceType.ANDROID_EMULATOR, PHONE: DeviceType.ANDROID_DEVICE,
                 "00008030-000C59623A69802E": DeviceType.DEVICE}
        adb = SimpleNamespace(
            is_console_serial=lambda s: s.startswith("emulator-"),
            get_lan_ip=AsyncMock(return_value=PHONE_IP),
            get_http_proxy=AsyncMock(return_value="192.168.1.188:9101"),
        )
        ctrl = client.app.state.device_controller
        ctrl._device_type = kinds.get
        ctrl.list_devices = AsyncMock(return_value=[])
        ctrl.adb = adb
        client.app.state.proxy_adapter.listen_port = 9101
        return adb

    def test_an_emulator_scope_reaches_the_addon_as_spelt(self, client, adapter, android):
        r = _set(client, simulator_udid=EMU)
        assert r.status_code == 200, r.text
        sent = adapter.send_command.await_args.args[0]
        assert (sent["simulator_udid"], sent["client_ip"]) == (EMU, None)
        assert "warning" not in r.json()

    def test_a_phone_scope_carries_its_address(self, client, adapter, android):
        r = _set(client, simulator_udid=PHONE)
        assert r.status_code == 200, r.text
        assert adapter.send_command.await_args.args[0]["client_ip"] == PHONE_IP

    def test_a_phone_with_no_known_address_is_refused(self, client, adapter, android,
                                                      monkeypatch):
        android.get_lan_ip.return_value = None
        monkeypatch.setattr("server.proxy.cert_state.read_cert_state_for_device",
                            lambda udid: None)
        r = _set(client, simulator_udid=PHONE)
        assert r.status_code == 422 and "device-proxy-config" in r.json()["detail"]
        adapter.send_command.assert_not_awaited()

    def test_a_phone_falls_back_to_its_recorded_address(self, client, adapter, android,
                                                        monkeypatch):
        android.get_lan_ip.return_value = None
        monkeypatch.setattr(
            "server.proxy.cert_state.read_cert_state_for_device",
            lambda udid: {"wifi_proxy_configs": {"home": {"client_ip": "192.168.1.77"}}})
        assert _set(client, simulator_udid=PHONE).status_code == 200
        assert adapter.send_command.await_args.args[0]["client_ip"] == "192.168.1.77"

    @pytest.mark.parametrize("scope", ["emulator-5999", "00008030-000C59623A69802E"])
    def test_an_unknown_or_ios_serial_is_refused(self, client, adapter, android, scope):
        r = _set(client, simulator_udid=scope)
        assert r.status_code == 422, r.text
        adapter.send_command.assert_not_awaited()

    def test_a_tcp_attached_emulator_is_pointed_at_its_console_serial(
            self, client, adapter, android):
        client.app.state.device_controller._device_type = (
            lambda s: DeviceType.ANDROID_DEVICE if s == "localhost:5555" else None)
        r = _set(client, simulator_udid="localhost:5555")
        assert r.status_code == 422 and "emulator-<port>" in r.json()["detail"]

    def test_an_emulator_not_routed_through_the_proxy_is_warned_about(
            self, client, android):
        android.get_http_proxy.return_value = None
        r = _set(client, simulator_udid=EMU)
        assert r.status_code == 200 and "not routed through this proxy" in r.json()["warning"]

    def test_the_answer_says_which_platform_the_scope_is(self, client, android):
        # The MCP's carried record reads this to send a copy to the right
        # kind of device after use_remote.
        assert _set(client, simulator_udid=EMU).json()["platform"] == "android"
        assert _set(client, simulator_udid=ALPHA).json()["platform"] == "ios"
        assert _set(client).json()["platform"] is None

    def test_a_proxy_on_another_port_is_warned_about(self, client, android):
        android.get_http_proxy.return_value = "192.168.1.188:9999"
        r = _set(client, simulator_udid=EMU)
        assert "not routed through this proxy" in r.json()["warning"]

    def test_a_cold_cache_lists_once_before_refusing(self, client, adapter, android):
        known: dict = {}
        ctrl = client.app.state.device_controller
        ctrl._device_type = known.get

        async def list_devices():
            known[EMU] = DeviceType.ANDROID_EMULATOR

        ctrl.list_devices = list_devices
        assert _set(client, simulator_udid=EMU).status_code == 200

    def test_a_listing_that_does_not_finish_is_could_not_tell(self, client, adapter, android,
                                                              monkeypatch):
        from server.api import proxy_intercept

        ctrl = client.app.state.device_controller
        ctrl._device_type = lambda s: None

        async def hangs():
            await asyncio.sleep(10)

        ctrl.list_devices = hangs
        monkeypatch.setattr(proxy_intercept, "_SCOPE_LISTING_TIMEOUT_S", 0.05)
        r = _set(client, simulator_udid=EMU)
        assert r.status_code == 503, r.text
        adapter.send_command.assert_not_awaited()

    @pytest.mark.parametrize("serial", ["10.0.0.5:5555", "localhost:5555"])
    def test_any_tcp_attached_emulator_is_refused(self, client, adapter, android, serial):
        client.app.state.device_controller._device_type = (
            lambda s: DeviceType.ANDROID_EMULATOR if s == serial else None)
        r = _set(client, simulator_udid=serial)
        assert r.status_code == 422 and "emulator-<port>" in r.json()["detail"]

    def test_a_bracketed_address_is_not_a_serial(self, client, adapter, android):
        assert _set(client, simulator_udid="[::1]:5555").status_code == 422
        adapter.send_command.assert_not_awaited()

    def test_the_newest_recorded_address_is_used(self, client, adapter, android, monkeypatch):
        android.get_lan_ip.return_value = None
        monkeypatch.setattr(
            "server.proxy.cert_state.read_cert_state_for_device",
            lambda udid: {"wifi_proxy_configs": {
                "home": {"client_ip": "192.168.1.77", "set_at": "2026-10-01T10:00:00+00:00"},
                "office": {"client_ip": "10.1.2.3", "set_at": "2026-10-30T10:00:00+00:00"}}})
        assert _set(client, simulator_udid=PHONE).status_code == 200
        assert adapter.send_command.await_args.args[0]["client_ip"] == "10.1.2.3"

    def test_re_scoping_away_from_a_phone_drops_its_address(self, client, adapter, android):
        rid = _set(client, simulator_udid=PHONE).json()["rule_id"]
        client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH, json={"simulator_udid": EMU})
        assert adapter._mock_rules[0]["client_ip"] is None
        assert adapter.send_command.await_args.args[0]["client_ip"] is None

    def test_updating_an_unknown_rule_is_404_before_any_scope_check(self, client, android):
        android.get_lan_ip.return_value = None
        r = client.patch("/api/v1/proxy/mocks/mock_nope", headers=AUTH,
                         json={"simulator_udid": PHONE})
        assert r.status_code == 404

    def test_list_mocks_shows_what_a_phone_rule_is_pinned_to(self, client, android):
        _set(client, simulator_udid=PHONE)
        rule = client.get("/api/v1/proxy/mocks", headers=AUTH).json()["rules"][0]
        assert rule["client_ip"] == PHONE_IP

    def test_re_scoping_to_a_phone_brings_its_address(self, client, adapter, android):
        rid = _set(client, simulator_udid=EMU).json()["rule_id"]
        r = client.patch(f"/api/v1/proxy/mocks/{rid}", headers=AUTH,
                         json={"simulator_udid": PHONE})
        assert r.status_code == 200, r.text
        assert adapter._mock_rules[0]["client_ip"] == PHONE_IP
        assert adapter.send_command.await_args.args[0]["client_ip"] == PHONE_IP


async def test_a_phones_address_is_sent_again_to_a_restarted_proxy(adapter):
    """A restarted mitmdump is sent every rule the server holds. A phone's
    rule without its address would match no request -- or, worse, be read as
    an emulator's (#582)."""
    adapter.leader = None
    adapter._bypass_patterns = []
    adapter._intercept_pattern = None
    adapter._mock_rules = [{"rule_id": "r1", "pattern": "~d x", "response": {},
                            "simulator_udid": PHONE, "client_ip": PHONE_IP}]
    assert await adapter._send_rules()
    sent = [c.args[0] for c in adapter.send_command.await_args_list
            if c.args[0].get("action") == "set_mock"]
    assert sent and sent[0]["client_ip"] == PHONE_IP
