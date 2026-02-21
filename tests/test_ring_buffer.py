"""Tests for the ring buffer storage."""

from datetime import datetime, timezone

import pytest

from server.models import LogEntry, LogLevel, LogQueryParams, LogSource
from server.storage.ring_buffer import RingBuffer


def _make_entry(
    message: str = "test message",
    level: LogLevel = LogLevel.INFO,
    process: str = "TestApp",
    source: LogSource = LogSource.SYSLOG,
    **kwargs,
) -> LogEntry:
    """Helper to create a LogEntry for testing."""
    return LogEntry(
        id="test123",
        timestamp=kwargs.get("timestamp", datetime.now(timezone.utc)),
        device_id=kwargs.get("device_id", "default"),
        process=process,
        level=level,
        message=message,
        source=source,
    )


@pytest.mark.asyncio
async def test_append_and_size():
    buf = RingBuffer(max_size=100)
    assert buf.size == 0

    await buf.append(_make_entry("first"))
    assert buf.size == 1

    await buf.append(_make_entry("second"))
    assert buf.size == 2


@pytest.mark.asyncio
async def test_ring_buffer_wraps():
    buf = RingBuffer(max_size=3)

    await buf.append(_make_entry("a"))
    await buf.append(_make_entry("b"))
    await buf.append(_make_entry("c"))
    await buf.append(_make_entry("d"))  # should evict "a"

    assert buf.size == 3
    recent = await buf.get_recent(10)
    messages = [e.message for e in recent]
    assert messages == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_query_by_level():
    buf = RingBuffer(max_size=100)

    await buf.append(_make_entry("info msg", level=LogLevel.INFO))
    await buf.append(_make_entry("error msg", level=LogLevel.ERROR))
    await buf.append(_make_entry("debug msg", level=LogLevel.DEBUG))

    params = LogQueryParams(level=LogLevel.ERROR)
    results, total = await buf.query(params)

    assert total == 1
    assert results[0].message == "error msg"


@pytest.mark.asyncio
async def test_query_by_process():
    buf = RingBuffer(max_size=100)

    await buf.append(_make_entry("app log", process="MyApp"))
    await buf.append(_make_entry("system log", process="SpringBoard"))

    params = LogQueryParams(process="MyApp")
    results, total = await buf.query(params)

    assert total == 1
    assert results[0].process == "MyApp"


@pytest.mark.asyncio
async def test_query_search():
    buf = RingBuffer(max_size=100)

    await buf.append(_make_entry("HTTP 401 Unauthorized"))
    await buf.append(_make_entry("Request succeeded"))
    await buf.append(_make_entry("HTTP 500 Server Error"))

    params = LogQueryParams(search="HTTP")
    results, total = await buf.query(params)

    assert total == 2


@pytest.mark.asyncio
async def test_query_pagination():
    buf = RingBuffer(max_size=100)

    for i in range(10):
        await buf.append(_make_entry(f"msg {i}"))

    params = LogQueryParams(limit=3, offset=0)
    results, total = await buf.query(params)
    assert total == 10
    assert len(results) == 3

    params = LogQueryParams(limit=3, offset=9)
    results, total = await buf.query(params)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_filter_entries_ignores_pagination():
    """filter_entries returns ALL matching entries, ignoring limit/offset."""
    buf = RingBuffer(max_size=100)

    for i in range(20):
        await buf.append(_make_entry(f"msg {i}", process="MyApp"))
    await buf.append(_make_entry("other", process="OtherApp"))

    # Even with limit=3 and offset=5, filter_entries should return all 20 MyApp entries
    params = LogQueryParams(process="MyApp", limit=3, offset=5)
    results = await buf.filter_entries(params)
    assert len(results) == 20
    assert all(e.process == "MyApp" for e in results)


@pytest.mark.asyncio
async def test_filter_entries_applies_filters():
    """filter_entries applies source/level/search filters."""
    buf = RingBuffer(max_size=100)

    await buf.append(_make_entry("server start", source=LogSource.SERVER, level=LogLevel.INFO))
    await buf.append(_make_entry("device log", source=LogSource.SYSLOG, level=LogLevel.INFO))
    await buf.append(_make_entry("server error", source=LogSource.SERVER, level=LogLevel.ERROR))

    params = LogQueryParams(source=LogSource.SERVER)
    results = await buf.filter_entries(params)
    assert len(results) == 2
    assert all(e.source == LogSource.SERVER for e in results)


@pytest.mark.asyncio
async def test_subscribe_receives_new_entries():
    buf = RingBuffer(max_size=100)
    queue = buf.subscribe()

    await buf.append(_make_entry("live entry"))

    entry = queue.get_nowait()
    assert entry.message == "live entry"

    buf.unsubscribe(queue)
