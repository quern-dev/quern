"""Tests for the ring buffer storage."""

from datetime import UTC, datetime

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
        timestamp=kwargs.get("timestamp", datetime.now(UTC)),
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
async def test_purge_removes_non_matching():
    buf = RingBuffer(max_size=100)

    await buf.append(_make_entry("keep", process="MyApp"))
    await buf.append(_make_entry("drop", process="noisyd"))
    await buf.append(_make_entry("keep2", process="MyApp"))
    await buf.append(_make_entry("drop2", process="wifid"))

    removed = await buf.purge(lambda e: e.process == "MyApp")
    assert removed == 2
    assert buf.size == 2

    recent = await buf.get_recent(10)
    assert all(e.process == "MyApp" for e in recent)


@pytest.mark.asyncio
async def test_purge_empty_buffer():
    buf = RingBuffer(max_size=100)
    removed = await buf.purge(lambda e: True)
    assert removed == 0


@pytest.mark.asyncio
async def test_query_tail_returns_newest():
    buf = RingBuffer(max_size=100)

    for i in range(10):
        await buf.append(_make_entry(f"msg {i}"))

    params = LogQueryParams(limit=3, tail=True)
    results, total = await buf.query(params)

    assert total == 10
    assert len(results) == 3
    assert [e.message for e in results] == ["msg 7", "msg 8", "msg 9"]


@pytest.mark.asyncio
async def test_query_tail_with_filter():
    buf = RingBuffer(max_size=100)

    for i in range(10):
        proc = "MyApp" if i % 2 == 0 else "Other"
        await buf.append(_make_entry(f"msg {i}", process=proc))

    params = LogQueryParams(process="MyApp", limit=2, tail=True)
    results, total = await buf.query(params)

    assert total == 5  # 5 MyApp entries
    assert len(results) == 2
    # Should be the last 2 MyApp entries: msg 6, msg 8
    assert [e.message for e in results] == ["msg 6", "msg 8"]


@pytest.mark.asyncio
async def test_subscribe_receives_new_entries():
    buf = RingBuffer(max_size=100)
    queue = buf.subscribe()

    await buf.append(_make_entry("live entry"))

    entry = queue.get_nowait()
    assert entry.message == "live entry"

    buf.unsubscribe(queue)


# ---------------------------------------------------------------------------
# Eviction is counted (#255)
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def _at(seconds, source=LogSource.SYSLOG, level=LogLevel.INFO):
    return _make_entry(
        source=source, level=level, timestamp=T0 + timedelta(seconds=seconds),
    )


class TestEvictionIsCounted:
    """A `deque(maxlen=)` drops its oldest entry without a word. These are the
    numbers that let a reader tell "nothing matched" from "it was evicted"."""

    @pytest.mark.asyncio
    async def test_intake_and_evictions_are_counted(self):
        buffer = RingBuffer(max_size=3)
        for i in range(5):
            await buffer.append(_at(i))

        stats = buffer.stats()
        assert stats["appended"] == 5
        assert stats["evicted"] == 2
        assert stats["size"] == 3

    @pytest.mark.asyncio
    async def test_evictions_are_keyed_by_the_source_value_a_query_uses(self):
        """`LogSource` is a (str, Enum); `str()` of one is `LogSource.SYSLOG`
        on 3.11+, a spelling no query parameter accepts."""
        buffer = RingBuffer(max_size=1)
        await buffer.append(_at(0, source=LogSource.SYSLOG))
        await buffer.append(_at(1, source=LogSource.SIMULATOR))
        await buffer.append(_at(2))

        assert buffer.stats()["evicted_by_source"] == {"syslog": 1, "simulator": 1}

    @pytest.mark.asyncio
    async def test_a_buffer_that_never_overflowed_evicted_nothing(self):
        buffer = RingBuffer(max_size=3)
        for i in range(3):  # exactly full
            await buffer.append(_at(i))

        assert buffer.stats()["evicted"] == 0
        assert buffer.evicted_through() is None
        assert buffer.is_complete_since(None)

    @pytest.mark.asyncio
    async def test_the_mark_is_the_newest_evicted_timestamp_not_the_last(self):
        """Entries arrive out of timestamp order. Recording the most recent
        eviction instead of the newest-stamped one would let a window claim
        completeness over an entry that is gone."""
        buffer = RingBuffer(max_size=1)
        await buffer.append(_at(50))   # evicted first
        await buffer.append(_at(10))   # evicted second, but stamped earlier
        await buffer.append(_at(60))

        assert buffer.evicted_through() == T0 + timedelta(seconds=50)
        assert not buffer.is_complete_since(T0 + timedelta(seconds=20))


