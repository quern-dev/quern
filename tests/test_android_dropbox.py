"""Android crash reports from DropBox, and pulls that say what happened (#316).

The fixtures are real `dumpsys dropbox --print` output from an API 32 emulator
set to America/Los_Angeles: a Java crash (`am crash`), a native crash
(SIGSEGV) and an ANR (SIGSTOP plus input). The ANR fixture keeps part of
system_server's stack dump on purpose -- see the frames test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.sources.android_dropbox import (
    CRASH_TAGS,
    DropboxPull,
    DropboxPullError,
    device_zone,
    parse_dropbox,
    parse_open_dialogs,
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

    def test_an_anrs_own_main_thread_is_its_stack(self):
        """Where the app did dump its threads, its main thread is where it was
        stuck -- the answer an ANR report exists to give."""
        text = (
            "========\n2026-09-27 10:00:00 data_app_anr (text, 1 bytes)\n"
            "Process: com.example.app\nPID: 4242\nSubject: Input dispatching timed out\n\n"
            "----- pid 4242 at 2026-09-27 10:00:00 -----\n"
            '"RenderThread" prio=7 tid=9 Native\n'
            '  | group="main" sCount=1\n'
            "  at com.example.Render.draw(Render.java:1)\n\n"
            '"main" prio=5 tid=1 Sleeping\n'
            '  | group="main" sCount=1\n'
            "  at java.lang.Thread.sleep(Native method)\n"
            "  at com.example.Main.onClick(Main.java:42)\n\n"
            "----- end 4242 -----\n"
            "----- pid 1 at 2026-09-27 10:00:00 -----\n"
            '"main" prio=5 tid=1 Native\n'
            "  at com.android.server.SystemServer.run(SystemServer.java:1)\n"
        )
        [anr] = parse_dropbox(text, serial="s", zone=PACIFIC)
        # Not RenderThread's (whose `group="main"` line matched a bare
        # `"main"`), and not the next process's main thread either.
        assert anr.top_frames == [
            "java.lang.Thread.sleep(Native method)",
            "com.example.Main.onClick(Main.java:42)",
        ]
        assert anr.pid == 4242

    def test_an_anr_without_its_own_main_thread_borrows_nothing_after_it(self):
        """The app's section ends where the next process's begins, with or
        without an `----- end` line; its main thread is not the app's."""
        text = (
            "========\n2026-09-27 10:00:00 data_app_anr (text, 1 bytes)\n"
            "Process: com.example.app\nPID: 4242\nSubject: Input dispatching timed out\n\n"
            "----- pid 4242 at 2026-09-27 10:00:00 -----\n"
            '"Binder:4242_1" prio=5 tid=2 Native\n'
            "  at android.os.Binder.execTransact(Binder.java:1)\n\n"
            "----- pid 1 at 2026-09-27 10:00:00 -----\n"
            '"main" prio=5 tid=1 Native\n'
            "  at com.android.server.SystemServer.run(SystemServer.java:1)\n"
        )
        [anr] = parse_dropbox(text, serial="s", zone=PACIFIC)
        assert anr.top_frames == []

    def test_the_id_survives_the_device_changing_zone(self):
        """DropBox prints every header in the zone in force now, so the same
        crash reads 11:05 on a Pacific device and 14:05 once it is on Eastern
        time. The id must not change, or the crash is new all over again."""
        text = (FIXTURES / "system_app_crash.dropbox").read_text()
        pacific = parse_dropbox(text, serial="s", zone=PACIFIC)[0]
        eastern = parse_dropbox(
            text.replace("2026-09-26 11:05:07", "2026-09-26 14:05:07"),
            serial="s", zone=device_zone("America/New_York", ""),
        )[0]
        assert eastern.timestamp == pacific.timestamp
        assert eastern.crash_id == pacific.crash_id

    def test_non_crash_tags_are_ignored(self):
        text = (
            "========================================\n"
            "2026-09-27 10:00:00 data_app_wtf (text, 10 bytes)\nProcess: x\n\nboom\n"
        )
        assert parse_dropbox(text, serial="s", zone=PACIFIC) == []

    def test_both_app_families_are_read_for_every_kind(self):
        """Settings is a system app: its crashes are `system_app_*`, and asking
        for `data_app_*` alone found nothing on either test device."""
        assert set(CRASH_TAGS) == {
            f"{family}_app_{kind}"
            for family in ("data", "system") for kind in ("crash", "native_crash", "anr")
        }


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


def _device_output(*, zone="America/Los_Angeles", offset="-0700", procs=None, tags=None,
                   rc=None, omit=()):
    """What the pull's shell script prints, one section per tag.

    `tags` maps a tag to its dumpsys output (default: nothing stored);
    `rc` a tag to its exit status; `omit` drops a tag's section entirely.
    """
    tags, rc = tags or {}, rc or {}
    if procs is None:
        procs = (FIXTURES / "processes_crash_dialog.dumpsys").read_bytes()
    out = f"__QUERN_TZ__ {zone}\n__QUERN_OFFSET__ {offset}\n__QUERN_PROCS__\n".encode()
    out += procs + b"__QUERN_DROPBOX__\n"
    for tag in CRASH_TAGS:
        if tag in omit:
            continue
        out += f"__QUERN_TAG__ {tag}\n".encode() + tags.get(tag, b"")
        out += f"__QUERN_RC__ {rc.get(tag, 0)}\n".encode()
    return out


