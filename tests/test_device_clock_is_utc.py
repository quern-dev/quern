"""Device log lines are stamped in UTC, not in whatever zone printed them (#255).

Both device adapters read a timestamp with no zone and labelled it UTC. On a
device (Android) or host (iOS) in Pacific time that put every line seven
hours in the past -- measured on an API 32 emulator: a line logged at 18:05Z
stored as 11:05Z. Android lines then fell outside every windowed query, the
summary and the trace, and a windowed query reported itself complete after
capturing thousands of lines.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from server.sources.device_log import PhysicalDeviceLogAdapter, host_local_to_utc
from server.sources.logcat import LogcatAdapter


@pytest.fixture
def pacific(monkeypatch):
    """Run as if the host were in Pacific time, whatever the real machine is."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestLogcat:
    def test_a_utc_line_keeps_its_time(self):
        """The line as `-v threadtime -v UTC -v year` prints it -- copied from
        the emulator, crash and all."""
        entry = LogcatAdapter(serial="emulator-5554")._parse_line(
            "2026-09-26 18:05:07.696 +0000 25084 25084 E AndroidRuntime: FATAL EXCEPTION: main",
        )

        assert entry.timestamp == datetime(2026, 9, 26, 18, 5, 7, 696000, tzinfo=UTC)
        assert entry.process == "AndroidRuntime"
        assert entry.pid == 25084
        assert entry.message == "FATAL EXCEPTION: main"

    def test_a_zone_other_than_utc_is_converted(self):
        entry = LogcatAdapter(serial="x")._parse_line(
            "2026-09-26 11:05:07.696 -0700  1 2 I Tag: m",
        )
        assert entry.timestamp == datetime(2026, 9, 26, 18, 5, 7, 696000, tzinfo=UTC)

    def test_the_year_comes_from_the_line(self):
        """The old parse assumed the current year, so a line from 31 December
        read on 1 January was stamped almost a year in the future."""
        entry = LogcatAdapter(serial="x")._parse_line("2025-12-31 23:59:59.900 +0000  1 2 I T: m")
        assert entry.timestamp.year == 2025

    def test_a_legacy_line_is_stamped_on_arrival_not_as_utc(self):
        """A device too old for `-v UTC` prints local time with no zone. The
        zone is not in the line, so reading it as UTC is a guess that is
        wrong by the offset; arrival time is late by milliseconds instead."""
        before = datetime.now(UTC)
        entry = LogcatAdapter(serial="x")._parse_line("03-08 14:22:45.123  1234  5678 D MyTag  : m")

        assert before <= entry.timestamp <= datetime.now(UTC)
        assert entry.process == "MyTag"

    async def test_logcat_is_asked_for_utc_with_a_year(self, monkeypatch):
        import asyncio
        import shutil

        calls = []

        class _Proc:
            returncode = None
            stdout = None

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return _Proc()

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        adapter = LogcatAdapter(serial="emulator-5554")
        monkeypatch.setattr(adapter, "_read_loop", lambda: asyncio.sleep(0))

        await adapter.start()

        stream = [c for c in calls if "-c" not in c][0]
        assert stream[stream.index("logcat") + 1:][:6] == (
            "-v", "threadtime", "-v", "UTC", "-v", "year",
        )


class TestPhysicalIos:
    """`pymobiledevice3` prints host-local time with no zone."""

    def test_a_host_local_line_is_converted_to_utc(self, pacific):
        adapter = PhysicalDeviceLogAdapter(udid="TESTUDID0000", device_id="PHONE")
        parsed = adapter._parse_line("2026-09-26 11:05:07.207000 MyApp[4410] <NOTICE>: hello")

        assert parsed.timestamp == datetime(2026, 9, 26, 18, 5, 7, 207000, tzinfo=UTC)

    def test_the_offset_is_the_one_in_force_on_that_date(self, pacific):
        """January is PST (-8), September is PDT (-7). A fixed offset taken
        once would be an hour wrong for half the year."""
        winter = host_local_to_utc(datetime(2026, 1, 15, 10, 0, 0))
        summer = host_local_to_utc(datetime(2026, 7, 15, 10, 0, 0))

        assert winter == datetime(2026, 1, 15, 18, 0, 0, tzinfo=UTC)
        assert summer == datetime(2026, 7, 15, 17, 0, 0, tzinfo=UTC)
