"""Tests for the OSLog source adapter parser."""

from pathlib import Path

import pytest

from server.models import LogLevel, LogSource
from server.sources.oslog import OslogAdapter, _extract_process_name, _parse_oslog_timestamp


@pytest.fixture
def adapter() -> OslogAdapter:
    return OslogAdapter(device_id="test-device")


@pytest.fixture
def sample_lines() -> list[str]:
    fixture = Path(__file__).parent / "fixtures" / "oslog_sample.json"
    return fixture.read_text().strip().splitlines()


def test_parse_default_message_type(adapter: OslogAdapter, sample_lines: list[str]):
    """Default messageType should map to INFO."""
    entry = adapter._parse_json_line(sample_lines[0])

    assert entry is not None
    assert entry.level == LogLevel.INFO
    assert entry.message == "Request completed in 234ms"
    assert entry.subsystem == "com.myapp.networking"
    assert entry.category == "performance"
    assert entry.process == "MyApp"
    assert entry.pid == 1234
    assert entry.source == LogSource.OSLOG
    assert entry.device_id == "test-device"


def test_parse_error_message_type(adapter: OslogAdapter, sample_lines: list[str]):
    """Error messageType should map to ERROR."""
    entry = adapter._parse_json_line(sample_lines[1])

    assert entry is not None
    assert entry.level == LogLevel.ERROR
    assert entry.message == "Failed to fetch user profile: HTTP 401"
    assert entry.category == "auth"


def test_parse_info_message_type(adapter: OslogAdapter, sample_lines: list[str]):
    """Info messageType should map to INFO."""
    entry = adapter._parse_json_line(sample_lines[2])

    assert entry is not None
    assert entry.level == LogLevel.INFO
    assert entry.message == "viewDidLoad called for HomeViewController"


def test_parse_fault_message_type(adapter: OslogAdapter, sample_lines: list[str]):
    """Fault messageType should map to FAULT."""
    entry = adapter._parse_json_line(sample_lines[3])

    assert entry is not None
    assert entry.level == LogLevel.FAULT
    assert entry.message == "Low memory warning received"


def test_parse_debug_message_type(adapter: OslogAdapter, sample_lines: list[str]):
    """Debug messageType should map to DEBUG."""
    entry = adapter._parse_json_line(sample_lines[4])

    assert entry is not None
    assert entry.level == LogLevel.DEBUG
    assert entry.process == "OtherApp"
    assert entry.pid == 5678


def test_parse_non_json_lines(adapter: OslogAdapter):
    """Non-JSON lines (array brackets, commas) should return None."""
    assert adapter._parse_json_line("[") is None
    assert adapter._parse_json_line("]") is None
    assert adapter._parse_json_line(",") is None
    assert adapter._parse_json_line("") is None
    assert adapter._parse_json_line("not json at all") is None


def test_parse_json_with_leading_comma(adapter: OslogAdapter, sample_lines: list[str]):
    """Lines with leading commas (from JSON array format) should still parse."""
    entry = adapter._parse_json_line("," + sample_lines[0])
    assert entry is not None
    assert entry.message == "Request completed in 234ms"


def test_timestamp_parsing():
    """OSLog timestamps should be correctly parsed to UTC."""
    ts = _parse_oslog_timestamp("2026-02-07 14:23:01.234567-0800")
    assert ts.year == 2026
    assert ts.month == 2
    assert ts.day == 7
    # 14:23:01 PST (-0800) = 22:23:01 UTC
    assert ts.hour == 22
    assert ts.minute == 23


def test_process_name_extraction():
    """Process name should be extracted from the image path."""
    assert _extract_process_name(
        "/private/var/containers/Bundle/Application/ABC123/MyApp"
    ) == "MyApp"
    assert _extract_process_name("") == ""
    assert _extract_process_name("/usr/bin/logd") == "logd"


def test_build_command_no_filters(adapter: OslogAdapter):
    """Command without filters should be basic log stream."""
    cmd = adapter._build_command()
    assert cmd == ["log", "stream", "--style", "json"]


def test_build_command_with_subsystem():
    """Subsystem filter should add a predicate."""
    adapter = OslogAdapter(subsystem_filter="com.myapp.networking")
    cmd = adapter._build_command()
    assert "--predicate" in cmd
    assert 'subsystem == "com.myapp.networking"' in cmd[-1]


def test_build_command_with_both_filters():
    """Both filters should be combined with AND."""
    adapter = OslogAdapter(
        subsystem_filter="com.myapp.networking",
        process_filter="MyApp",
    )
    cmd = adapter._build_command()
    assert "--predicate" in cmd
    predicate = cmd[-1]
    assert "subsystem ==" in predicate
    assert "processImagePath ENDSWITH" in predicate
    assert " AND " in predicate