class TestIsCompleteSince:
    async def _evicting(self, *stamps, **kwargs):
        buffer = RingBuffer(max_size=1)
        for s in stamps:
            await buffer.append(_at(s, **kwargs))
        await buffer.append(_at(1000, **kwargs))  # pushes the last one out
        return buffer

    @pytest.mark.asyncio
    async def test_a_window_after_the_mark_is_complete(self):
        buffer = await self._evicting(10)
        assert buffer.is_complete_since(T0 + timedelta(seconds=11))

    @pytest.mark.asyncio
    async def test_a_window_starting_exactly_at_the_mark_is_not(self):
        """The evicted entry was stamped at the mark, so a window starting
        there included it."""
        buffer = await self._evicting(10)
        assert not buffer.is_complete_since(T0 + timedelta(seconds=10))

    @pytest.mark.asyncio
    async def test_all_time_is_incomplete_once_anything_was_evicted(self):
        buffer = await self._evicting(10)
        assert not buffer.is_complete_since(None)

    @pytest.mark.asyncio
    async def test_another_sources_eviction_does_not_count(self):
        buffer = await self._evicting(10, source=LogSource.BUILD)
        # the pusher is BUILD too; ask about simulator lines
        assert buffer.is_complete_since(T0, [LogSource.SIMULATOR])
        assert not buffer.is_complete_since(T0, [LogSource.BUILD])

    @pytest.mark.asyncio
    async def test_evicting_debug_lines_loses_no_errors(self):
        """A busy buffer sheds debug lines constantly. If that made every
        errors-only question incomplete, the flag would always be on and
        carry no information."""
        buffer = RingBuffer(max_size=1)
        await buffer.append(_at(10, level=LogLevel.DEBUG))
        await buffer.append(_at(11, level=LogLevel.DEBUG))

        assert buffer.is_complete_since(T0, min_level=LogLevel.ERROR)
        assert not buffer.is_complete_since(T0)

    @pytest.mark.asyncio
    async def test_evicting_an_error_is_reported_to_an_errors_query(self):
        buffer = RingBuffer(max_size=1)
        await buffer.append(_at(10, level=LogLevel.FAULT))
        await buffer.append(_at(11, level=LogLevel.DEBUG))

        assert not buffer.is_complete_since(T0, min_level=LogLevel.ERROR)


class TestPurgeIsNotEviction:
    """A purge follows a filter change the caller made, and the call that did
    it already reports the count. An entry the caller's own filter excluded is
    not a lost answer, so it must not make queries read as truncated."""

    @pytest.mark.asyncio
    async def test_a_purge_is_counted_apart(self):
        buffer = RingBuffer(max_size=10)
        for i in range(4):
            await buffer.append(_at(i, level=LogLevel.DEBUG if i % 2 else LogLevel.INFO))

        removed = await buffer.purge(lambda e: e.level != LogLevel.DEBUG)

        stats = buffer.stats()
        assert removed == 2
        assert stats["purged"] == 2
        assert stats["evicted"] == 0
        assert buffer.is_complete_since(None)

    @pytest.mark.asyncio
    async def test_eviction_is_remembered_across_a_purge(self):
        """The purge rebuilds the deque. The record of what overflow lost
        must survive that, or a filter change would erase the evidence."""
        buffer = RingBuffer(max_size=2)
        for i in range(3):  # t=0 evicted
            await buffer.append(_at(i))
        await buffer.purge(lambda e: False)

        assert buffer.stats()["evicted"] == 1
        assert not buffer.is_complete_since(T0)


@pytest.mark.asyncio
async def test_stats_report_the_span_by_timestamp_not_position():
    buffer = RingBuffer(max_size=10)
    await buffer.append(_at(30))
    await buffer.append(_at(5))    # older, but arrived later
    await buffer.append(_at(20))

    stats = buffer.stats()
    assert stats["oldest"] == (T0 + timedelta(seconds=5)).isoformat()
    assert stats["newest"] == (T0 + timedelta(seconds=30)).isoformat()


@pytest.mark.asyncio
async def test_a_zero_capacity_buffer_counts_what_it_cannot_hold():
    """`--buffer-size 0` is accepted by the config. Reading `[0]` of the empty
    deque to record the eviction raised IndexError into every adapter."""
    buffer = RingBuffer(max_size=0)
    await buffer.append(_at(5))

    assert buffer.stats()["evicted"] == 1
    assert not buffer.is_complete_since(T0)
