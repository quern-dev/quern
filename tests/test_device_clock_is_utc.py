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
        adb = FakeAdb(monkeypatch, sdk=b"34\n")
        await LogcatAdapter(serial="emulator-5554").start()

        assert adb.logcat_args()[:8] == (
            "logcat", "-v", "threadtime", "-v", "UTC", "-v", "year", "-T",
        )

    async def test_starting_capture_does_not_clear_the_devices_logs(self, monkeypatch):
        """`logcat -c` empties the device's own buffers -- history that
        belongs to whoever else reads the device, and to the user."""
        adb = FakeAdb(monkeypatch)
        await LogcatAdapter(serial="emulator-5554").start()

        assert not any("-c" in call for call in adb.calls), adb.calls
        assert adb.logcat_args()[-2:] == ("-T", "1")


class TestOldAndroid:
    """Before Android 7, logcat's `-v` accepts format names only and exits on
    `UTC` or `year`. Asking for them regardless made capture on those devices
    a process that died at once, while the start call reported success."""

    async def test_an_old_device_is_not_asked_for_formats_it_rejects(self, monkeypatch):
        adb = FakeAdb(monkeypatch, sdk=b"23\n")
        adapter = LogcatAdapter(serial="old")
        await adapter.start()

        assert "UTC" not in adb.logcat_args() and "year" not in adb.logcat_args()
        assert adapter.api_level == 23
        assert adapter._error is None

    async def test_an_unanswered_level_is_not_mistaken_for_an_answer(self, monkeypatch):
        """Could not ask is not "old": the modern format is used, and the
        early-exit check below is what catches a wrong guess."""
        adb = FakeAdb(monkeypatch, sdk=b"")
        adapter = LogcatAdapter(serial="x")
        await adapter.start()

        assert adapter.api_level is None
        assert "UTC" in adb.logcat_args()

    async def test_a_getprop_that_hangs_is_killed_and_reads_as_unknown(self, monkeypatch):
        """An unauthorised or wedged device can leave getprop hanging. The
        call is abandoned, the process killed, and the level is unknown --
        not a guess. Survivor P25: dropping the kill failed nothing."""
        import asyncio
        import shutil

        from server.sources import logcat

        killed = []

        class _Hung:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            def kill(self):
                killed.append(True)

        async def fake_exec(*args, **kwargs):
            return _Hung()

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(logcat, "GETPROP_TIMEOUT_S", 0.05)

        level = await LogcatAdapter(serial="x")._api_level()

        assert level is None
        assert killed == [True], "a hung getprop was left running"

    async def test_a_logcat_that_exits_at_once_is_an_error_with_its_reason(self, monkeypatch):
        FakeAdb(monkeypatch, sdk=b"", logcat_exit=255,
                stderr=b"Invalid parameter to -v: UTC\n")
        adapter = LogcatAdapter(serial="x")
        await adapter.start()

        assert adapter._error is not None
        assert "Invalid parameter to -v: UTC" in adapter._error
        assert "exit 255" in adapter._error
        assert not adapter.is_running

    async def test_an_old_adb_reason_on_stdout_is_not_lost(self, monkeypatch):
        """Before Android 7 the device's error comes back on stdout and the
        exit code as 0; reading stderr alone said "exit 0: no output"."""
        FakeAdb(monkeypatch, sdk=b"", logcat_exit=0, stderr=b"",
                stdout=b"Invalid parameter to -v: UTC\nUsage: logcat [options]\n")
        adapter = LogcatAdapter(serial="x")
        await adapter.start()

        assert "Invalid parameter to -v: UTC" in adapter._error

    async def test_a_stream_that_ends_by_itself_is_an_error_not_a_stop(self, monkeypatch):
        """The device unplugged, or logcat died: status must read "error"."""
        adapter = LogcatAdapter(serial="x")

        class _Stdout:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class _Stderr:
            async def read(self):
                return b"error: device 'x' not found\n"

        class _Proc:
            stdout, stderr, returncode = _Stdout(), _Stderr(), 1

        adapter._process = _Proc()
        adapter._running = True
        await adapter._read_loop()

        assert "device 'x' not found" in adapter._error
        assert adapter.status().status == "error"


async def test_a_normal_stop_is_not_an_error():
    """The end-of-stream error is for a stream nobody stopped. Survivor P4 of
    the second review: reporting it after `stop()` too failed nothing."""
    import asyncio

    ended = asyncio.Event()

    class _Stdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await ended.wait()
            raise StopAsyncIteration

    class _Stderr:
        async def read(self):
            return b""

    class _Proc:
        stdout, stderr, returncode = _Stdout(), _Stderr(), None

        def terminate(self):
            self.returncode = -15
            ended.set()

        async def wait(self):
            # A real wait suspends until the process has exited, and the read
            # loop sees EOF in that time. A wait that returned without
            # yielding let stop() cancel the read task before it reached the
            # check under test, so this passed with the check removed.
            await ended.wait()
            await asyncio.sleep(0.01)
            return self.returncode

    adapter = LogcatAdapter(serial="x")
    adapter._process = _Proc()
    adapter._running = True
    adapter._read_task = asyncio.create_task(adapter._read_loop())
    await asyncio.sleep(0)

    await adapter.stop()

    assert adapter._read_task is None
    assert adapter._error is None
    assert adapter.status().status == "stopped"


class FakeAdb:
    """`adb` for the adapter's two calls: getprop, then logcat."""

    def __init__(self, monkeypatch, *, sdk=b"34\n", logcat_exit=None, stderr=b"", stdout=None):
        import asyncio
        import shutil

        self.calls: list[tuple] = []
        fake = self

        class _Stream:
            def __init__(self, data):
                self._data = data

            async def read(self):
                return self._data

        class _Getprop:
            returncode = 0

            async def communicate(self):
                return sdk, b""

        class _Logcat:
            def __init__(self):
                self.stdout = _Stream(stdout) if stdout is not None else None
                self.stderr = _Stream(stderr)
                self.returncode = logcat_exit

            async def wait(self):
                if logcat_exit is None:
                    await asyncio.sleep(3600)   # a live logcat does not exit
                return logcat_exit

        async def fake_exec(*args, **kwargs):
            fake.calls.append(args)
            return _Getprop() if "getprop" in args else _Logcat()

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/adb")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(LogcatAdapter, "_read_loop", lambda self: asyncio.sleep(0))

    def logcat_args(self) -> tuple:
        [call] = [c for c in self.calls if "logcat" in c]
        return call[call.index("logcat"):]


class TestIdevicesyslog:
    def test_a_line_is_stamped_on_arrival_not_as_utc_in_the_current_year(self):
        """idevicesyslog prints the device's local time with no zone and no
        year. Read as UTC in the current year it was seven hours off in
        Pacific time and a year off across New Year."""
        from server.sources.syslog import SyslogAdapter

        before = datetime.now(UTC)
        entry = SyslogAdapter()._parse_line(
            "Dec 31 23:59:59 iPhone MyApp(CoreFoundation)[1234] <Notice>: hello",
        )

        assert before <= entry.timestamp <= datetime.now(UTC)
        assert entry.process == "MyApp"


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