class TestPull:
    async def _pull(self, monkeypatch, proc=None, exc=None, adb="/usr/bin/adb", sent=None):
        async def fake_exec(*args, **kwargs):
            if sent is not None:
                sent.extend(args)
            if exc:
                raise exc
            return proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return await pull_dropbox(adb, "emulator-5554")

    async def test_a_pull_reads_the_zone_the_dialogs_and_every_tag(self, monkeypatch):
        out = _device_output(tags={
            "system_app_crash": (FIXTURES / "system_app_crash.dropbox").read_bytes(),
            "system_app_anr": (FIXTURES / "system_app_anr.dropbox").read_bytes(),
        })
        sent = []
        pulled = await self._pull(monkeypatch, _FakeProc(out=out), sent=sent)
        assert len(pulled.reports) == 4
        assert pulled.reports[0].timestamp == datetime(2026, 9, 26, 18, 5, 7, tzinfo=UTC)
        assert pulled.open_dialogs == {"com.google.android.deskclock": "crash"}
        assert pulled.errors == [] and pulled.undated == 0
        script = sent[-1]
        assert all(f"dumpsys dropbox --print {tag} 2>&1" in script for tag in CRASH_TAGS)

    async def test_an_empty_timezone_property_still_dates_the_records(self, monkeypatch):
        """Read by position, an empty property moved the offset into the
        zone-name slot, the zone came out unknown, and every Java crash and
        ANR was dropped."""
        out = _device_output(zone="", tags={
            "system_app_crash": (FIXTURES / "system_app_crash.dropbox").read_bytes(),
        })
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert len(pulled.reports) == 3 and pulled.undated == 0
        assert pulled.reports[0].timestamp == datetime(2026, 9, 26, 18, 5, 7, tzinfo=UTC)

    async def test_records_that_cannot_be_dated_are_counted(self, monkeypatch):
        out = _device_output(zone="", offset="", tags={
            "system_app_crash": (FIXTURES / "system_app_crash.dropbox").read_bytes(),
            "system_app_native_crash": (FIXTURES / "system_app_native_crash.dropbox").read_bytes(),
        })
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert pulled.undated == 3
        assert [r.kind for r in pulled.reports] == ["native_crash"]   # its own time

    async def test_one_tag_failing_is_reported_and_the_rest_are_kept(self, monkeypatch):
        """`;` passes on only the last command's status, so a failure in an
        earlier tag read as success. And dumpsys exits 0 after this line --
        the text below is a real emulator's, measured on API 32."""
        out = _device_output(tags={
            "data_app_crash": b"Can't find service: dropbox\n",
            "system_app_crash": (FIXTURES / "system_app_crash.dropbox").read_bytes(),
        })
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert pulled.errors == ["data_app_crash: Can't find service: dropbox"]
        assert len(pulled.reports) == 3

    async def test_a_tag_that_exited_non_zero_is_reported(self, monkeypatch):
        out = _device_output(rc={"system_app_anr": 1})
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert pulled.errors == ["system_app_anr: dumpsys exited 1"]

    async def test_a_dump_timeout_is_reported(self, monkeypatch):
        out = _device_output(tags={
            "data_app_anr": b"*** SERVICE 'dropbox' DUMP TIMEOUT (10000ms) EXPIRED ***\n",
        })
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert len(pulled.errors) == 1 and "DUMP TIMEOUT" in pulled.errors[0]

    async def test_a_tag_whose_status_line_never_printed_is_reported(self, monkeypatch):
        """Output cut off mid-tag: the section is there, its status is not."""
        out = _device_output().replace(b"__QUERN_RC__ 0\n", b"", 1)
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert pulled.errors == ["data_app_crash: dumpsys exited (unknown)"]

    async def test_a_crash_message_naming_dumpsys_failures_is_not_one(self, monkeypatch):
        """A record's own text may contain the phrases; only dumpsys's lines count."""
        record = (
            b"========================================\n"
            b"2026-09-27 10:00:00 data_app_crash (text, 1 bytes)\nProcess: com.example\nPID: 7\n\n"
            b"java.lang.IllegalStateException: Can't find service foo; DUMP TIMEOUT hit\n"
            b"\tat com.example.A.b(A.java:1)\n"
        )
        out = _device_output(tags={"data_app_crash": record})
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert pulled.errors == []
        assert len(pulled.reports) == 1

    async def test_a_tag_whose_section_never_printed_is_reported(self, monkeypatch):
        out = _device_output(omit=("system_app_anr",))
        pulled = await self._pull(monkeypatch, _FakeProc(out=out))
        assert pulled.errors == ["system_app_anr: not read"]

    async def test_an_empty_process_listing_is_unknown_not_clear(self, monkeypatch):
        """`dumpsys activity` failing prints nothing, and the script carries on
        to the DropBox part: the pull succeeds with no process lines."""
        pulled = await self._pull(monkeypatch, _FakeProc(out=_device_output(procs=b"")))
        assert pulled.open_dialogs is None

    async def test_no_adb_is_a_failed_pull_not_an_empty_one(self, monkeypatch):
        with pytest.raises(DropboxPullError, match="adb not found"):
            await self._pull(monkeypatch, adb=None)

    async def test_a_non_zero_exit_says_why(self, monkeypatch):
        with pytest.raises(DropboxPullError, match="device offline"):
            await self._pull(monkeypatch, _FakeProc(err=b"error: device offline", code=1))

    async def test_adb_itself_failing_after_output_began_is_a_failed_pull(self, monkeypatch):
        """adb's own exit status (a dropped connection, say) is still checked,
        even with the marker already printed."""
        proc = _FakeProc(out=_device_output(), err=b"error: closed", code=1)
        with pytest.raises(DropboxPullError, match="closed"):
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
        assert proc.killed and proc.waited      # killed, and reaped

    async def test_the_parse_runs_off_the_event_loop(self, monkeypatch):
        """Up to 1,000 records; parsing them must not hold the loop."""
        import threading

        from server.sources import android_dropbox

        seen = []
        real = android_dropbox._parse_pull

        def spy(*args):
            seen.append(threading.get_ident())
            return real(*args)

        monkeypatch.setattr(android_dropbox, "_parse_pull", spy)
        await self._pull(monkeypatch, _FakeProc(out=_device_output()))

        assert seen and seen[0] != threading.get_ident()

    async def test_a_cancelled_pull_does_not_leave_adb_running(self, monkeypatch):
        proc = _FakeProc(hang=True)
        proc.returncode = None

        async def fake_exec(*args, **kwargs):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        task = asyncio.create_task(pull_dropbox("/usr/bin/adb", "emulator-5554"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed

    async def test_a_spawn_failure_says_so(self, monkeypatch):
        with pytest.raises(DropboxPullError, match="could not run adb"):
            await self._pull(monkeypatch, exc=PermissionError("denied"))


class TestOpenDialogs:
    """Android holds a process that crashed twice behind a "keeps stopping"
    dialog and drops its further crashes silently. Found live, when `am crash`
    exited 0 and produced neither a DropBox record nor a logcat line."""

    def test_the_held_process_is_found_in_a_real_listing(self):
        text = (FIXTURES / "processes_crash_dialog.dumpsys").read_text()
        # 90 lines, ~60 naming processes; the flag belongs to the record above
        # it, which is in the middle -- not the first or the last one seen.
        assert parse_open_dialogs(text) == {"com.google.android.deskclock": "crash"}

    def test_an_anr_dialog(self):
        text = (
            "  *APP* UID 10110 ProcessRecord{8f70e56 15082:com.example.app/u0a110}\n"
            "     mCrashing=false null mNotResponding=true [AppNotRespondingDialog@1] bad=false\n"
        )
        assert parse_open_dialogs(text) == {"com.example.app": "anr"}

    def test_the_pre_android_12_spelling(self):
        text = (
            "  *APP* UID 10110 ProcessRecord{8f70e56 15082:com.example.app/u0a110}\n"
            "    crashing=true com.android.server.am.AppErrorDialog@1 notResponding=false\n"
        )
        assert parse_open_dialogs(text) == {"com.example.app": "crash"}

    def test_false_flags_are_not_dialogs(self):
        text = (
            "  *APP* UID 10110 ProcessRecord{8f70e56 15082:com.example.app/u0a110}\n"
            "     mCrashing=false null mNotResponding=false null bad=true\n"
        )
        assert parse_open_dialogs(text) == {}

    def test_a_listing_with_no_processes_is_unknown(self):
        assert parse_open_dialogs("") is None
        assert parse_open_dialogs("Permission Denial: can't dump\n") is None


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


#: What the fake refresh reports. The type cache starts cold, as on a fresh
#: server: a route that asks the type without warming it first gets None.
DEVICES = {
    "emulator-5554": "android_emulator",
    "PIXEL-SERIAL": "android_device",
    "00008101-PHONE": "device",
    "SIM-UDID": "simulator",
}


def _controller(*, lib_udid=None):
    """A real DeviceController with only the enumeration faked.

    The first version faked `_is_android` outright, which skipped the type
    cache entirely -- so a route asking a cold cache, and sending an
    emulator's serial down the iPhone path, could not fail a test.
    """
    from server.device.controller import DeviceController
    from server.models import DeviceType

    ctrl = DeviceController()
    ctrl.refreshes = 0

    async def list_devices():
        ctrl.refreshes += 1
        for udid, kind in DEVICES.items():
            ctrl._device_type_cache[udid] = DeviceType(kind)
        return []

    async def get_lib(udid):
        return lib_udid

    ctrl.list_devices = list_devices
    ctrl.get_libimobiledevice_udid = get_lib
    return ctrl


async def _latest(app, **params):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/crashes/latest", headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fake_dropbox(monkeypatch, *, reports=None, error=None, open_dialogs=None):
    from server.api import crashes

    async def fake_pull(adb_path, serial):
        if error:
            raise DropboxPullError(error)
        return DropboxPull(
            reports=reports if reports is not None else _parse("system_app_crash", serial=serial),
            open_dialogs={} if open_dialogs is None else open_dialogs,
        )

    monkeypatch.setattr(crashes, "pull_dropbox", fake_pull)


class TestEndpoint:
    async def test_android_crashes_are_returned(self, app, monkeypatch):
        """The issue: after a crash, get_latest_crash returned total 0 on
        Android because there was no Android pull at all."""
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch)

        data = await _latest(app, udid="emulator-5554")

        assert data["total"] == 3
        assert data["pull"] == {
            "udid": "emulator-5554", "platform": "android", "status": "pulled",
            "new_reports": 3, "reason": None, "open_dialogs": [],
            "window_days": None, "older_on_device": None, "oldest_on_device": None,
            "note": None,
        }
        assert data["crashes"][0]["kind"] == "crash"

    async def test_an_open_crash_dialog_is_reported(self, app, monkeypatch):
        """No new reports reads as "it stopped crashing" -- untrue while
        Android is dropping that process's crashes behind a dialog."""
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, open_dialogs={
            "com.google.android.deskclock": "crash", "com.example.hung": "anr",
        })

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["open_dialogs"] == [
            {"process": "com.example.hung", "kind": "anr"},
            {"process": "com.google.android.deskclock", "kind": "crash"},
        ]

    async def test_an_unreadable_process_listing_is_null_not_empty(self, app, monkeypatch):
        from server.api import crashes

        async def fake_pull(adb_path, serial):
            return DropboxPull(reports=[], open_dialogs=None)

        monkeypatch.setattr(crashes, "pull_dropbox", fake_pull)
        app.state.device_controller = _controller()

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["status"] == "pulled"
        assert data["pull"]["open_dialogs"] is None

    async def test_a_second_pull_adds_nothing_twice(self, app, monkeypatch):
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch)
        await _latest(app, udid="emulator-5554")

        again = await _latest(app, udid="emulator-5554")

        assert again["total"] == 3 and again["pull"]["new_reports"] == 0

    async def test_a_failed_android_pull_says_so(self, app, monkeypatch):
        app.state.device_controller = _controller()
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
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch)

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["new_reports"] == 3             # all three are reports
        assert len(app.state.emitted) == 2                  # but only two new log entries

    async def _logcat_saw(self, app, **fields):
        from server.models import LogEntry, LogLevel, LogSource

        await app.state.crash_buffer.append(LogEntry(**{
            "id": "android-crash-n1", "level": LogLevel.FAULT, "message": "crashed",
            "source": LogSource.CRASH, "device_id": "emulator-5554", **fields,
        }))

    async def test_a_native_crash_logcat_named_by_its_short_name_is_not_logged_twice(
        self, app, monkeypatch,
    ):
        """libc names the process by the kernel's 15-character name, so logcat
        said `ndroid.settings` where DropBox says `com.android.settings`, and
        matching names alone logged every native crash twice. The pid is on
        both."""
        [native] = _parse("system_app_native_crash")
        await self._logcat_saw(app, timestamp=native.timestamp, process="ndroid.settings",
                               pid=native.pid)
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[native])

        await _latest(app, udid="emulator-5554")

        assert app.state.emitted == []

    async def test_without_a_pid_the_short_name_still_matches(self, app, monkeypatch):
        [native] = _parse("system_app_native_crash")
        await self._logcat_saw(app, timestamp=native.timestamp, process="ndroid.settings")
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[native])

        await _latest(app, udid="emulator-5554")

        assert app.state.emitted == []

    async def test_a_name_that_is_only_a_suffix_is_not_the_kernel_name(self, app, monkeypatch):
        """The kernel name is exactly 15 characters; a shorter tail is some
        other process that happens to end the same way."""
        [first] = _parse("system_app_crash")[:1]                 # com.android.settings
        await self._logcat_saw(app, timestamp=first.timestamp, process="settings")
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[first])

        await _latest(app, udid="emulator-5554")

        assert len(app.state.emitted) == 1

    async def test_only_logcats_entries_count_as_logcat_having_seen_it(self, app, monkeypatch):
        """An entry this pull path emitted for an earlier crash of the same app
        a few seconds before is not logcat's record of this one."""
        [first] = _parse("system_app_crash")[:1]
        await self._logcat_saw(app, id="android-0123456789ab", timestamp=first.timestamp,
                               process=first.process, pid=first.pid)
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[first])

        await _latest(app, udid="emulator-5554")

        assert len(app.state.emitted) == 1

    async def test_another_processs_crash_at_the_same_moment_is_not_this_one(
        self, app, monkeypatch,
    ):
        [first] = _parse("system_app_crash")[:1]
        await self._logcat_saw(app, timestamp=first.timestamp, process="com.example.other",
                               pid=(first.pid or 0) + 1)
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[first])

        await _latest(app, udid="emulator-5554")

        assert [e.process for e in app.state.emitted] == ["com.android.settings"]

    async def test_the_same_app_crashing_again_under_a_new_pid_is_a_new_crash(
        self, app, monkeypatch,
    ):
        """An app restarted and crashed again within the window: same name,
        another process. The pid says which one logcat saw."""
        [first] = _parse("system_app_crash")[:1]
        await self._logcat_saw(app, timestamp=first.timestamp, process=first.process,
                               pid=first.pid + 1)
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[first])

        await _latest(app, udid="emulator-5554")

        assert len(app.state.emitted) == 1

    async def test_the_same_crash_on_another_device_is_not_this_one(self, app, monkeypatch):
        [first] = _parse("system_app_crash")[:1]
        await self._logcat_saw(app, timestamp=first.timestamp, process=first.process,
                               pid=first.pid, device_id="PIXEL-SERIAL")
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[first])

        await _latest(app, udid="emulator-5554")

        assert len(app.state.emitted) == 1

    async def test_a_cold_type_cache_is_warmed_before_routing(self, app, monkeypatch):
        """On a fresh server nothing has listed devices yet. Asking the type
        cold sent an emulator's serial down the iPhone path, to be told it
        was "not connected over USB"."""
        ctrl = _controller()
        app.state.device_controller = ctrl
        _fake_dropbox(monkeypatch)
        assert ctrl._device_type("emulator-5554") is None     # cold

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["platform"] == "android" and data["pull"]["status"] == "pulled"
        assert ctrl.refreshes == 1

    async def test_a_partial_android_pull_is_failed_and_keeps_what_it_read(
        self, app, monkeypatch,
    ):
        """"pulled" promises the list reflects the device; with a tag unread
        it does not. The records that were read are still returned."""
        from server.api import crashes

        async def fake_pull(adb_path, serial):
            return DropboxPull(reports=_parse("system_app_crash", serial=serial),
                               errors=["data_app_anr: dumpsys exited 1"], undated=2)

        monkeypatch.setattr(crashes, "pull_dropbox", fake_pull)
        app.state.device_controller = _controller()

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["status"] == "failed"
        assert "data_app_anr: dumpsys exited 1" in data["pull"]["reason"]
        assert "2 record(s) skipped" in data["pull"]["reason"]
        assert data["pull"]["new_reports"] == 3 and data["total"] == 3

    async def test_an_unread_tag_alone_makes_the_pull_failed(self, app, monkeypatch):
        from server.api import crashes

        async def fake_pull(adb_path, serial):
            return DropboxPull(reports=[], errors=["data_app_anr: dumpsys exited 1"])

        monkeypatch.setattr(crashes, "pull_dropbox", fake_pull)
        app.state.device_controller = _controller()

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["status"] == "failed"
        assert data["pull"]["reason"] == "data_app_anr: dumpsys exited 1"

    async def test_an_iphone_asked_for_by_its_hardware_udid(self, app, monkeypatch):
        """The id Xcode, Finder and idevice_id show. On a cold server the alias
        is learned by the refresh, so the id has to be canonicalised after it:
        before, the phone is unknown and is skipped as such."""
        from server.device.ios import devicectl
        from server.models import CrashReport, DeviceType
        from server.sources.crash import PullResult

        ctrl = _controller(lib_udid="LIB")
        refresh = ctrl.list_devices

        async def list_devices():
            devicectl._remember_identity("CORE-UUID", "00008101-HWUDID")
            ctrl._device_type_cache["CORE-UUID"] = DeviceType.DEVICE
            return await refresh()

        ctrl.list_devices = list_devices
        app.state.device_controller = ctrl
        app.state.crash_adapter.crash_reports.append(CrashReport(
            crash_id="ios1", timestamp=datetime(2026, 9, 27, tzinfo=UTC), device_id="CORE-UUID",
        ))
        asked = {}

        async def pull(lib_udid, *, device_id="", days=3):
            asked["device_id"] = device_id
            return PullResult()

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", pull)

        data = await _latest(app, udid="00008101-HWUDID")

        assert data["pull"]["status"] == "pulled"
        assert asked == {"device_id": "CORE-UUID"}
        assert [c["crash_id"] for c in data["crashes"]] == ["ios1"]

    async def test_undated_records_alone_make_the_pull_failed(self, app, monkeypatch):
        from server.api import crashes

        async def fake_pull(adb_path, serial):
            return DropboxPull(reports=[], undated=4)

        monkeypatch.setattr(crashes, "pull_dropbox", fake_pull)
        app.state.device_controller = _controller()

        data = await _latest(app, udid="emulator-5554")

        assert data["pull"]["status"] == "failed"
        assert data["pull"]["reason"].startswith("4 record(s) skipped")

    async def test_a_simulator_is_skipped_with_a_true_reason(self, app, monkeypatch):
        """Every simulator used to be told it was "not connected over USB"."""
        from server.sources.crash import DIAGNOSTIC_REPORTS_DIR

        app.state.device_controller = _controller()
        app.state.crash_adapter.extra_watch_dirs = [DIAGNOSTIC_REPORTS_DIR]

        data = await _latest(app, udid="SIM-UDID")

        assert data["pull"]["status"] == "skipped"
        assert "read continuously" in data["pull"]["reason"]
        assert "USB" not in data["pull"]["reason"]

    async def test_a_simulator_with_its_crash_watching_off_says_so(self, app):
        app.state.device_controller = _controller()
        app.state.crash_adapter.extra_watch_dirs = []

        data = await _latest(app, udid="SIM-UDID")

        assert "--no-simulator-crashes" in data["pull"]["reason"]

    async def test_an_unknown_device_is_not_called_an_iphone(self, app):
        app.state.device_controller = _controller()

        data = await _latest(app, udid="NOT-A-DEVICE")

        assert data["pull"]["status"] == "skipped" and data["pull"]["platform"] is None
        assert "does not know" in data["pull"]["reason"]

    async def test_an_iphone_not_on_usb_is_skipped_with_the_reason(self, app):
        """This was a debug log line while the response read as "no new crashes"."""
        app.state.device_controller = _controller()

        data = await _latest(app, udid="00008101-PHONE")

        assert data["pull"]["status"] == "skipped"
        assert "USB" in data["pull"]["reason"]

    async def test_a_failed_iphone_pull_says_so(self, app, monkeypatch):
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(lib_udid="00008101-LIB")

        async def failing(lib_udid, *, device_id="", days=3):
            return PullResult(error="pymobiledevice3 crash pull timed out after 30s")

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", failing)

        data = await _latest(app, udid="00008101-PHONE")

        assert data["pull"] == {
            "udid": "00008101-PHONE", "platform": "ios", "status": "failed",
            "new_reports": 0, "reason": "pymobiledevice3 crash pull timed out after 30s",
            "open_dialogs": None,
            "window_days": None, "older_on_device": None, "oldest_on_device": None,
            "note": None,
        }

    async def test_a_successful_iphone_pull_says_so(self, app, monkeypatch):
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(lib_udid="00008101-LIB")

        async def ok(lib_udid, *, device_id="", days=3):
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
        app.state.device_controller = _controller()
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
        app.state.device_controller = _controller()
        _fake_dropbox(monkeypatch, reports=[])

        data = await _latest(app, udid="emulator-5554")

        assert [c["crash_id"] for c in data["crashes"]] == ["sim1"]

    async def test_an_iphone_pull_is_told_which_phone_it_is(self, app, monkeypatch):
        """The files do not say which phone they came from; the pull tags them
        (tested in test_crash_adapter), given the device by the route."""
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(lib_udid="LIB")
        asked = {}

        async def pull(lib_udid, *, device_id="", days=3):
            asked.update(lib_udid=lib_udid, device_id=device_id)
            return PullResult()

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", pull)

        await _latest(app, udid="00008101-PHONE")

        assert asked == {"lib_udid": "LIB", "device_id": "00008101-PHONE"}


