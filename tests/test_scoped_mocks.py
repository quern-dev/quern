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
    """Patch the two lookups `_flow_simulator_udid` makes, per client id."""
    pids = {cid: 1000 + i for i, cid in enumerate(udid_by_client)}
    udids = {pids[cid]: udid for cid, udid in udid_by_client.items()}
    return (
        patch.object(addon_mod, "_lookup_process_info",
                     side_effect=lambda cid: {"pid": pids[cid]} if cid in pids else None),
        patch.object(addon_mod, "_resolve_simulator_udid",
                     side_effect=lambda pid: udids.get(pid)),
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

    @pytest.mark.parametrize("why", ["no process info", "no pid", "no simulator", "raises"])
    def test_fails_closed_when_the_simulator_cannot_be_told(self, why):
        a = _addon(("only_alpha", ALPHA))
        flow = _flow("alpha")
        lookup = {
            "no process info": lambda cid: None,
            "no pid": lambda cid: {"pid": None},
            "no simulator": lambda cid: {"pid": 4242},
            "raises": MagicMock(side_effect=OSError("lookup failed")),
        }[why]
        with patch.object(addon_mod, "_lookup_process_info", side_effect=lookup), \
             patch.object(addon_mod, "_resolve_simulator_udid", return_value=None):
            _run(a, flow)
        assert flow.response is None and _mocked_by(flow) is None

    def test_scope_is_compared_without_case(self):
        a = _addon(("only_alpha", ALPHA.lower()))
        flow = _flow("alpha")
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup, resolve:
            _run(a, flow)
        assert _mocked_by(flow) == "only_alpha"

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
        lookup, resolve = _attributed_to({"alpha": ALPHA})
        with lookup as looked, resolve:
            _run(a, flow)
        assert _mocked_by(flow) == "r3"
        assert looked.call_count == 1


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
        with lookup, resolve, Out() as out:
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
