"""Tests for LogcatAdapter — test line parsing and lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.models import LogLevel, LogSource
from server.sources.logcat import LogcatAdapter, LOGCAT_PATTERN


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------


class TestParseLine:
    def setup_method(self):
        self.adapter = LogcatAdapter(serial="emulator-5554")

    def test_debug_line(self):
        line = "03-08 14:22:45.123  1234  5678 D MyTag  : Debug message here"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.DEBUG
        assert entry.process == "MyTag"
        assert entry.pid == 1234
        assert entry.message == "Debug message here"
        assert entry.source == LogSource.LOGCAT

    def test_info_line(self):
        line = "03-08 14:22:45.456  1234  5678 I MyTag  : Info message here"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.INFO
        assert entry.message == "Info message here"

    def test_warning_line(self):
        line = "03-08 14:22:45.789  1234  5678 W MyTag  : Warning message here"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.WARNING

    def test_error_line(self):
        line = "03-08 14:22:46.012  1234  5678 E MyTag  : Error message here"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.ERROR

    def test_fatal_line(self):
        line = "03-08 14:22:46.345  1234  5678 F MyTag  : Fatal message here"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.FAULT

    def test_verbose_line(self):
        line = "03-08 14:22:46.678  2345  6789 V SystemTag: Verbose system message"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.DEBUG
        assert entry.process == "SystemTag"
        assert entry.message == "Verbose system message"

    def test_unparseable_line_emitted_as_info(self):
        line = "some random unparseable line"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.INFO
        assert entry.message == "some random unparseable line"
        assert entry.source == LogSource.LOGCAT

    def test_activity_manager_line(self):
        line = "03-08 14:22:47.001  2345  6789 I ActivityManager: Start proc 1234:com.example.app/u0a123"
        entry = self.adapter._parse_line(line)
        assert entry is not None
        assert entry.level == LogLevel.INFO
        assert entry.process == "ActivityManager"
        assert "Start proc" in entry.message

    def test_all_fixture_lines_parse(self):
        """Every non-header line in the fixture should parse without error."""
        fixture_text = (FIXTURES_DIR / "logcat_sample.txt").read_text()
        parsed = 0
        for line in fixture_text.strip().splitlines():
            if line.startswith("---------"):
                continue
            entry = self.adapter._parse_line(line)
            assert entry is not None
            parsed += 1
        assert parsed == 7  # 7 non-header lines in fixture


# ---------------------------------------------------------------------------
# Regex pattern
# ---------------------------------------------------------------------------


class TestLogcatPattern:
    def test_matches_standard_line(self):
        line = "03-08 14:22:45.123  1234  5678 D MyTag  : message"
        m = LOGCAT_PATTERN.match(line)
        assert m is not None
        assert m.group(1) == "03-08"
        assert m.group(2) == "14:22:45.123"
        assert m.group(3) == "1234"
        assert m.group(4) == "5678"
        assert m.group(5) == "D"
        assert m.group(6) == "MyTag"
        assert m.group(7) == "message"

    def test_matches_tag_with_no_trailing_spaces(self):
        line = "03-08 14:22:45.123  1234  5678 I ActivityManager: proc started"
        m = LOGCAT_PATTERN.match(line)
        assert m is not None
        assert m.group(6) == "ActivityManager"
        assert m.group(7) == "proc started"


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_adapter_id_uses_serial_prefix(self):
        adapter = LogcatAdapter(serial="emulator-5554")
        assert adapter.adapter_id == "logcat-emulator"

    def test_adapter_type(self):
        adapter = LogcatAdapter(serial="emulator-5554")
        assert adapter.adapter_type == "adb_logcat"

    def test_source_is_logcat(self):
        adapter = LogcatAdapter(serial="emulator-5554")
        entry = adapter._parse_line("03-08 14:22:45.123  1234  5678 D Tag  : msg")
        assert entry.source == LogSource.LOGCAT