# -- #322: the pull's window, clearing the Mac, clearing a phone ------------------


class TestWindowOnTheResponse:
    async def test_what_the_pull_left_on_the_phone_is_said(self, app, monkeypatch):
        """Skipping old reports must not read as a full sync."""
        from datetime import date

        from server.sources.crash import PullResult

        app.state.device_controller = _controller(lib_udid="LIB")
        asked = {}

        async def pull(lib_udid, *, device_id="", days=3):
            asked["days"] = days
            return PullResult(window_days=days, older_on_device=214,
                              oldest_on_device=date(2026, 3, 2))

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", pull)

        data = await _latest(app, udid="00008101-PHONE", days=5)

        assert asked == {"days": 5}
        p = data["pull"]
        assert (p["status"], p["window_days"], p["older_on_device"], p["oldest_on_device"]) == (
            "pulled", 5, 214, "2026-03-02")
        assert "214 report(s) older than 5 day(s)" in p["note"]
        assert "clear_device_crashes" in p["note"]

    async def test_nothing_left_behind_means_no_note(self, app, monkeypatch):
        from server.sources.crash import PullResult

        app.state.device_controller = _controller(lib_udid="LIB")

        async def pull(lib_udid, *, device_id="", days=3):
            return PullResult(window_days=3, older_on_device=0)

        monkeypatch.setattr(app.state.crash_adapter, "pull_from_device", pull)

        data = await _latest(app, udid="00008101-PHONE")

        assert data["pull"]["older_on_device"] == 0 and data["pull"]["note"] is None

    async def test_days_is_bounded(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/crashes/latest", headers=HEADERS,
                                    params={"udid": "00008101-PHONE", "days": 0})
        assert resp.status_code == 422


