"""iPhone crash reports through pymobiledevice3: list, pick recent, pull, clear (#322)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import pytest

from server.sources import ios_crash
from server.sources.ios_crash import IosCrashError, report_time, select_recent

NOW = datetime(2026, 9, 27, 22, 0, 0)


class TestReportTime:
    def test_the_capture_time_in_the_name(self):
        assert report_time("Calculator-2026-09-27-190847.ips") == datetime(2026, 9, 27, 19, 8, 47)

    def test_names_with_other_hyphens_and_plus_signs(self):
        name = "stacks+com.apple.x-y-2026-09-01-010203.ips"
        assert report_time(name) == datetime(2026, 9, 1, 1, 2, 3)

    def test_a_double_extension(self):
        name = "JetsamEvent-2026-09-01-010203.ips.synced"
        assert report_time(name) == datetime(2026, 9, 1, 1, 2, 3)

    def test_no_date(self):
        assert report_time("Undated.ips") is None
        assert report_time("MyApp-2026-13-45-990000.ips") is None       # not a real date


class TestSelectRecent:
    def test_the_window_has_a_day_of_margin_for_zones(self):
        """Names are device-local and now is the Mac's; the two can differ."""
        names = [
            "A-2026-09-24-120000.ips",      # 3 days 10 hours: only the margin keeps it
            "B-2026-09-23-210000.ips",      # just past days + 1
        ]
        selection = select_recent(names, 3, NOW)
        assert selection.wanted == ["A-2026-09-24-120000.ips"]
        assert selection.older == 1

    def test_undated_reports_are_pulled(self):
        assert select_recent(["Undated.ips"], 3, NOW).wanted == ["Undated.ips"]

    def test_the_oldest_date_covers_pulled_and_left_alike(self):
        names = ["A-2026-09-27-010000.ips", "B-2026-03-02-010000.ips", "C-2026-05-01-010000.ips"]
        selection = select_recent(names, 3, NOW)
        assert str(selection.oldest) == "2026-03-02"
        assert (selection.wanted, selection.older) == (["A-2026-09-27-010000.ips"], 2)

    def test_nothing_listed(self):
        selection = select_recent([], 3, NOW)
        assert (selection.wanted, selection.older, selection.oldest) == ([], 0, None)


def _fake_run(monkeypatch, out="", error=None, sent=None):
    async def run(cmd, timeout):
        if sent is not None:
            sent.append(cmd)
        if error:
            raise error
        return out
    monkeypatch.setattr(ios_crash, "_run", run)


class TestCommands:
    async def test_the_listing_keeps_top_level_reports_only(self, monkeypatch):
        """Measured output: one path per line on stdout, directories included."""
        _fake_run(monkeypatch, out=(
            "/DiagnosticLogs\n/Calculator-2026-09-27-190847.ips\n/Old.crash\n"
            "/DiagnosticLogs/Search/spotlight_heartbeat_last.log\n"
            "/Retired/MyApp-2026-01-01-000000.ips\n/notes.log\nnoise\n"
        ))
        names = await ios_crash.list_reports("/bin/pmd3", "HW")
        assert names == ["Calculator-2026-09-27-190847.ips", "Old.crash"]

    async def test_the_listing_targets_the_device(self, monkeypatch):
        sent = []
        _fake_run(monkeypatch, sent=sent)
        await ios_crash.list_reports("/bin/pmd3", "00008101-HW")
        assert sent == [["/bin/pmd3", "crash", "ls", "--udid", "00008101-HW", "--depth", "1"]]

    async def test_a_pull_matches_exactly_the_names_and_never_erases(self, monkeypatch, tmp_path):
        sent = []
        _fake_run(monkeypatch, sent=sent)
        names = ["stacks+com.x-2026-09-27-010000.ips", "A.ips"]
        await ios_crash.pull_reports("/bin/pmd3", "HW", names, tmp_path)
        [cmd] = sent
        pattern = cmd[cmd.index("--match") + 1]
        # pymobiledevice3's pull applies it with re.search over each basename
        # (services/afc.py), which is unanchored -- so the anchors are ours.
        assert all(re.search(pattern, n) for n in names)
        assert not re.search(pattern, "stacksXcom.x-2026-09-27-010000.ips")    # "+" escaped
        assert not re.search(pattern, "A.ips.synced")                          # anchored at end
        assert not re.search(pattern, "OldA.ips")                              # and at start
        assert "--erase" not in cmd and cmd[-1] == str(tmp_path)

    async def test_nothing_to_pull_runs_nothing(self, monkeypatch, tmp_path):
        sent = []
        _fake_run(monkeypatch, sent=sent)
        await ios_crash.pull_reports("/bin/pmd3", "HW", [], tmp_path)
        assert sent == []

    async def test_clear_is_the_clear_command(self, monkeypatch):
        sent = []
        _fake_run(monkeypatch, sent=sent)
        await ios_crash.clear_reports("/bin/pmd3", "HW")
        assert sent == [["/bin/pmd3", "crash", "clear", "--udid", "HW"]]


class _Proc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self._out, self._err, self.returncode, self._hang = out, err, code, hang
        self.killed = self.waited = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._out, self._err

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


class TestRun:
    def _spawn(self, monkeypatch, proc=None, exc=None):
        async def fake_exec(*args, **kwargs):
            if exc:
                raise exc
            return proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def test_a_failure_says_why_without_the_log_prefix(self, monkeypatch):
        """The stderr line is measured, for a udid usbmux does not have."""
        err = (b"2026-09-27 22:09:54 jerimiah-mbp16 pymobiledevice3.__main__[8039] ERROR "
               b"Device not found: usbmux has no device matching udid 00008101-DEADBEEF0000000\n")
        self._spawn(monkeypatch, _Proc(err=err, code=1))
        with pytest.raises(IosCrashError) as e:
            await ios_crash._run(["/bin/pmd3", "crash", "ls"], 5)
        assert str(e.value) == (
            "pymobiledevice3 crash ls exited 1: Device not found: usbmux has no "
            "device matching udid 00008101-DEADBEEF0000000"
        )

    async def test_a_hung_device_times_out_and_is_killed_and_reaped(self, monkeypatch):
        proc = _Proc(hang=True)
        proc.returncode = None
        self._spawn(monkeypatch, proc)
        with pytest.raises(IosCrashError, match="timed out"):
            await ios_crash._run(["/bin/pmd3", "crash", "pull"], 0.05)
        assert proc.killed and proc.waited

    async def test_a_spawn_failure_says_so(self, monkeypatch):
        self._spawn(monkeypatch, exc=PermissionError("denied"))
        with pytest.raises(IosCrashError, match="could not run pymobiledevice3"):
            await ios_crash._run(["/bin/pmd3", "crash", "ls"], 5)

    async def test_a_cancelled_call_does_not_leave_it_running(self, monkeypatch):
        proc = _Proc(hang=True)
        proc.returncode = None
        self._spawn(monkeypatch, proc)
        task = asyncio.create_task(ios_crash._run(["/bin/pmd3", "crash", "pull"], 60))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed

    async def test_success_returns_stdout(self, monkeypatch):
        self._spawn(monkeypatch, _Proc(out=b"/A.ips\n"))
        assert await ios_crash._run(["/bin/pmd3", "crash", "ls"], 5) == "/A.ips\n"
