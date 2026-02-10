"""Tests for the in-memory FlowStore."""

from datetime import datetime, timedelta, timezone

import pytest

from server.models import FlowQueryParams, FlowRecord, FlowRequest, FlowResponse, FlowTiming
from server.proxy.flow_store import FlowStore


def _make_flow(
    flow_id: str = "f_test",
    host: str = "api.example.com",
    path: str = "/v1/test",
    method: str = "GET",
    status_code: int = 200,
    reason: str = "OK",
    error: str | None = None,
    timestamp: datetime | None = None,
) -> FlowRecord:
    return FlowRecord(
        id=flow_id,
        timestamp=timestamp or datetime.now(timezone.utc),
        request=FlowRequest(
            method=method,
            url=f"https://{host}{path}",
            host=host,
            path=path,
        ),
        response=FlowResponse(
            status_code=status_code,
            reason=reason,
        ) if error is None else None,
        timing=FlowTiming(total_ms=100.0),
        error=error,
    )


@pytest.mark.asyncio
async def test_add_and_get():
    store = FlowStore(max_size=100)
    flow = _make_flow(flow_id="f_1")
    await store.add(flow)

    assert store.size == 1
    result = await store.get("f_1")
    assert result is not None
    assert result.id == "f_1"


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    store = FlowStore()
    result = await store.get("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_fifo_eviction():
    store = FlowStore(max_size=3)
    for i in range(5):
        await store.add(_make_flow(flow_id=f"f_{i}"))

    assert store.size == 3
    # Oldest two should be evicted
    assert await store.get("f_0") is None
    assert await store.get("f_1") is None
    # Newest three should remain
    assert await store.get("f_2") is not None
    assert await store.get("f_3") is not None
    assert await store.get("f_4") is not None


@pytest.mark.asyncio
async def test_update_existing_flow():
    store = FlowStore(max_size=10)
    flow1 = _make_flow(flow_id="f_1", status_code=200)
    await store.add(flow1)

    flow2 = _make_flow(flow_id="f_1", status_code=404)
    await store.add(flow2)

    assert store.size == 1
    result = await store.get("f_1")
    assert result is not None
    assert result.response.status_code == 404


@pytest.mark.asyncio
async def test_query_filter_host():
    store = FlowStore()
    await store.add(_make_flow(flow_id="f_1", host="api.example.com"))
    await store.add(_make_flow(flow_id="f_2", host="other.example.com"))

    flows, total = await store.query(FlowQueryParams(host="api.example.com"))
    assert total == 1
    assert flows[0].id == "f_1"


@pytest.mark.asyncio
async def test_query_filter_method():
    store = FlowStore()
    await store.add(_make_flow(flow_id="f_1", method="GET"))
    await store.add(_make_flow(flow_id="f_2", method="POST"))

    flows, total = await store.query(FlowQueryParams(method="POST"))
    assert total == 1
    assert flows[0].id == "f_2"


@pytest.mark.asyncio
async def test_query_filter_status_range():
    store = FlowStore()
    await store.add(_make_flow(flow_id="f_1", status_code=200))
    await store.add(_make_flow(flow_id="f_2", status_code=401))
    await store.add(_make_flow(flow_id="f_3", status_code=500))

    flows, total = await store.query(FlowQueryParams(status_min=400))
    assert total == 2
    ids = {f.id for f in flows}
    assert ids == {"f_2", "f_3"}


@pytest.mark.asyncio
async def test_query_filter_status_max():
    store = FlowStore()
    await store.add(_make_flow(flow_id="f_1", status_code=200))
    await store.add(_make_flow(flow_id="f_2", status_code=301))
    await store.add(_make_flow(flow_id="f_3", status_code=500))

    flows, total = await store.query(FlowQueryParams(status_max=399))
    assert total == 2
    ids = {f.id for f in flows}
    assert ids == {"f_1", "f_2"}


@pytest.mark.asyncio
async def test_query_filter_has_error():
    store = FlowStore()
    await store.add(_make_flow(flow_id="f_1"))
    await store.add(_make_flow(flow_id="f_2", error="Connection refused"))

    flows, total = await store.query(FlowQueryParams(has_error=True))
    assert total == 1
    assert flows[0].id == "f_2"

    flows, total = await store.query(FlowQueryParams(has_error=False))
    assert total == 1
    assert flows[0].id == "f_1"


@pytest.mark.asyncio
async def test_query_filter_time_range():
    store = FlowStore()
    now = datetime.now(timezone.utc)
    await store.add(_make_flow(flow_id="f_1", timestamp=now - timedelta(minutes=10)))
    await store.add(_make_flow(flow_id="f_2", timestamp=now - timedelta(minutes=5)))
    await store.add(_make_flow(flow_id="f_3", timestamp=now))

    flows, total = await store.query(FlowQueryParams(since=now - timedelta(minutes=6)))
    assert total == 2
    ids = {f.id for f in flows}
    assert ids == {"f_2", "f_3"}


@pytest.mark.asyncio
async def test_query_filter_path_contains():
    store = FlowStore()
    await store.add(_make_flow(flow_id="f_1", path="/v1/users"))
    await store.add(_make_flow(flow_id="f_2", path="/v1/login"))
    await store.add(_make_flow(flow_id="f_3", path="/v2/users"))

    flows, total = await store.query(FlowQueryParams(path_contains="users"))
    assert total == 2
    ids = {f.id for f in flows}
    assert ids == {"f_1", "f_3"}


@pytest.mark.asyncio
async def test_query_pagination():
    store = FlowStore()
    for i in range(10):
        await store.add(_make_flow(flow_id=f"f_{i}"))

    flows, total = await store.query(FlowQueryParams(limit=3, offset=0))
    assert total == 10
    assert len(flows) == 3

    flows, total = await store.query(FlowQueryParams(limit=3, offset=8))
    assert total == 10
    assert len(flows) == 2  # Only 2 remaining


@pytest.mark.asyncio
async def test_clear():
    store = FlowStore()
    for i in range(5):
        await store.add(_make_flow(flow_id=f"f_{i}"))

    assert store.size == 5
    await store.clear()
    assert store.size == 0