async def _delete(app, **params):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.delete("/api/v1/crashes", headers=HEADERS, params=params)


async def _clear_device(app, udid):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/crashes/device/clear", headers=HEADERS,
                                 json={"udid": udid})


class TestClearCrashes:
    async def test_one_devices_reports(self, app):
        from server.models import CrashReport

        app.state.device_controller = _controller()
        adapter = app.state.crash_adapter
        adapter.crash_reports += [
            CrashReport(crash_id="a", timestamp=datetime(2026, 9, 27, tzinfo=UTC),
                        device_id="emulator-5554"),
            CrashReport(crash_id="b", timestamp=datetime(2026, 9, 27, tzinfo=UTC),
                        device_id="PIXEL-SERIAL"),
        ]

        resp = await _delete(app, udid="emulator-5554")

        assert resp.status_code == 200, resp.text
        assert resp.json()["reports_removed"] == 1
        assert [r.crash_id for r in adapter.crash_reports] == ["b"]

    async def test_a_device_quern_never_saw_is_not_cleared(self, app):
        """#182: success for an id that never existed tells teardown it worked."""
        app.state.device_controller = _controller()

        resp = await _delete(app, udid="NOT-A-DEVICE")

        assert resp.status_code == 404

    async def test_a_known_device_with_nothing_stored_is_cleared(self, app):
        app.state.device_controller = _controller()

        resp = await _delete(app, udid="PIXEL-SERIAL")

        assert resp.status_code == 200 and resp.json()["reports_removed"] == 0

    async def test_everything(self, app):
        from server.models import CrashReport

        app.state.crash_adapter.crash_reports.append(
            CrashReport(crash_id="a", timestamp=datetime(2026, 9, 27, tzinfo=UTC)))

        resp = await _delete(app)

        assert resp.status_code == 200 and resp.json()["reports_removed"] == 1

    async def test_an_empty_udid_is_refused_not_read_as_all(self, app):
        resp = await _delete(app, udid="")
        assert resp.status_code == 400

    async def test_disabled_capture(self, app):
        app.state.crash_adapter = None
        assert (await _delete(app)).status_code == 409


