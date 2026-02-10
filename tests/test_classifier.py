"""Tests for the log classifier."""

from datetime import datetime, timedelta, timezone

from server.models import LogEntry, LogLevel, LogSource
from server.processing.classifier import (
    detect_resolution,
    extract_pattern,
    is_noise,
)


def _ts(offset_seconds: float = 0) -> datetime:
    base = datetime(2026, 2, 7, 14, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def _make_entry(
    message: str,
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
# Pattern extraction
# ---------------------------------------------------------------------------


def test_extract_pattern_strips_numbers():
    assert extract_pattern("HTTP 401 on request 42") == "HTTP <N> on request <N>"


def test_extract_pattern_strips_uuids():
    msg = "Loading ABC12345-1234-5678-9ABC-DEF012345678 failed"
    assert "<UUID>" in extract_pattern(msg)
    assert "ABC12345-1234" not in extract_pattern(msg)


def test_extract_pattern_strips_hex():
    assert extract_pattern("Object at 0x1a2b3c released") == "Object at <HEX> released"


def test_extract_pattern_strips_ips():
    assert extract_pattern("Connected to 192.168.1.1:8080") == "Connected to <IP>"


def test_extract_pattern_preserves_words():
    """Words with numbers inside them should still get normalized."""
    result = extract_pattern("Failed to fetch user profile")
    assert result == "Failed to fetch user profile"


def test_extract_pattern_same_template_for_variants():
    """Messages differing only in numbers should produce the same template."""
    a = extract_pattern("Request 123 failed with status 500")
    b = extract_pattern("Request 456 failed with status 502")
    assert a == b


# ---------------------------------------------------------------------------
# Noise detection
# ---------------------------------------------------------------------------


def test_noise_sandbox_deny():
    entry = _make_entry("Sandbox: MyApp(1234) deny(1) mach-lookup com.apple.foo")
    assert is_noise(entry) is True


def test_noise_boringssl():
    entry = _make_entry("boringssl_context_set_handshake_log: NULL")
    assert is_noise(entry) is True


def test_not_noise_normal_error():
    entry = _make_entry("Failed to fetch user profile: HTTP 401")
    assert is_noise(entry) is False


def test_not_noise_app_message():
    entry = _make_entry("viewDidLoad called for HomeViewController")
    assert is_noise(entry) is False


# ---------------------------------------------------------------------------
# Resolution detection
# ---------------------------------------------------------------------------


def test_detect_resolution_error_then_success():
    entries = [
        _make_entry("HTTP 401 Unauthorized", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("Token refresh succeeded", level=LogLevel.INFO, timestamp=_ts(5)),
    ]
    resolutions = detect_resolution(entries)

    assert len(resolutions) == 1
    assert resolutions[0]["error_count"] == 1
    assert "succeeded" in resolutions[0]["resolution_message"]


def test_detect_resolution_multiple_errors_before_success():
    entries = [
        _make_entry("HTTP 401 error", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("HTTP 401 error", level=LogLevel.ERROR, timestamp=_ts(1)),
        _make_entry("HTTP 401 error", level=LogLevel.ERROR, timestamp=_ts(2), repeat_count=3),
        _make_entry("Auth token refreshed successfully", level=LogLevel.INFO, timestamp=_ts(5)),
    ]
    resolutions = detect_resolution(entries)

    assert len(resolutions) == 1
    # 1 + 1 + 3 = 5 total errors (repeat_count is respected)
    assert resolutions[0]["error_count"] == 5


def test_detect_resolution_no_success():
    entries = [
        _make_entry("HTTP 500 Server Error", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("Another info log", level=LogLevel.INFO, timestamp=_ts(1)),
    ]
    resolutions = detect_resolution(entries)
    assert len(resolutions) == 0


def test_detect_resolution_different_processes_not_resolved():
    """Success from a different process should not resolve errors."""
    entries = [
        _make_entry("connection failed", process="AppA", level=LogLevel.ERROR, timestamp=_ts(0)),
        _make_entry("connection succeeded", process="AppB", level=LogLevel.INFO, timestamp=_ts(1)),
    ]
    resolutions = detect_resolution(entries)
    assert len(resolutions) == 0
