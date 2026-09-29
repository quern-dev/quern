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


def _flow(flow_id, *, ago_s=0.0, udid=None, ip=None, serial=None,
          host="api.example.com", path="/v1/x"):
    flow = _make_flow(
        flow_id=flow_id, host=host, path=path,
        timestamp=datetime.now(UTC) - timedelta(seconds=ago_s),
    )
    flow.simulator_udid = udid
    flow.client_ip = ip
    flow.device_serial = serial
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

    async def test_a_devices_mark_is_its_newest_eviction_not_its_first(self):
        """Keeping the first-evicted stamp would let a recent window read
        complete after a later eviction for the same device -- a false
        all-clear that every other test missed (review mutation M16)."""
        store = FlowStore(max_size=1)
        await store.add(_flow("old", udid="SIM-A", ago_s=600))
        await store.add(_flow("recent", udid="SIM-A", ago_s=5))   # evicts "old"
        await store.add(_flow("filler", udid="SIM-B"))            # evicts "recent"

        window = datetime.now(UTC) - timedelta(seconds=30)
        assert not store.is_complete_since(window, simulator_udid="SIM-A")

    async def test_trimming_the_device_map_never_makes_an_answer_complete(self, monkeypatch):
        """Past the cap, a dropped device's mark folds into a floor, so its
        queries stay flagged rather than reading complete."""
        from server.proxy import flow_store as fs

        monkeypatch.setattr(fs, "MAX_DEVICE_KEYS", 2)
        store = FlowStore(max_size=1)
        await store.add(_flow("a", udid="SIM-A", ago_s=5))
        for dev in ("SIM-B", "SIM-C", "SIM-D"):      # each evicts the one before
            await store.add(_flow(f"x-{dev}", udid=dev))

        assert len(store._evicted_through_by_device) <= 2
        window = datetime.now(UTC) - timedelta(seconds=30)
        assert not store.is_complete_since(window, simulator_udid="SIM-A")

    async def test_stats_report_the_span_by_timestamp_not_position(self):
        """The store holds flows in completion order, and a flow is stamped
        when its request started; the oldest held is not the first held
        (review mutation M20)."""
        store = FlowStore(max_size=10)
        await store.add(_flow("late", ago_s=1))
        await store.add(_flow("long", ago_s=300))    # started long ago, finished now
        stats = store.stats()
        assert stats["oldest"] < stats["newest"]
        assert stats["oldest"] == min(f.timestamp for f in store._flows.values()).isoformat()

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

    async def test_a_full_first_page_still_says_the_answer_is_incomplete(self, app):
        """The page is the newest N and all present, but `total` and
        `has_more` count only survivors. Exempting it answered "total 5,
        has_more false, truncated false" for 8 matching flows (review)."""
        store = app.state.flow_store
        for i in range(8):
            await store.add(_flow(f"sync{i}", path="/sync", ago_s=(8 - i) * 0.01))

        data = (await _call(app, "GET", "/api/v1/proxy/flows",
                            params={"path_contains": "/sync", "limit": 5})).json()

        assert (data["total"], data["has_more"]) == (5, False)
        assert data["truncated"] is True

    async def test_an_old_eviction_does_not_flag_a_recent_window(self, app):
        """Every other eviction test floods flows stamped a moment ago, so no
        test could tell `since` being passed from `since` being dropped."""
        store = app.state.flow_store
        for i in range(6):                                  # all stamped 10 min ago
            await store.add(_flow(f"old{i}", ago_s=600 - i))
        await store.add(_flow("fresh", path="/login"))

        recent = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        scoped = (await _call(app, "GET", "/api/v1/proxy/flows",
                              params={"path_contains": "/login", "since": recent})).json()
        unscoped = (await _call(app, "GET", "/api/v1/proxy/flows",
                                params={"path_contains": "/login"})).json()

        assert scoped["truncated"] is False
        assert unscoped["truncated"] is True

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

    async def test_a_match_still_reports_the_real_mark(self, app):
        """`complete_after: null` means nothing was ever evicted; a match
        after evictions returned it anyway (review)."""
        store = app.state.flow_store
        await _flood(store, 6)                                 # one eviction
        await store.add(_flow("hit", path="/login"))

        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/login", "timeout": 0.5,
                                  "interval": 0.1})).json()

        assert data["matched"] is True
        assert data["truncated"] is False
        assert data["complete_after"] is not None

    async def test_a_quiet_timeout_is_a_true_negative(self, app):
        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/login", "timeout": 0.2,
                                  "interval": 0.1})).json()

        assert data["matched"] is False
        assert data["truncated"] is False

    async def test_another_emulators_eviction_does_not_flag_this_wait(self, app):
        """The wait forwarded `device_serial` to the query but not to its own
        completeness check, so the check got no keys at all and fell back to
        the global eviction mark: any other device shedding traffic made this
        one's timeout read `truncated` (#262, review of #333).

        No `client_ip` here on purpose -- that is the caller the bug needed,
        and the one an agent writes when it knows the serial.
        """
        await _flood(app.state.flow_store, 6, serial="emulator-5554")

        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/login",
                                  "device_serial": "emulator-5556",
                                  "timeout": 0.2, "interval": 0.1})).json()

        assert data["matched"] is False
        # emulator-5556 has sent nothing and lost nothing.
        assert data["truncated"] is False
        assert data["complete_after"] is None

    async def test_a_match_reports_only_this_emulators_mark(self, app):
        """Same omission on the matched branch, where it lands in
        `complete_after` -- a mark for traffic this device never sent."""
        store = app.state.flow_store
        await _flood(store, 6, serial="emulator-5554")        # one eviction
        await store.add(_flow("hit", serial="emulator-5556", path="/login"))

        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/login",
                                  "device_serial": "emulator-5556",
                                  "timeout": 0.5, "interval": 0.1})).json()

        assert data["matched"] is True
        assert data["complete_after"] is None


