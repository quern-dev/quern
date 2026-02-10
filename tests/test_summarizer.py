"""Tests for the template-based log summarizer."""

from datetime import datetime, timedelta, timezone

from server.models import LogEntry, LogLevel, LogSource
from server.processing.summarizer import generate_summary, make_cursor, parse_cursor


def _ts(offset_seconds: float = 0) -> datetime:
    base = datetime(2026, 2, 7, 14, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def _make_entry(
    message: str = "test message",
    process: str = "MyApp",
    level: LogLevel = LogLevel.INFO,
    timestamp: datetime | None = None,
    repeat_count: int = 1,
) -> LogEntry:
    return LogEntry(
        id="test",
        timestamp=timestamp or _ts(),
        process=process,
        level=level,
        message=message,
        source=LogSource.SYSLOG,
        repeat_count=repeat_count,
    )


# ---------------------------------------------------------------------------
# Cursor round-trip
# ---------------------------------------------------------------------------


def test_cursor_roundtrip():
    ts = datetime(2026, 2, 7, 14, 23, 1, 234567, tzinfo=timezone.utc)
    cursor = make_cursor(ts)
    assert cursor.startswith("c_")

    decoded = parse_cursor(cursor)
    assert decoded is not None
    # Microsecond precision should survive the round-trip
    assert abs((decoded - ts).total_seconds()) < 0.001


def test_parse_cursor_invalid():
    assert parse_cursor("not_a_cursor") is None
    assert parse_cursor("c_!!!invalid!!!") is None
    assert parse_cursor("") is None


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------


def test_summary_empty_entries():
    result = generate_summary([], window="5m")

    assert result.total_count == 0
    assert result.error_count == 0
    assert result.warning_count == 0
    assert result.window == "5m"
    assert "no log entries" in result.summary.lower()
    assert result.top_issues == []


def test_summary_counts_by_level():
    entries = [
        _make_entry("info 1", level=LogLevel.INFO, timestamp=_ts(0)),
        _make_entry("info 2", level=LogLevel.INFO, timestamp=_ts(1)),
        _make_entry("warning 1", level=LogLevel.WARNING, timestamp=_ts(2)),
        _make_entry("error 1", level=LogLevel.ERROR, timestamp=_ts(3)),
        _make_entry("fault 1", level=LogLevel.FAULT, timestamp=_ts(4)),
    ]
    result = generate_summary(entries, window="5m")

    assert result.total_count == 5
    assert result.error_count == 2  # ERROR + FAULT
    assert result.warning_count == 1


def test_summary_respects_repeat_count():
    entries = [
        _make_entry("spam error", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("spam error", level=LogLevel.ERROR, timestamp=_ts(1), repeat_count=9),
    ]
    result = generate_summary(entries, window="5m")

    assert result.total_count == 10  # 1 + 9
    assert result.error_count == 10
    assert len(result.top_issues) == 1
    assert result.top_issues[0].count == 10


def test_summary_groups_errors_by_pattern():
    entries = [
        _make_entry("HTTP 401 on request 1", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("HTTP 401 on request 2", level=LogLevel.ERROR, timestamp=_ts(1)),
        _make_entry("Connection timeout at 10.0.0.1", level=LogLevel.ERROR, timestamp=_ts(2)),
    ]
    result = generate_summary(entries, window="5m")

    assert result.error_count == 3
    # Two distinct patterns: HTTP 401 (2x) and Connection timeout (1x)
    assert len(result.top_issues) == 2
    # Sorted by count descending
    assert result.top_issues[0].count == 2
    assert result.top_issues[1].count == 1


def test_summary_detects_resolution():
    entries = [
        _make_entry("HTTP 401 Unauthorized", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("HTTP 401 Unauthorized", level=LogLevel.ERROR, timestamp=_ts(1)),
        _make_entry("Token refresh succeeded", level=LogLevel.INFO, timestamp=_ts(5)),
    ]
    result = generate_summary(entries, window="5m")

    resolved_issues = [i for i in result.top_issues if i.resolved]
    assert len(resolved_issues) == 1
    assert "resolved" in result.summary.lower()


def test_summary_filters_by_process():
    entries = [
        _make_entry("app log", process="MyApp", level=LogLevel.INFO, timestamp=_ts(0)),
        _make_entry("system log", process="SpringBoard", level=LogLevel.INFO, timestamp=_ts(1)),
    ]
    result = generate_summary(entries, window="5m", process="MyApp")

    assert result.total_count == 1


def test_summary_cursor_advances():
    entries = [
        _make_entry("msg", timestamp=_ts(0)),
        _make_entry("msg", timestamp=_ts(10)),
    ]
    result = generate_summary(entries, window="5m")

    # Cursor should be based on the latest entry's timestamp
    decoded = parse_cursor(result.cursor)
    assert decoded is not None
    assert decoded == _ts(10)


def test_summary_prose_no_errors():
    entries = [
        _make_entry("all good", level=LogLevel.INFO, timestamp=_ts(0)),
        _make_entry("still good", level=LogLevel.INFO, timestamp=_ts(1)),
    ]
    result = generate_summary(entries, window="1m")

    assert "no errors" in result.summary.lower()
    assert result.error_count == 0
