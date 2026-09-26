"""A flow answer says when the flow store may have evicted part of it (#318).

The store holds 5,000 flows and evicts the oldest-completed at capacity.
Until this change nothing reading it could tell "no request to /login" from
"the request to /login was evicted" -- the same silent true-negative #255
removed from the log buffers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.proxy.flow_store import FlowStore
from tests.test_flow_store import _make_flow

HEADERS = {"Authorization": "Bearer test-key-12345"}


def _flow(flow_id, *, ago_s=0.0, udid=None, ip=None, host="api.example.com", path="/v1/x"):
    flow = _make_flow(
        flow_id=flow_id, host=host, path=path,
        timestamp=datetime.now(UTC) - timedelta(seconds=ago_s),
    )
    flow.simulator_udid = udid
    flow.client_ip = ip
    return flow


@pytest.fixture
def app():
    app = create_app(
        config=ServerConfig(api_key="test-key-12345"),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    app.state.flow_store = FlowStore(max_size=5)
    # Created by the lifespan, which these tests do not run.
    from server.proxy.capture_session import CaptureSessionManager
    app.state.capture_sessions = CaptureSessionManager()
    return app


async def _call(app, method, path, **kwargs):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.request(method, path, headers=HEADERS, **kwargs)
    return resp


async def _flood(store, count, **kwargs):
    for i in range(count):
        await store.add(_flow(f"noise{i}-{kwargs.get('udid')}", ago_s=(count - i) * 0.01, **kwargs))


class TestTheStore:
    async def test_another_devices_eviction_does_not_count(self):
        store = FlowStore(max_size=1)
        await store.add(_flow("a", udid="SIM-A", ago_s=10))
        await store.add(_flow("b", udid="SIM-B"))      # evicts SIM-A's flow

        since = datetime.now(UTC) - timedelta(minutes=1)
        assert not store.is_complete_since(since, simulator_udid="SIM-A")
        assert store.is_complete_since(since, simulator_udid="SIM-B")
        assert not store.is_complete_since(since)       # globally, something went

    async def test_a_flow_is_recorded_under_both_its_device_fields(self):
        store = FlowStore(max_size=1)
        await store.add(_flow("a", udid="SIM-A", ip="127.0.0.1", ago_s=10))
        await store.add(_flow("b"))

        since = datetime.now(UTC) - timedelta(minutes=1)
        assert not store.is_complete_since(since, client_ip="127.0.0.1")
        assert not store.is_complete_since(since, simulator_udid="SIM-A")

    async def test_one_ips_eviction_does_not_flag_another_ip(self):
        """Physical devices are told apart by client_ip. A test that only
        checked an evicted ip passed with the ip key removed altogether,
        because the query then fell back to the global mark -- which was also
        set. Here the two disagree."""
        store = FlowStore(max_size=1)
        await store.add(_flow("a", ip="10.0.0.1", ago_s=10))
        await store.add(_flow("b", ip="10.0.0.2"))      # evicts 10.0.0.1's flow

        since = datetime.now(UTC) - timedelta(minutes=1)
        assert store.is_complete_since(since, client_ip="10.0.0.2")
        assert not store.is_complete_since(since, client_ip="10.0.0.1")

    async def test_stats_separate_intake_from_what_survived(self):
        store = FlowStore(max_size=2)
        for i in range(5):
            await store.add(_flow(f"f{i}"))
        await store.add(_flow("f4"))                     # an update, not intake

        stats = store.stats()
        assert (stats["capacity"], stats["size"], stats["added"], stats["evicted"]) == (2, 2, 5, 3)


class TestQueryFlows:
    async def test_an_evicted_match_reads_as_truncated_not_absent(self, app):
        """The case in the issue: the request happened, the store turned over,
        and the query found nothing."""
        store = app.state.flow_store
        await store.add(_flow("login", path="/login", ago_s=10))
        await _flood(store, 5)

        data = (await _call(
            app, "GET", "/api/v1/proxy/flows", params={"path_contains": "/login"},
        )).json()

        assert data["total"] == 0
        assert data["truncated"] is True
        assert data["complete_after"] is not None

    async def test_a_quiet_store_is_a_true_negative(self, app):
        await _flood(app.state.flow_store, 3)

        data = (await _call(
            app, "GET", "/api/v1/proxy/flows", params={"path_contains": "/login"},
        )).json()

        assert data["total"] == 0 and data["truncated"] is False

    async def test_another_devices_eviction_does_not_flag_this_one(self, app):
        store = app.state.flow_store
        await store.add(_flow("mine", udid="SIM-A", ago_s=10))
        await _flood(store, 5, udid="SIM-B")             # evicts SIM-A's too...
        await store.add(_flow("mine2", udid="SIM-C"))

        other = (await _call(app, "GET", "/api/v1/proxy/flows",
                             params={"simulator_udid": "SIM-C"})).json()
        mine = (await _call(app, "GET", "/api/v1/proxy/flows",
                            params={"simulator_udid": "SIM-A"})).json()

        assert other["truncated"] is False
        assert mine["truncated"] is True

    async def test_a_full_first_page_is_whole(self, app):
        """The store evicts in completion order and pages newest-first by
        completion, so the newest N are always all there."""
        await _flood(app.state.flow_store, 10)

        data = (await _call(app, "GET", "/api/v1/proxy/flows", params={"limit": 3})).json()

        assert len(data["flows"]) == 3
        assert data["truncated"] is False

    async def test_a_later_page_after_eviction_is_not(self, app):
        await _flood(app.state.flow_store, 10)

        data = (await _call(app, "GET", "/api/v1/proxy/flows",
                            params={"limit": 3, "offset": 1})).json()

        # A full page, so only the offset separates it from a first page --
        # at offset 3 of 5 the page came back short and the test could not
        # tell whether the offset condition existed at all.
        assert len(data["flows"]) == 3
        assert data["truncated"] is True


class TestSummary:
    async def test_truncation_is_in_the_prose_as_well_as_the_field(self, app):
        await _flood(app.state.flow_store, 10)

        data = (await _call(app, "GET", "/api/v1/proxy/flows/summary")).json()

        assert data["truncated"] is True
        assert data["summary"].startswith("Flows in this window were evicted")

    async def test_a_whole_window_says_nothing_extra(self, app):
        await _flood(app.state.flow_store, 3)

        data = (await _call(app, "GET", "/api/v1/proxy/flows/summary")).json()

        assert data["truncated"] is False and "evicted" not in data["summary"]


class TestWaitForFlow:
    async def test_a_timeout_after_the_match_was_evicted_says_so(self, app):
        """A matching flow can arrive and be evicted between polls; the wait
        then reported that the request never happened."""
        store = app.state.flow_store
        await store.add(_flow("login", path="/login", ago_s=1))
        await _flood(store, 5)

        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/login", "timeout": 0.2,
                                  "interval": 0.1})).json()

        assert data["matched"] is False
        assert data["truncated"] is True

    async def test_a_quiet_timeout_is_a_true_negative(self, app):
        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/login", "timeout": 0.2,
                                  "interval": 0.1})).json()

        assert data["matched"] is False
        assert data["truncated"] is False


class TestCaptureSession:
    async def test_flows_evicted_during_the_session_are_reported(self, app):
        start = (await _call(app, "POST", "/api/v1/proxy/capture/start", json={})).json()
        store = app.state.flow_store
        await store.add(_flow("during"))
        await _flood(store, 5)                           # evicts "during"

        data = (await _call(app, "POST", "/api/v1/proxy/capture/stop",
                            json={"session_id": start["session_id"]})).json()

        assert data["truncated"] is True

    async def test_a_session_with_nothing_evicted_is_whole(self, app):
        start = (await _call(app, "POST", "/api/v1/proxy/capture/start", json={})).json()
        await app.state.flow_store.add(_flow("during"))

        data = (await _call(app, "POST", "/api/v1/proxy/capture/stop",
                            json={"session_id": start["session_id"]})).json()

        assert data["truncated"] is False and data["total_flows"] == 1


async def test_a_missing_flow_says_it_may_have_been_evicted(app):
    await _flood(app.state.flow_store, 10)

    resp = await _call(app, "GET", "/api/v1/proxy/flows/gone")

    assert resp.status_code == 404
    assert "may have been evicted" in resp.json()["detail"]


async def test_a_missing_flow_in_a_store_that_never_evicted_is_just_missing(app):
    resp = await _call(app, "GET", "/api/v1/proxy/flows/gone")

    assert resp.status_code == 404
    assert "evicted" not in resp.json()["detail"]


async def test_proxy_status_reports_what_the_store_took_in_and_lost(app):
    """`flows_captured` is what survived, and stays at capacity on a busy
    proxy while traffic is lost. The stats say how much."""
    await _flood(app.state.flow_store, 8)

    data = (await _call(app, "GET", "/api/v1/proxy/status")).json()

    stats = data["flow_store"]
    assert (stats["capacity"], stats["added"], stats["evicted"]) == (5, 8, 3)
    assert data["flows_captured"] == 5