class TestClearDeviceCrashes:
    def _pmd3(self, monkeypatch, listings, sent, refuse=(), exact=True):
        """listings: what successive `crash ls` calls answer. The delete
        removes the names it is given, except `refuse`. `exact` records the
        phone's hardware UDID ("LIB") as its alias, as devicectl does."""
        from server.device.ios import devicectl
        from server.sources import ios_crash

        if exact:
            devicectl._remember_identity("00008101-PHONE", "LIB")
        calls = iter(listings)

        async def run(argv, what, timeout):
            sent.append(what)
            if what == "crash ls":
                assert argv[argv.index("--udid") + 1] == "LIB"
                return "".join(f"/{n}\n" for n in next(calls)), ""
            if what == "crash delete":
                assert argv[3] == "LIB"
                names = argv[4:]
                removed = [n for n in names if n not in refuse]
                return json.dumps({"removed": removed, "failed": list(refuse)}), ""
            raise AssertionError(argv)

        monkeypatch.setattr(ios_crash, "command", lambda: ["/bin/pmd3"])
        monkeypatch.setattr(ios_crash, "_run", run)

    async def test_an_iphone_is_cleared_and_the_count_reported(self, app, monkeypatch):
        app.state.device_controller = _controller(lib_udid="LIB")
        sent = []
        self._pmd3(monkeypatch, [["A.ips", "B.ips"], []], sent)

        resp = await _clear_device(app, "00008101-PHONE")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"udid": "00008101-PHONE", "removed": 2, "remaining": 0,
                               "failed": []}
        # Each report by name -- never `crash clear`, which takes DiagnosticLogs too.
        assert sent == ["crash ls", "crash delete", "crash ls"]

    async def test_a_report_that_survives_the_clear_is_not_counted_as_removed(
        self, app, monkeypatch,
    ):
        """One written between the clear and the second listing, or one the
        phone would not delete: `removed` is what went, not what was there."""
        app.state.device_controller = _controller(lib_udid="LIB")
        sent = []
        self._pmd3(monkeypatch, [["A.ips", "B.ips", "C.ips"], ["C.ips"]], sent, refuse=("C.ips",))

        resp = await _clear_device(app, "00008101-PHONE")

        assert resp.json() == {"udid": "00008101-PHONE", "removed": 2, "remaining": 1,
                               "failed": ["C.ips"]}

    async def test_android_is_refused_with_the_reason(self, app, monkeypatch):
        app.state.device_controller = _controller()
        sent = []
        self._pmd3(monkeypatch, [], sent)

        resp = await _clear_device(app, "emulator-5554")

        assert resp.status_code == 400 and "only be read" in resp.json()["detail"]
        assert sent == []

    async def test_a_simulator_is_refused_with_the_reason(self, app):
        app.state.device_controller = _controller()
        resp = await _clear_device(app, "SIM-UDID")
        assert resp.status_code == 400 and "DiagnosticReports" in resp.json()["detail"]

    async def test_an_unknown_device_is_not_found(self, app):
        app.state.device_controller = _controller()
        assert (await _clear_device(app, "NOT-A-DEVICE")).status_code == 404

    async def test_an_iphone_not_on_usb(self, app):
        app.state.device_controller = _controller(lib_udid=None)
        resp = await _clear_device(app, "00008101-PHONE")
        assert resp.status_code == 409
        assert "not connected over USB" in resp.json()["detail"]

    async def test_a_listing_that_fails_deletes_nothing(self, app, monkeypatch):
        from server.device.ios import devicectl
        from server.sources import ios_crash

        devicectl._remember_identity("00008101-PHONE", "LIB")
        app.state.device_controller = _controller(lib_udid="LIB")
        sent = []

        async def run(argv, what, timeout):
            sent.append(what)
            raise ios_crash.IosCrashError("pymobiledevice3 crash ls exited 1: Device not found")

        monkeypatch.setattr(ios_crash, "command", lambda: ["/bin/pmd3"])
        monkeypatch.setattr(ios_crash, "_run", run)

        resp = await _clear_device(app, "00008101-PHONE")

        assert resp.status_code == 502 and "Device not found" in resp.json()["detail"]
        assert sent == ["crash ls"]                      # never reached the delete

    async def test_a_delete_that_fails_partway_says_what_is_gone(self, app, monkeypatch):
        """The delete is permanent and goes one report at a time: a 502 that
        says only why reads as "nothing was deleted"."""
        from server.device.ios import devicectl
        from server.sources import ios_crash

        devicectl._remember_identity("00008101-PHONE", "LIB")
        app.state.device_controller = _controller(lib_udid="LIB")
        listings = iter([["A.ips", "B.ips"], ["B.ips"]])

        async def run(argv, what, timeout):
            if what == "crash ls":
                return "".join(f"/{n}\n" for n in next(listings)), ""
            raise ios_crash.IosCrashError("pymobiledevice3 crash delete timed out after 30s")

        monkeypatch.setattr(ios_crash, "command", lambda: ["/bin/pmd3"])
        monkeypatch.setattr(ios_crash, "_run", run)

        resp = await _clear_device(app, "00008101-PHONE")

        assert resp.status_code == 502
        assert resp.json()["detail"] == (
            "pymobiledevice3 crash delete timed out after 30s; "
            "1 of 2 report(s) may already be deleted; 1 remain"
        )

    async def test_a_phone_matched_only_by_name_is_not_deleted_from(self, app, monkeypatch):
        """The #323 fallback is fine for reading, not for deleting."""
        app.state.device_controller = _controller(lib_udid="LIB")
        sent = []
        self._pmd3(monkeypatch, [["A.ips"]], sent, exact=False)

        resp = await _clear_device(app, "00008101-PHONE")

        assert resp.status_code == 409 and "matched by name" in resp.json()["detail"]
        assert sent == []

    async def test_a_tool_failure_is_not_a_success(self, app, monkeypatch):
        from server.device.ios import devicectl
        from server.sources import ios_crash

        devicectl._remember_identity("00008101-PHONE", "LIB")
        app.state.device_controller = _controller(lib_udid="LIB")

        async def run(argv, what, timeout):
            if what == "crash ls":                  # the listing works; the delete fails
                return "/A.ips\n", ""
            raise ios_crash.IosCrashError("pymobiledevice3 crash delete exited 1: boom")

        monkeypatch.setattr(ios_crash, "command", lambda: ["/bin/pmd3"])
        monkeypatch.setattr(ios_crash, "_run", run)

        resp = await _clear_device(app, "00008101-PHONE")

        assert resp.status_code == 502 and "boom" in resp.json()["detail"]