class TestCaptureSession:
    async def test_flows_evicted_during_the_session_are_reported(self, app):
        start = (await _call(app, "POST", "/api/v1/proxy/capture/start", json={})).json()
        store = app.state.flow_store
        await store.add(_flow("during"))
        await _flood(store, 5)                           # evicts "during"

        data = (await _call(app, "POST", "/api/v1/proxy/capture/stop",
                            json={"session_id": start["session_id"]})).json()

        assert data["truncated"] is True
        assert data["complete_after"] is not None   # the real mark, not null (M21)

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


class TestEachEndpointScopesItsCheck:
    """Every endpoint passes its window and its device filter to the check.
    The review dropped each of those arguments in turn and nothing failed,
    because every eviction in the other tests was stamped a moment ago and
    every query was unfiltered."""

    @staticmethod
    async def _old_evictions(store, **device):
        """Fill the store with flows stamped 10 minutes ago, then push them out."""
        for i in range(5):
            await store.add(_flow(f"old{i}", ago_s=600 - i, **device))
        for i in range(5):
            await store.add(_flow(f"newer{i}", ago_s=300 - i, **device))

    @staticmethod
    async def _recent_eviction(store, **device):
        await store.add(_flow("gone", **device))
        await _flood(store, 5, udid="SIM-OTHER")

    # -- get_flow_summary ------------------------------------------------------

    async def test_summary_window_ignores_older_evictions(self, app):
        await self._old_evictions(app.state.flow_store)
        data = (await _call(app, "GET", "/api/v1/proxy/flows/summary",
                            params={"window": "1m"})).json()
        assert data["truncated"] is False

    async def test_summary_for_one_device_ignores_anothers_evictions(self, app):
        await self._recent_eviction(app.state.flow_store, udid="SIM-A")
        mine = (await _call(app, "GET", "/api/v1/proxy/flows/summary",
                            params={"simulator_udid": "SIM-OTHER"})).json()
        theirs = (await _call(app, "GET", "/api/v1/proxy/flows/summary",
                              params={"simulator_udid": "SIM-A"})).json()
        assert mine["truncated"] is False and theirs["truncated"] is True

    # -- wait_for_flow ---------------------------------------------------------

    async def test_wait_ignores_evictions_from_before_it_began(self, app):
        await self._old_evictions(app.state.flow_store)
        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/never", "timeout": 0.2,
                                  "interval": 0.1})).json()
        assert data["matched"] is False and data["truncated"] is False

    async def test_wait_for_one_device_ignores_anothers_evictions(self, app):
        await _flood(app.state.flow_store, 8, udid="SIM-OTHER")
        data = (await _call(app, "POST", "/api/v1/proxy/flows/wait",
                            json={"path_contains": "/never", "simulator_udid": "SIM-A",
                                  "timeout": 0.2, "interval": 0.1})).json()
        assert data["matched"] is False and data["truncated"] is False

    # -- stop_capture_session --------------------------------------------------

    async def test_capture_ignores_evictions_from_before_the_session(self, app):
        """Flows stamped before the session are not part of it, even if they
        were evicted while it ran."""
        store = app.state.flow_store
        for i in range(5):
            await store.add(_flow(f"before{i}", ago_s=600 - i))
        start = (await _call(app, "POST", "/api/v1/proxy/capture/start", json={})).json()
        for i in range(5):                                   # evicts the old ones
            await store.add(_flow(f"during{i}"))

        data = (await _call(app, "POST", "/api/v1/proxy/capture/stop",
                            json={"session_id": start["session_id"]})).json()

        assert data["total_flows"] == 5 and data["truncated"] is False

    async def test_capture_for_one_device_is_clean_when_only_others_were_evicted(self, app):
        """The other device's flow is evicted *during* the session, so only
        the device narrowing -- not the window -- keeps this clean. With the
        eviction before the session the window alone made it clean, and the
        test passed with the narrowing removed (mutation M1)."""
        store = app.state.flow_store
        start = (await _call(app, "POST", "/api/v1/proxy/capture/start",
                             json={"simulator_udid": "SIM-A"})).json()
        # Stamped now, strictly after the start. `_flood` back-dates its flows
        # by up to 50ms, which put the evicted one before the session began,
        # and the window then made the test pass with no narrowing at all.
        for i in range(5):
            await store.add(_flow(f"other{i}", udid="SIM-OTHER", ago_s=0))
        await store.add(_flow("mine", udid="SIM-A"))          # evicts a SIM-OTHER flow

        data = (await _call(app, "POST", "/api/v1/proxy/capture/stop",
                            json={"session_id": start["session_id"]})).json()

        assert data["total_flows"] == 1 and data["truncated"] is False
        assert data["complete_after"] is None       # SIM-A lost nothing (M21)

    # -- query_flows by client_ip ----------------------------------------------

    async def test_query_by_ip_ignores_another_ips_evictions(self, app):
        store = app.state.flow_store
        await store.add(_flow("theirs", ip="10.0.0.1"))
        await _flood(store, 5)                                # evicts 10.0.0.1's flow
        await store.add(_flow("mine", ip="10.0.0.2"))

        mine = (await _call(app, "GET", "/api/v1/proxy/flows",
                            params={"client_ip": "10.0.0.2"})).json()
        theirs = (await _call(app, "GET", "/api/v1/proxy/flows",
                              params={"client_ip": "10.0.0.1"})).json()

        assert mine["truncated"] is False and theirs["truncated"] is True
        # The narrowed mark, not the global one (review mutation M22).
        assert mine["complete_after"] is None and theirs["complete_after"] is not None


@pytest.mark.parametrize("state", ["running", "error", "stopped"])
async def test_proxy_status_reports_store_stats_in_every_production_branch(state):
    """With a proxy adapter present -- which the lifespan always creates --
    status goes through the running, error or final stopped branch. The one
    status test there was ran only the adapter-is-None branch, which
    production never reaches; dropping the stats from the others failed
    nothing (review mutations M11, M12)."""
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from server.api.proxy import router

    adapter = MagicMock()
    adapter.is_running = state == "running"
    adapter._error = "boom" if state == "error" else None
    adapter.listen_port, adapter.listen_host = 9101, "0.0.0.0"
    adapter.started_at = datetime.now(UTC)
    adapter._intercept_pattern, adapter._held_flows, adapter._mock_rules = None, {}, []

    store = FlowStore(max_size=2)
    for i in range(3):
        await store.add(_flow(f"f{i}"))

    app = FastAPI()
    app.include_router(router)
    app.state.proxy_adapter = adapter
    app.state.flow_store = store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        data = (await client.get("/api/v1/proxy/status")).json()

    assert data["flow_store"]["evicted"] == 1 and data["flow_store"]["added"] == 3
    assert data["flows_captured"] == 2
