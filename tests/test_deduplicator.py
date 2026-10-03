"""Tests for the log entry deduplicator."""

from datetime import UTC, datetime, timedelta

import pytest

from server.models import LogEntry, LogLevel, LogSource
from server.processing.deduplicator import Deduplicator


def _ts(offset_seconds: float = 0) -> datetime:
    """Create a UTC timestamp with an offset from a fixed base time."""
    base = datetime(2026, 2, 7, 14, 0, 0, tzinfo=UTC)
    return base + timedelta(seconds=offset_seconds)


def _make_entry(
    message: str = "test error",
    process: str = "MyApp",
    level: LogLevel = LogLevel.ERROR,
    timestamp: datetime | None = None,
) -> LogEntry:
    return LogEntry(
        id="test",
        timestamp=timestamp or _ts(),
        process=process,
        level=level,
        message=message,
        source=LogSource.SYSLOG,
    )


@pytest.mark.asyncio
async def test_first_occurrence_emitted():
    """The first occurrence of a message should be emitted immediately."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)
    await dedup.process(_make_entry("error A", timestamp=_ts(0)))

    assert len(emitted) == 1
    assert emitted[0].message == "error A"
    assert emitted[0].repeat_count == 1  # default, not a summary


@pytest.mark.asyncio
async def test_duplicate_suppressed_within_window():
    """Duplicate messages within the window should be suppressed."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)

    await dedup.process(_make_entry("error A", timestamp=_ts(0)))
    await dedup.process(_make_entry("error A", timestamp=_ts(1)))
    await dedup.process(_make_entry("error A", timestamp=_ts(2)))

    # Only the first should have been emitted so far
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_summary_emitted_after_window_expires():
    """After the window expires, a summary with repeat_count should be emitted."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)

    await dedup.process(_make_entry("error A", timestamp=_ts(0)))
    await dedup.process(_make_entry("error A", timestamp=_ts(1)))
    await dedup.process(_make_entry("error A", timestamp=_ts(2)))

    # Trigger flush by sending an entry well after the window
    await dedup.process(_make_entry("error B", timestamp=_ts(10)))

    # Should have: first "error A", summary for "error A" (2 repeats), first "error B"
    assert len(emitted) == 3
    summary = emitted[1]
    assert summary.message == "error A"  # original message preserved
    assert summary.repeat_count == 2  # 2 suppressed duplicates


@pytest.mark.asyncio
async def test_different_messages_not_deduplicated():
    """Different messages should each be emitted independently."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)

    await dedup.process(_make_entry("error A", timestamp=_ts(0)))
    await dedup.process(_make_entry("error B", timestamp=_ts(1)))
    await dedup.process(_make_entry("error C", timestamp=_ts(2)))

    assert len(emitted) == 3


@pytest.mark.asyncio
async def test_different_process_not_deduplicated():
    """Same message from different processes should not be deduplicated."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)

    await dedup.process(_make_entry("error A", process="AppA", timestamp=_ts(0)))
    await dedup.process(_make_entry("error A", process="AppB", timestamp=_ts(1)))

    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_max_suppressed_forces_emit():
    """Hitting max_suppressed should force-emit a summary."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=60.0, max_suppressed=5)

    for i in range(5):
        await dedup.process(_make_entry("spam", timestamp=_ts(i * 0.1)))

    # First occurrence + forced summary when count hits max_suppressed
    assert len(emitted) == 2
    assert emitted[1].repeat_count == 4  # 5 total - 1 first = 4 suppressed


@pytest.mark.asyncio
async def test_flush_all():
    """flush_all should emit summaries for all pending buckets."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=60.0)

    await dedup.process(_make_entry("error A", timestamp=_ts(0)))
    await dedup.process(_make_entry("error A", timestamp=_ts(1)))
    await dedup.process(_make_entry("error A", timestamp=_ts(2)))

    assert len(emitted) == 1  # only first

    await dedup.flush_all()

    assert len(emitted) == 2
    assert emitted[1].repeat_count == 2


@pytest.mark.asyncio
async def test_single_occurrence_no_summary_on_flush():
    """A message seen only once should not produce a summary on flush."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)

    dedup = Deduplicator(on_entry=capture, window_seconds=60.0)

    await dedup.process(_make_entry("unique msg", timestamp=_ts(0)))
    await dedup.flush_all()

    # Only the original entry, no summary
    assert len(emitted) == 1
    assert emitted[0].repeat_count == 1


@pytest.mark.asyncio
async def test_a_summary_keeps_the_sender():
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)
    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)
    first = _make_entry("error A", timestamp=_ts(0)).model_copy(
        update={"subsystem": "com.apple.CFNetwork", "sender": "CFNetwork"})
    await dedup.process(first)
    await dedup.process(first.model_copy(update={"timestamp": _ts(1)}))
    await dedup.process(_make_entry("error B", timestamp=_ts(10)))
    summary = emitted[1]
    assert summary.repeat_count == 1 and summary.sender == "CFNetwork"


@pytest.mark.asyncio
async def test_the_same_text_from_another_sender_or_level_is_its_own_entry():
    """Two libraries saying the same thing are two entries; and an ERROR
    after a DEBUG of the same text must not be folded into the DEBUG one,
    which a filter quieting below error would then drop (CodeRabbit)."""
    emitted: list[LogEntry] = []

    async def capture(entry: LogEntry) -> None:
        emitted.append(entry)
    dedup = Deduplicator(on_entry=capture, window_seconds=5.0)
    base = _make_entry("connection reset", timestamp=_ts(0))
    await dedup.process(base.model_copy(update={"sender": "Network", "level": LogLevel.DEBUG}))
    await dedup.process(base.model_copy(update={"sender": "CFNetwork", "level": LogLevel.DEBUG,
                                                "timestamp": _ts(1)}))
    await dedup.process(base.model_copy(update={"sender": "Network", "level": LogLevel.ERROR,
                                                "timestamp": _ts(2)}))
    assert [(e.sender, e.level) for e in emitted] == [
        ("Network", LogLevel.DEBUG), ("CFNetwork", LogLevel.DEBUG), ("Network", LogLevel.ERROR)]