class TestCrashListShape:
    async def test_a_simulators_crash_is_not_in_a_phones_list(self, app, monkeypatch):
        from server.sources.crash import PullResult

        fixture = Path(__file__).parent / "fixtures" / "crash_ips" / "simulator_debug.ips"
        adapter = app.state.crash_adapter
        adapter.crash_reports.append(adapter._parse_crash_file(fixture, fixture.read_text()))
        app.state.device_controller = _controller(lib_udid="LIB")

        async def pull(lib_udid, *, device_id="", days=3):
            return PullResult()

        monkeypatch.setattr(adapter, "pull_from_device", pull)

        assert (await _latest(app, udid="00008101-PHONE"))["total"] == 0
        assert (await _latest(app))["total"] == 1

    async def test_the_macs_own_crash_is_in_no_devices_list(self, app, monkeypatch):
        """#330: a `node` crash on the Mac sat in every phone's and emulator's
        list. It is still listed without a udid."""
        from server.sources.crash import PullResult

        adapter = app.state.crash_adapter
        for name in ("mac_process", "simulator_system_extension"):
            fixture = Path(__file__).parent / "fixtures" / "crash_ips" / f"{name}.ips"
            adapter.crash_reports.append(adapter._parse_crash_file(fixture, fixture.read_text()))
        app.state.device_controller = _controller(lib_udid="LIB")

        async def pull(lib_udid, *, device_id="", days=3):
            return PullResult()

        monkeypatch.setattr(adapter, "pull_from_device", pull)

        assert (await _latest(app, udid="00008101-PHONE"))["total"] == 0
        on_sim = await _latest(app, udid="00000000-0000-0000-0000-00000000000b")
        assert [c["process"] for c in on_sim["crashes"]] == ["TypeToSiriWidgetExtension"]
        assert (await _latest(app))["total"] == 2

    async def test_a_report_that_cannot_be_placed_is_still_listed(self, app, monkeypatch):
        """What might be the device's crash stays in its list."""
        from server.models import CrashReport
        from server.sources.crash import PullResult

        adapter = app.state.crash_adapter
        adapter.crash_reports.append(
            CrashReport(crash_id="a", timestamp=datetime(2026, 9, 27, tzinfo=UTC)))
        app.state.device_controller = _controller(lib_udid="LIB")

        async def pull(lib_udid, *, device_id="", days=3):
            return PullResult()

        monkeypatch.setattr(adapter, "pull_from_device", pull)

        assert (await _latest(app, udid="00008101-PHONE"))["total"] == 1

    async def test_a_simulator_asked_for_in_lower_case(self, app, monkeypatch):
        """Its UDID is read upper-case from the report's path."""
        fixture = Path(__file__).parent / "fixtures" / "crash_ips" / "simulator_fatal_error.ips"
        adapter = app.state.crash_adapter
        adapter.crash_reports.append(adapter._parse_crash_file(fixture, fixture.read_text()))
        app.state.device_controller = _controller()

        data = await _latest(app, udid="00000000-0000-0000-0000-00000000000a")

        assert data["total"] == 1

    async def test_raw_text_is_left_out_unless_asked_for(self, app):
        from server.models import CrashReport

        app.state.crash_adapter.crash_reports.append(CrashReport(
            crash_id="a", timestamp=datetime(2026, 9, 27, tzinfo=UTC), raw_text="x" * 3000))

        assert (await _latest(app))["crashes"][0]["raw_text"] == ""
        assert (await _latest(app, include_raw="true"))["crashes"][0]["raw_text"] == "x" * 3000
        # The stored report keeps it.
        assert app.state.crash_adapter.crash_reports[0].raw_text == "x" * 3000

    async def test_frames_and_images_come_with_detail(self, app):
        fixture = Path(__file__).parent / "fixtures" / "crash_ips" / "simulator_fatal_error.ips"
        adapter = app.state.crash_adapter
        adapter.crash_reports.append(adapter._parse_crash_file(fixture, fixture.read_text()))

        compact = (await _latest(app))["crashes"][0]
        assert compact["frames"] == [] and compact["images"] == []
        assert compact["app_frame"]["line"] == 368 and compact["top_frames"]

        full = (await _latest(app, detail="true"))["crashes"][0]
        assert len(full["frames"]) == 30 and full["images"]
        assert len(adapter.crash_reports[0].frames) == 30     # stored in full
