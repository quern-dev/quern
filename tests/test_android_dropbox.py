"""Android crash reports from DropBox, and pulls that say what happened (#316).

The fixtures are real `dumpsys dropbox --print` output from an API 32 emulator
set to America/Los_Angeles: a Java crash (`am crash`), a native crash
(SIGSEGV) and an ANR (SIGSTOP plus input). The ANR fixture keeps part of
system_server's stack dump on purpose -- see the frames test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from server.sources.android_dropbox import (
    CRASH_TAGS,
    DropboxPullError,
    device_zone,
    parse_dropbox,
    pull_dropbox,
)

FIXTURES = Path(__file__).parent / "fixtures" / "android_dropbox"
PACIFIC = device_zone("America/Los_Angeles", "")
HEADERS = {"Authorization": "Bearer test-key-12345"}


def _parse(name, serial="emulator-5554", zone=PACIFIC):
    return parse_dropbox((FIXTURES / f"{name}.dropbox").read_text(), serial=serial, zone=zone)


class TestParsing:
    def test_a_java_crash(self):
        first = _parse("system_app_crash")[0]
        assert first.kind == "crash"
        assert first.process == "com.android.settings"
        assert first.device_id == "emulator-5554"
        assert first.exception_type == (
            "android.app.RemoteServiceException$CrashedByAdbException"
        )
        assert first.exception_codes == "shell-induced crash"
        assert first.top_frames[0].startswith(
            "android.app.ActivityThread.throwRemoteServiceException",
        )

    def test_the_header_time_is_converted_from_device_local_to_utc(self):
        """`2026-09-26 11:05:07` on a Pacific device is 18:05:07 UTC -- the
        crash induced during #255's live test, whose logcat line read 18:05:07Z."""
        first = _parse("system_app_crash")[0]
        assert first.timestamp == datetime(2026, 9, 26, 18, 5, 7, tzinfo=UTC)

    def test_a_native_crash_uses_the_tombstones_own_zone_stamped_time(self):
        [native] = _parse("system_app_native_crash")
        assert native.kind == "native_crash"
        assert native.signal == "SIGSEGV"
        assert native.exception_type == "signal 11 (SIGSEGV)"
        assert native.top_frames[0].startswith("#00 pc")
        # 13:54:24.057854241-0700 in the tombstone; the header has no fraction.
        assert native.timestamp == datetime(2026, 9, 27, 20, 54, 24, 57854, tzinfo=UTC)

    def test_a_native_crash_is_not_filed_as_a_java_crash(self):
        """`system_app_native_crash` also ends in `_crash`; checking that suffix
        first filed every native crash as a Java one with no signal."""
        assert {r.kind for r in _parse("system_app_native_crash")} == {"native_crash"}

    def test_an_anr(self):
        [anr] = _parse("system_app_anr")
        assert anr.kind == "anr"
        assert anr.exception_type == "ANR"
        assert anr.exception_codes.startswith("Input dispatching timed out")

    def test_an_anr_never_borrows_another_processs_stack(self):
        """The app was too wedged to dump its own threads, so the record's only
        `"main"` thread is system_server's. Returning that as the app's stack
        points the reader at the wrong process entirely."""
        text = (FIXTURES / "system_app_anr.dropbox").read_text()
        assert '"main"' in text                     # the trap is in the fixture
        [anr] = _parse("system_app_anr")
        assert anr.top_frames == []

    def test_the_same_record_gets_the_same_id_every_pull(self):
        """DropBox prints every stored record each time; a crash pulled twice
        must be one report."""
        assert [r.crash_id for r in _parse("system_app_crash")] == [
            r.crash_id for r in _parse("system_app_crash")
        ]
        other = _parse("system_app_crash", serial="other")[0]
        assert other.crash_id != _parse("system_app_crash")[0].crash_id

    def test_non_crash_tags_are_ignored(self):
        text = (
            "========================================\n"
            "2026-09-27 10:00:00 data_app_wtf (text, 10 bytes)\nProcess: x\n\nboom\n"
        )
        assert parse_dropbox(text, serial="s", zone=PACIFIC) == []

    def test_both_app_families_are_read(self):
        """Settings is a system app: its crashes are `system_app_*`, and asking
        for `data_app_*` alone found nothing on either test device."""
        assert "system_app_crash" in CRASH_TAGS and "data_app_crash" in CRASH_TAGS


class TestTimezones:
    def test_the_offset_is_the_one_in_force_on_that_date(self):
        winter = parse_dropbox(
            "========\n2026-01-15 10:00:00 data_app_crash (text, 1 bytes)\nProcess: a\nPID: 1\n\n",
            serial="s", zone=PACIFIC,
        )[0]
        summer = parse_dropbox(
            "========\n2026-07-15 10:00:00 data_app_crash (text, 1 bytes)\nProcess: a\nPID: 1\n\n",
            serial="s", zone=PACIFIC,
        )[0]
        assert winter.timestamp.hour == 18 and summer.timestamp.hour == 17

    def test_an_unusable_zone_name_falls_back_to_the_current_offset(self):
        zone = device_zone("Not/AZone", "-0700")
        assert zone is not None
        assert zone.utcoffset(None).total_seconds() == -7 * 3600

    def test_with_no_zone_at_all_a_record_is_dropped_rather_than_guessed(self):
        """A Java crash has no zone-stamped time of its own; stamping it as
        UTC would be the seven-hour error #255 removed."""
        assert _parse("system_app_crash", zone=None) == []
        # A native crash still has its tombstone's time.
        assert len(_parse("system_app_native_crash", zone=None)) == 1


