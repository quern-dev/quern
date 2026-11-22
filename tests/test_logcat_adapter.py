"""Tests for LogcatAdapter — test line parsing and lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

from server.models import LogLevel, LogSource
from server.sources.logcat import LOGCAT_PATTERN, STARTUP_GRACE_S, LogcatAdapter

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
        line = "03-08 14:22:47.001  2345  6789 I ActivityManager: Start proc 1234:com.example.app/u0a123"  # noqa: E501
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


# ---------------------------------------------------------------------------
# Finding adb
# ---------------------------------------------------------------------------


class TestFindingAdb:
    """Capture finds adb as the rest of quern does. It used PATH alone, and a
    server started by the menu-bar app has no platform-tools on its PATH when
    adb came with Android Studio: every other Android call worked and capture
    said "adb not found on PATH" (measured on this Mac's own server)."""

    @staticmethod
    def _no_adb_on_path(monkeypatch, tmp_path, *, sdk_has_adb: bool):
        import shutil

        from server.device.android import adb as adb_mod

        sdk = tmp_path / "Library" / "Android" / "sdk"
        if sdk_has_adb:
            (sdk / "platform-tools").mkdir(parents=True)
            (sdk / "platform-tools" / "adb").write_text("#!/bin/sh\n")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.delenv("ANDROID_HOME", raising=False)
        monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
        monkeypatch.setattr(adb_mod, "_SDK_SEARCH_PATHS", [sdk])
        return str(sdk / "platform-tools" / "adb")

    @staticmethod
    def _fake_exec(monkeypatch, calls):
        import asyncio

        class _Getprop:
            returncode = 0

            async def communicate(self):
                return b"23\n", b""

        class _Logcat:
            """Runs until terminated, as a live logcat does."""

            def __init__(self):
                self.returncode = None
                self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
                self._exited = asyncio.Event()

            async def wait(self):
                await self._exited.wait()
                return 0

            def terminate(self):
                for stream in (self.stdout, self.stderr):
                    stream.feed_eof()
                self.returncode = 0
                self._exited.set()

            kill = terminate

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _Getprop() if "getprop" in args else _Logcat()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(LogcatAdapter, "_read_loop", lambda self: asyncio.sleep(0))

    async def test_adb_in_the_android_sdk_is_used_when_it_is_not_on_path(
        self, monkeypatch, tmp_path,
    ):
        adb = self._no_adb_on_path(monkeypatch, tmp_path, sdk_has_adb=True)
        calls: list = []
        self._fake_exec(monkeypatch, calls)

        adapter = LogcatAdapter(serial="08649a85")
        # Bounded here, not only by start()'s own grace: a test that hangs
        # when that guard is removed is not a working test (CR).
        await asyncio.wait_for(adapter.start(), timeout=STARTUP_GRACE_S + 5)

        assert adapter._error is None, adapter._error
        assert [c[0] for c in calls] == [adb, adb], "getprop and logcat both use it"
        assert adapter.api_level == 23
        await asyncio.wait_for(adapter.stop(), timeout=10)

    async def test_no_adb_anywhere_is_an_error_that_says_where_it_looked(
        self, monkeypatch, tmp_path,
    ):
        self._no_adb_on_path(monkeypatch, tmp_path, sdk_has_adb=False)
        calls: list = []
        self._fake_exec(monkeypatch, calls)

        adapter = LogcatAdapter(serial="08649a85")
        await asyncio.wait_for(adapter.start(), timeout=STARTUP_GRACE_S + 5)

        assert calls == [], "nothing is run without an adb"
        assert not adapter.is_running
        assert "ANDROID_HOME" in adapter._error and "not on PATH" in adapter._error