class _FakeProc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self._out, self._err, self.returncode, self._hang = out, err, code, hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._out, self._err

    def kill(self):
        self.killed = True


class TestPull:
    async def _pull(self, monkeypatch, proc=None, exc=None, adb="/usr/bin/adb"):
        async def fake_exec(*args, **kwargs):
            if exc:
                raise exc
            return proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await pull_dropbox(adb, "emulator-5554")

    async def test_a_pull_reads_the_zone_and_every_tag(self, monkeypatch):
        out = (
            b"America/Los_Angeles\n-0700\n__QUERN_DROPBOX__\n"
            + (FIXTURES / "system_app_crash.dropbox").read_bytes()
        )
        reports = await self._pull(monkeypatch, _FakeProc(out=out))
        assert len(reports) == 3
        assert reports[0].timestamp == datetime(2026, 9, 26, 18, 5, 7, tzinfo=UTC)

    async def test_no_adb_is_a_failed_pull_not_an_empty_one(self, monkeypatch):
        with pytest.raises(DropboxPullError, match="adb not found"):
            await self._pull(monkeypatch, adb=None)

    async def test_a_non_zero_exit_says_why(self, monkeypatch):
        with pytest.raises(DropboxPullError, match="device offline"):
            await self._pull(monkeypatch, _FakeProc(err=b"error: device offline", code=1))

    async def test_a_run_that_failed_partway_is_not_read_as_complete(self, monkeypatch):
        """The zone lines and the marker printed, then dumpsys failed: the
        output looks well-formed and holds no records, which read as "no
        crashes". Only the exit code says otherwise."""
        proc = _FakeProc(
            out=b"America/Los_Angeles\n-0700\n__QUERN_DROPBOX__\n",
            err=b"Can't find service: dropbox", code=1,
        )
        with pytest.raises(DropboxPullError, match="Can't find service"):
            await self._pull(monkeypatch, proc)

    async def test_output_without_the_marker_is_not_mistaken_for_no_crashes(self, monkeypatch):
        """An unauthorised device can answer with exit 0 and nothing useful."""
        with pytest.raises(DropboxPullError):
            await self._pull(monkeypatch, _FakeProc(out=b"unauthorized", code=0))

    async def test_a_hung_device_times_out_and_is_killed(self, monkeypatch):
        from server.sources import android_dropbox

        monkeypatch.setattr(android_dropbox, "PULL_TIMEOUT_S", 0.05)
        proc = _FakeProc(hang=True)
        proc.returncode = None
        with pytest.raises(DropboxPullError, match="timed out"):
            await self._pull(monkeypatch, proc)
        assert proc.killed

    async def test_a_spawn_failure_says_so(self, monkeypatch):
        with pytest.raises(DropboxPullError, match="could not run adb"):
            await self._pull(monkeypatch, exc=PermissionError("denied"))


# -- the endpoint ----------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    from server.config import ServerConfig
    from server.main import create_app
    from server.sources.crash import CrashAdapter

    app = create_app(
        config=ServerConfig(api_key="test-key-12345"),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    entries = []

    async def collect(entry):
        entries.append(entry)
        await app.state.crash_buffer.append(entry)

    adapter = CrashAdapter(watch_dir=tmp_path, on_entry=collect, poll_interval=3600)
    app.state.crash_adapter = adapter
    app.state.emitted = entries
    return app


def _controller(*, android, lib_udid=None):
    async def get_lib(udid):
        return lib_udid

    return SimpleNamespace(
        _is_android=lambda u: android,
        get_libimobiledevice_udid=get_lib,
        adb=SimpleNamespace(adb_path="/usr/bin/adb"),
    )


async def _latest(app, **params):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/crashes/latest", headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fake_dropbox(monkeypatch, *, reports=None, error=None):
    from server.api import crashes

    async def fake_pull(adb_path, serial):
        if error:
            raise DropboxPullError(error)
        return reports if reports is not None else _parse("system_app_crash", serial=serial)

    monkeypatch.setattr(crashes, "pull_dropbox", fake_pull)


class TestEndpoint:
    async def test_android_crashes_are_returned(self, app, monkeypatch):
        """The issue: after a crash, get_latest_crash returned total 0 on
        Android because there was no Android pull at all."""
        app.state.device_controller = _controller(android=True)
        _fake_dropbox(monkeypatch)

        data = await _latest(app, udid="emulator-5554")

        assert data["total"] == 3
        assert data["pull"] == {
            "udid": "emulator-5554", "platform": "android", "status": "pulled",
            "new_reports": 3, "reason": None,
        }
        assert data["crashes"][0]["kind"] == "crash"

    async def test_a_second_pull_adds_nothing_twice(self, app, monkeypatch):
        app.state.device_controller = _controller(android=True)
        _fake_dropbox(monkeypatch)
        await _latest(app, udid="emulator-5554")

        again = await _latest(app, udid="emulator-5554")

        assert again["total"] == 3 and again["pull"]["new_reports"] == 0

    async def test_a_failed_android_pull_says_so(self, app, monkeypatch):
        app.state.device_controller = _controller(android=True)
        _fake_dropbox(monkeypatch, error="adb shell failed (exit 1): device offline")

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["status"] == "failed"
        assert "device offline" in data["pull"]["reason"]

    async def test_a_crash_logcat_already_reported_is_not_logged_twice(self, app, monkeypatch):
        """With capture running, logcat recognised the crash as it happened
        (#255). The pulled report must not put a second entry on the timeline."""
        from server.models import LogEntry, LogLevel, LogSource

        [first, *_] = _parse("system_app_crash")
        await app.state.crash_buffer.append(LogEntry(
            id="android-crash-abc", timestamp=first.timestamp, device_id="emulator-5554",
            process="com.android.settings", level=LogLevel.FAULT,
            message="com.android.settings crashed", source=LogSource.CRASH,
        ))
        app.state.device_controller = _controller(android=True)
        _fake_dropbox(monkeypatch)

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["new_reports"] == 3             # all three are reports
        assert len(app.state.emitted) == 2                  # but only two new log entries

    async def test_an_iphone_not_on_usb_is_skipped_with_the_reason(self, app):
        """This was a debug log line while the response read as "no new crashes"."""
        app.state.device_controller = _controller(android=False, lib_udid=None)

        data = await _latest(app, udid="00008101-PHONE")

        assert data["pull"]["status"] == "skipped"
        assert "USB" in data["pull"]["reason"]

    async def test_a_failed_iphone_pull_says_so(self, app, monkeypatch):
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(android=False, lib_udid="00008101-LIB")

        async def failing(lib_udid):
            return PullResult(error="idevicecrashreport timed out after 30s")

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", failing)

        data = await _latest(app, udid="00008101-PHONE")

        assert data["pull"] == {
            "udid": "00008101-PHONE", "platform": "ios", "status": "failed",
            "new_reports": 0, "reason": "idevicecrashreport timed out after 30s",
        }

    async def test_a_successful_iphone_pull_says_so(self, app, monkeypatch):
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(android=False, lib_udid="00008101-LIB")

        async def ok(lib_udid):
            return PullResult()

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", ok)

        data = await _latest(app, udid="00008101-PHONE")

        assert data["pull"]["status"] == "pulled" and data["pull"]["platform"] == "ios"

    async def test_disabled_crash_capture_is_a_skip_not_an_empty_answer(self, app):
        app.state.crash_adapter = None
        data = await _latest(app, udid="emulator-5554")
        assert data["pull"]["status"] == "skipped"
        assert "disabled" in data["pull"]["reason"]

    async def test_no_udid_means_no_pull_field(self, app):
        data = await _latest(app)
        assert data["pull"] is None


class TestOneDevicesCrashes:
    """With a udid, the answer was every device's crashes -- the emulator's
    crash at the top of a Pixel's list, found in the live test."""

    async def test_another_devices_crashes_are_not_returned(self, app, monkeypatch):
        app.state.device_controller = _controller(android=True)
        _fake_dropbox(monkeypatch)
        await _latest(app, udid="emulator-5554")                    # 3 emulator crashes

        pixel = await _latest(app, udid="PIXEL-SERIAL")

        assert all(c["device_id"] == "PIXEL-SERIAL" for c in pixel["crashes"])
        assert pixel["total"] == 3                                   # its own, pulled now

    async def test_reports_that_name_no_device_still_appear(self, app, monkeypatch):
        """A simulator crash file does not say which simulator; filtering it
        out would hide what used to show."""
        from server.models import CrashReport

        app.state.crash_adapter.crash_reports.append(CrashReport(
            crash_id="sim1", timestamp=datetime(2026, 9, 27, tzinfo=UTC), process="MyApp",
        ))
        app.state.device_controller = _controller(android=True)
        _fake_dropbox(monkeypatch, reports=[])

        data = await _latest(app, udid="emulator-5554")

        assert [c["crash_id"] for c in data["crashes"]] == ["sim1"]

    async def test_an_iphone_pull_tags_its_reports_with_the_phone(self, app, monkeypatch):
        from server.models import CrashReport
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(android=False, lib_udid="LIB")
        pulled = CrashReport(crash_id="ios1", timestamp=datetime(2026, 9, 27, tzinfo=UTC))

        async def pull(lib_udid):
            app.state.crash_adapter.crash_reports.append(pulled)
            return PullResult(new=[pulled])

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", pull)

        data = await _latest(app, udid="00008101-PHONE")

        assert data["crashes"][0]["device_id"] == "00008101-PHONE"
