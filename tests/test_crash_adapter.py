"""Tests for the crash report watcher adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.models import LogLevel, LogSource
from server.sources.crash import CrashAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_crash_dir(tmp_path):
    """Provide a temporary crash directory."""
    d = tmp_path / "crashes"
    d.mkdir()
    return d


def _collect_entries(adapter: CrashAdapter):
    """Replace the adapter's on_entry with a collector."""
    entries = []

    async def collect(entry):
        entries.append(entry)

    adapter.on_entry = collect
    return entries


# ------------------------------------------------------------------
# .ips parsing
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_ips_fixture(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.1)
    entries = _collect_entries(adapter)

    await adapter.start()

    # Copy fixture AFTER start so it's detected as new
    src = FIXTURES / "crash_sample.ips"
    (tmp_crash_dir / "crash_sample.ips").write_text(src.read_text())

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.level == LogLevel.FAULT
    assert entry.source == LogSource.CRASH
    assert "MyApp" in entry.process
    assert "CRASH" in entry.message

    # Check parsed crash report
    assert len(adapter.crash_reports) == 1
    report = adapter.crash_reports[0]
    assert report.process == "MyApp"
    assert report.exception_type == "EXC_CRASH"
    assert report.signal == "SIGABRT"
    assert len(report.top_frames) > 0
    assert "crashAction" in report.top_frames[0]


@pytest.mark.asyncio
async def test_parse_ips_with_header_line(tmp_crash_dir):
    """Some .ips files have a non-JSON header line before the JSON body."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.1)
    entries = _collect_entries(adapter)

    await adapter.start()

    ips_data = {
        "procName": "HeaderApp",
        "exception": {"type": "EXC_BREAKPOINT", "signal": "SIGTRAP"},
        "faultingThread": 0,
        "threads": [{"frames": [{"symbol": "swift_runtime_unreachable"}]}],
    }
    content = f'{{"bug_type":"309"}}\n{json.dumps(ips_data)}'
    (tmp_crash_dir / "with_header.ips").write_text(content)

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1
    report = adapter.crash_reports[0]
    assert report.process == "HeaderApp"
    assert report.signal == "SIGTRAP"


# ------------------------------------------------------------------
# .crash parsing
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_crash_fixture(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.1)
    entries = _collect_entries(adapter)

    await adapter.start()

    src = FIXTURES / "crash_sample.crash"
    (tmp_crash_dir / "crash_sample.crash").write_text(src.read_text())

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.level == LogLevel.FAULT
    assert "MyApp" in entry.message

    report = adapter.crash_reports[0]
    assert report.process == "MyApp"
    assert "EXC_BAD_ACCESS" in report.exception_type
    assert report.signal == "SIGSEGV"
    assert len(report.top_frames) > 0
    assert "cellForRowAtIndexPath" in report.top_frames[0]


# ------------------------------------------------------------------
# Dedup / re-scan behavior
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_does_not_reemit_on_restart(tmp_crash_dir):
    """Files present at startup should be indexed but not emitted."""
    # Pre-populate before start
    src = FIXTURES / "crash_sample.ips"
    (tmp_crash_dir / "existing.ips").write_text(src.read_text())

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.1)
    entries = _collect_entries(adapter)

    await adapter.start()
    await asyncio.sleep(0.5)

    # File was there before start — should not emit, nor list: a loose file
    # names no device, and listed it would appear under every device's udid.
    assert len(entries) == 0
    assert adapter.crash_reports == []

    # Now add a new file
    (tmp_crash_dir / "new_crash.ips").write_text(src.read_text())
    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1


@pytest.mark.asyncio
async def test_ignores_non_crash_files(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.1)
    entries = _collect_entries(adapter)

    await adapter.start()

    (tmp_crash_dir / "readme.txt").write_text("not a crash")
    (tmp_crash_dir / "data.json").write_text("{}")

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 0


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Extra watch dirs
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extra_watch_dirs_scanned(tmp_crash_dir, tmp_path):
    """Crash files in extra_watch_dirs should be detected."""
    extra_dir = tmp_path / "diagnostic_reports"
    extra_dir.mkdir()

    adapter = CrashAdapter(
        watch_dir=tmp_crash_dir,
        poll_interval=0.1,
        extra_watch_dirs=[extra_dir],
    )
    entries = _collect_entries(adapter)

    await adapter.start()

    # Add a crash file to the extra dir
    src = FIXTURES / "crash_sample.ips"
    (extra_dir / "sim_crash.ips").write_text(src.read_text())

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1
    report = adapter.crash_reports[0]
    assert report.process == "MyApp"


@pytest.mark.asyncio
async def test_extra_watch_dirs_indexed_at_start(tmp_crash_dir, tmp_path):
    """Files already in extra dirs at startup should be indexed, not emitted."""
    extra_dir = tmp_path / "diagnostic_reports"
    extra_dir.mkdir()

    src = FIXTURES / "crash_sample.ips"
    (extra_dir / "old_crash.ips").write_text(src.read_text())

    adapter = CrashAdapter(
        watch_dir=tmp_crash_dir,
        poll_interval=0.1,
        extra_watch_dirs=[extra_dir],
    )
    entries = _collect_entries(adapter)

    await adapter.start()
    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 0


# ------------------------------------------------------------------
# Process filter
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_filter_skips_non_matching(tmp_crash_dir):
    """Crashes from non-matching processes should be ignored."""
    adapter = CrashAdapter(
        watch_dir=tmp_crash_dir,
        poll_interval=0.1,
        process_filter="Geocaching",
    )
    entries = _collect_entries(adapter)

    await adapter.start()

    # crash_sample.ips has procName="MyApp" — should not match
    src = FIXTURES / "crash_sample.ips"
    (tmp_crash_dir / "myapp_crash.ips").write_text(src.read_text())

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 0
    assert len(adapter.crash_reports) == 0


@pytest.mark.asyncio
async def test_process_filter_allows_matching(tmp_crash_dir):
    """Crashes from matching processes should be captured."""
    adapter = CrashAdapter(
        watch_dir=tmp_crash_dir,
        poll_interval=0.1,
        process_filter="MyApp",
    )
    entries = _collect_entries(adapter)

    await adapter.start()

    src = FIXTURES / "crash_sample.ips"
    (tmp_crash_dir / "myapp_crash.ips").write_text(src.read_text())

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1
    assert adapter.crash_reports[0].process == "MyApp"


@pytest.mark.asyncio
async def test_process_filter_skips_non_matching_crash_text(tmp_crash_dir):
    """Process filter should also work for .crash text format."""
    adapter = CrashAdapter(
        watch_dir=tmp_crash_dir,
        poll_interval=0.1,
        process_filter="Geocaching",
    )
    entries = _collect_entries(adapter)

    await adapter.start()

    # crash_sample.crash has Process: MyApp — should not match
    src = FIXTURES / "crash_sample.crash"
    (tmp_crash_dir / "myapp.crash").write_text(src.read_text())

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 0


# ------------------------------------------------------------------
# bug_type filtering
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_non_crash_ips(tmp_crash_dir):
    """Non-crash .ips files (Jetsam, SFA, analytics) should be ignored."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.1)
    entries = _collect_entries(adapter)

    await adapter.start()

    # Jetsam event (bug_type 298)
    jetsam = '{"bug_type":"298"}\n{"build":"iPhone OS 18.6","product":"iPhone12,1"}'
    (tmp_crash_dir / "JetsamEvent.ips").write_text(jetsam)

    # SFA diagnostic (bug_type 226)
    sfa = '{"bug_type":"226"}\n{"postTime":123,"events":[]}'
    (tmp_crash_dir / "SFA-networking.ips").write_text(sfa)

    # Real crash (bug_type 309) — should be captured
    crash = json.dumps(
        {
            "bug_type": "309",
            "procName": "TestApp",
            "exception": {"type": "EXC_CRASH", "signal": "SIGABRT"},
            "faultingThread": 0,
            "threads": [{"frames": [{"symbol": "abort"}]}],
        }
    )
    (tmp_crash_dir / "TestApp-crash.ips").write_text(f'{{"bug_type":"309"}}\n{crash}')

    await asyncio.sleep(0.5)
    await adapter.stop()

    assert len(entries) == 1
    assert adapter.crash_reports[0].process == "TestApp"


# ------------------------------------------------------------------
# on-crash hook
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_crash_hook_receives_json(tmp_crash_dir, tmp_path):
    """The on-crash hook should receive valid CrashReport JSON on stdin."""
    output_file = tmp_path / "hook_output.json"
    hook_cmd = f"cat > {output_file}"

    adapter = CrashAdapter(
        watch_dir=tmp_crash_dir,
        poll_interval=0.1,
        on_crash_hook=hook_cmd,
    )
    entries = _collect_entries(adapter)

    await adapter.start()

    src = FIXTURES / "crash_sample.ips"
    (tmp_crash_dir / "hook_test.ips").write_text(src.read_text())

    # Wait for poll + hook to complete
    await asyncio.sleep(1.0)
    await adapter.stop()

    assert len(entries) == 1
    assert output_file.exists(), "Hook output file was not created"

    data = json.loads(output_file.read_text())
    assert data["process"] == "MyApp"
    assert data["exception_type"] == "EXC_CRASH"
    assert data["signal"] == "SIGABRT"
    assert "crash_id" in data


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_watching(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir)
    await adapter.start()

    status = adapter.status()
    assert status.status == "watching"
    assert status.type == "crash_reporter"

    await adapter.stop()
    status = adapter.status()
    assert status.status == "stopped"


# ------------------------------------------------------------------
# On-demand pull_from_device
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_from_device_with_udid(tmp_crash_dir):
    """pull_from_device should call idevicecrashreport with -u <udid>."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    _collect_entries(adapter)
    await adapter.start()

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        await adapter.pull_from_device("00008030-AABBCCDD")

        # Verify the -u flag was passed
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert "-u" in args
        assert "00008030-AABBCCDD" in args
        assert "-e" in args
        # Keep the reports on the phone: without -k the tool deletes them.
        assert "-k" in args

    await adapter.stop()


@pytest.mark.asyncio
async def test_pull_from_device_without_udid(tmp_crash_dir):
    """pull_from_device without UDID should not pass -u flag."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        await adapter.pull_from_device()

        args = mock_exec.call_args[0]
        assert "-u" not in args
        assert "-e" in args
        assert "-k" in args

    await adapter.stop()


@pytest.mark.asyncio
async def test_pull_from_device_returns_new_reports(tmp_crash_dir):
    """pull_from_device should return only newly discovered crash reports."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    _collect_entries(adapter)
    await adapter.start()

    src = FIXTURES / "crash_sample.ips"

    async def fake_communicate():
        # Simulate idevicecrashreport writing a file
        (tmp_crash_dir / "pulled_crash.ips").write_text(src.read_text())
        return b"", b""

    mock_proc = AsyncMock()
    mock_proc.communicate = fake_communicate
    mock_proc.returncode = 0

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        result = await adapter.pull_from_device("00008030-AABBCCDD")

    assert result.error is None
    assert len(result.new) == 1
    assert result.new[0].process == "MyApp"
    await adapter.stop()


@pytest.mark.asyncio
async def test_reports_left_on_the_phone_are_not_new_twice(tmp_crash_dir):
    """With -k every pull copies the phone's whole history again, to the same
    paths. That must not turn one crash into a new report on each pull, nor
    after a restart."""
    fresh = []    # stamped at the first copy, after start; the same bytes each time

    async def fake_communicate():
        fresh[:] = fresh or [_fresh_ips()]
        (tmp_crash_dir / "Calculator-2026-09-27-143510.ips").write_text(fresh[0])
        return b"", b""

    mock_proc = AsyncMock()
    mock_proc.communicate = fake_communicate
    mock_proc.returncode = 0

    async def pull(adapter):
        with (
            patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            return await adapter.pull_from_device("00008101-HWUDID")

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    entries = _collect_entries(adapter)
    await adapter.start()
    assert len((await pull(adapter)).new) == 1
    assert (await pull(adapter)).new == []
    assert len(entries) == 1
    await adapter.stop()

    restarted = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    later = _collect_entries(restarted)
    await restarted.start()
    assert (await pull(restarted)).new == []
    assert later == []
    await restarted.stop()


@pytest.mark.asyncio
async def test_pull_from_device_no_binary(tmp_crash_dir):
    """A missing idevicecrashreport is a failed pull, not an empty one: the
    silent `[]` it used to return read as "no new crashes" (#316)."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()

    with patch("shutil.which", return_value=None):
        result = await adapter.pull_from_device("00008030-AABBCCDD")

    assert result.new == []
    assert "idevicecrashreport not found" in result.error
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_failing_pull_says_why(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"ERROR: No device found"))
    mock_proc.returncode = 255

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        result = await adapter.pull_from_device("00008030-AABBCCDD")

    assert "exited 255" in result.error and "No device found" in result.error
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_pull_that_times_out_says_so(tmp_crash_dir, monkeypatch):
    import asyncio

    from server.sources import crash as crash_module

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()
    mock_proc = AsyncMock()
    mock_proc.returncode = None

    async def hang():
        await asyncio.sleep(3600)

    mock_proc.communicate = hang
    killed = []
    mock_proc.kill = lambda: killed.append(True)
    monkeypatch.setattr(crash_module, "PULL_TIMEOUT", 0.05)

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        result = await adapter.pull_from_device("00008030-AABBCCDD")

    assert "timed out" in result.error
    assert killed == [True]           # not left running against a wedged phone
    mock_proc.wait.assert_awaited()   # and reaped
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_pull_that_cannot_start_says_so(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", side_effect=PermissionError("denied")),
    ):
        result = await adapter.pull_from_device("00008030-AABBCCDD")

    assert "could not run idevicecrashreport" in result.error
    await adapter.stop()


# ---------------------------------------------------------------------------
# which reports a pull produced (#316 review)
# ---------------------------------------------------------------------------


def _fresh_ips():
    """crash_sample.ips, stamped now: a crash from after the adapter started."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f +0000")
    return (FIXTURES / "crash_sample.ips").read_text().replace("2026-02-08 10:30:45.000 +0000", now)


async def _pull_with(adapter, write, device_id="PHONE-UUID", sent=None):
    """Pull with an idevicecrashreport that runs `write(target)` while it runs,
    `target` being the directory the command was told to write to."""
    proc = AsyncMock()
    proc.returncode = 0

    async def exec_(*args, **kwargs):
        if sent is not None:
            sent.extend(args)

        async def communicate():
            await write(Path(args[-1]))
            return b"", b""

        proc.communicate = communicate
        return proc

    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", side_effect=exec_),
    ):
        return await adapter.pull_from_device("00008101-HW", device_id=device_id)


@pytest.mark.asyncio
async def test_a_pulled_report_names_the_phone_on_its_log_entry_too(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    entries = _collect_entries(adapter)
    await adapter.start()

    async def write(target):
        (target / "MyApp-1.ips").write_text(_fresh_ips())

    result = await _pull_with(adapter, write)

    assert [r.device_id for r in result.new] == ["PHONE-UUID"]
    assert [e.device_id for e in entries] == ["PHONE-UUID"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_simulator_crash_found_during_a_pull_is_not_the_phones(tmp_crash_dir, tmp_path):
    """The pull scans every watched directory, DiagnosticReports included. A
    simulator's crash written since the last poll was counted as the phone's
    and tagged with its udid, and vanished from the simulator's own list."""
    sim_dir = tmp_path / "DiagnosticReports"
    sim_dir.mkdir()
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60, extra_watch_dirs=[sim_dir])
    entries = _collect_entries(adapter)
    await adapter.start()
    (sim_dir / "MyApp-sim.ips").write_text((FIXTURES / "crash_sample.ips").read_text())

    async def write(target):
        pass                                   # the phone had nothing

    result = await _pull_with(adapter, write)

    assert result.new == []
    assert [r.device_id for r in adapter.crash_reports] == [""]
    assert [e.device_id for e in entries] == [""]      # still found, just not the phone's
    await adapter.stop()


@pytest.mark.asyncio
async def test_the_poll_loop_cannot_take_a_pulls_files_mid_pull(tmp_crash_dir):
    """The poll loop scanning while idevicecrashreport was still writing took
    the phone's reports as its own: untagged and uncounted."""
    import asyncio

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.001)
    await adapter.start()

    async def write(target):
        (target / "MyApp-2.ips").write_text((FIXTURES / "crash_sample.ips").read_text())
        await asyncio.sleep(0.1)              # the poll loop gets many chances

    result = await _pull_with(adapter, write)

    assert [r.device_id for r in result.new] == ["PHONE-UUID"]
    await adapter.stop()


def _report(**fields):
    from datetime import UTC, datetime

    from server.models import CrashReport

    return CrashReport(**{
        "crash_id": "android-1", "timestamp": datetime.now(UTC), "process": "com.example.myapp",
        "device_id": "emulator-5554", **fields,
    })


@pytest.mark.asyncio
async def test_added_reports_keep_their_own_device_on_the_log_entry(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    entries = _collect_entries(adapter)
    await adapter.start()

    await adapter.add_reports([_report()])

    assert [e.device_id for e in entries] == ["emulator-5554"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_added_reports_obey_the_process_filter(tmp_crash_dir):
    """`--crash-process-filter` applied to report files and not to DropBox."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60,
                           process_filter="com.example.myapp")
    await adapter.start()

    new = await adapter.add_reports([
        _report(), _report(crash_id="android-2", process="com.android.settings"),
    ])

    assert [r.process for r in new] == ["com.example.myapp"]
    assert [r.process for r in adapter.crash_reports] == ["com.example.myapp"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_history_from_before_start_is_listed_not_replayed(tmp_crash_dir, tmp_path):
    """DropBox keeps days of records and the list starts empty, so every
    restart logged the whole history as arriving now and ran the hook once
    per record."""
    import asyncio
    from datetime import timedelta

    marker = tmp_path / "hook-ran"
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60,
                           on_crash_hook=f"cat >> {marker}")
    entries = _collect_entries(adapter)
    await adapter.start()
    old = _report(crash_id="old", timestamp=adapter.started_at - timedelta(days=2))

    new = await adapter.add_reports([old])
    await asyncio.sleep(0.3)

    assert [r.crash_id for r in new] == ["old"]              # still in the list
    assert entries == []                                     # not on the timeline
    assert not marker.exists()                               # no hook
    await adapter.stop()


@pytest.mark.asyncio
async def test_the_hook_runs_for_a_crash_logcat_already_logged(tmp_crash_dir, tmp_path):
    """Logcat does not run the hook, so skipping it with the log entry made it
    fire only when capture happened to be off."""
    import asyncio

    marker = tmp_path / "hook-ran"
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60,
                           on_crash_hook=f"cat >> {marker}")
    entries = _collect_entries(adapter)
    await adapter.start()

    async def logged(report):
        return True

    await adapter.add_reports([_report()], already_logged=logged)
    for _ in range(50):
        if marker.exists() and marker.read_text():
            break
        await asyncio.sleep(0.05)

    assert entries == []
    assert "com.example.myapp" in marker.read_text()
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_pulled_crash_from_before_start_is_listed_not_replayed(tmp_crash_dir, tmp_path):
    """Reports stay on the phone now (-k), so the first pull into an empty
    directory copies its whole history; logging each old crash as new, and
    running the hook for each, is the replay add_reports already refuses."""
    import asyncio

    marker = tmp_path / "hook-ran"
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60,
                           on_crash_hook=f"cat >> {marker}")
    entries = _collect_entries(adapter)
    await adapter.start()

    async def write(target):      # crash_sample.ips is from 2026-02-08
        (target / "MyApp-old.ips").write_text((FIXTURES / "crash_sample.ips").read_text())

    result = await _pull_with(adapter, write)
    await asyncio.sleep(0.3)

    assert [r.device_id for r in result.new] == ["PHONE-UUID"]      # still reported
    assert entries == []
    assert not marker.exists()
    await adapter.stop()


# ---------------------------------------------------------------------------
# a directory per phone, so a restart still knows whose reports they are
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pull_writes_into_the_phones_own_directory(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()
    sent = []

    async def write(target):
        (target / "MyApp-1.ips").write_text(_fresh_ips())

    result = await _pull_with(adapter, write, sent=sent)

    assert sent[-1] == str(tmp_crash_dir / "devices" / "PHONE-UUID")
    assert Path(result.new[0].file_path).parent == tmp_crash_dir / "devices" / "PHONE-UUID"
    await adapter.stop()


@pytest.mark.asyncio
async def test_after_a_restart_a_phones_reports_are_listed_against_it(tmp_crash_dir, tmp_path):
    """They were marked seen and never listed, so after a restart the phone
    showed no crashes -- and, left on the phone and re-copied to the same
    paths, never would again -- while Android listed its history."""
    import asyncio

    first = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await first.start()

    async def write(target):
        (target / "MyApp-1.ips").write_text(_fresh_ips())

    await _pull_with(first, write)
    await first.stop()

    marker = tmp_path / "hook-ran"
    restarted = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60,
                             on_crash_hook=f"cat >> {marker}")
    entries = _collect_entries(restarted)
    await restarted.start()

    assert [(r.process, r.device_id) for r in restarted.crash_reports] == [("MyApp", "PHONE-UUID")]
    assert (await _pull_with(restarted, write)).new == []      # re-copied, same path
    await asyncio.sleep(0.3)
    assert entries == [] and not marker.exists()               # listed, not replayed
    assert len(restarted.crash_reports) == 1
    await restarted.stop()


@pytest.mark.asyncio
async def test_a_device_id_unfit_for_a_directory_name_still_tags_this_pull(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()

    async def write(target):
        assert target == tmp_crash_dir                # not a path built from "../x"
        (target / "MyApp-1.ips").write_text(_fresh_ips())

    result = await _pull_with(adapter, write, device_id="../x")

    assert [r.device_id for r in result.new] == ["../x"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_the_poll_loop_names_the_phone_for_a_file_in_its_directory(tmp_crash_dir):
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    entries = _collect_entries(adapter)
    await adapter.start()
    phone = tmp_crash_dir / "devices" / "PHONE-UUID"
    phone.mkdir(parents=True)
    (phone / "MyApp-3.ips").write_text(_fresh_ips())

    async with adapter._scan_lock:
        await adapter._scan_for_new_files()

    assert [e.device_id for e in entries] == ["PHONE-UUID"]
    await adapter.stop()


# ---------------------------------------------------------------------------
# failure paths around pulled files (second #316 review)
# ---------------------------------------------------------------------------

#: Well-formed JSON of the wrong shape: the parser raises AttributeError on it.
MALFORMED_IPS = '{"bug_type":"309"}\n{"procName":"MyApp","exception": "boom"}'


@pytest.mark.asyncio
async def test_a_report_cut_short_by_a_timed_out_pull_is_read_when_complete(tmp_crash_dir):
    """The pull scans after a timeout, so it read a half-copied file, failed,
    and marked the path seen; with -k the next pull copies the complete file
    to the same path, and it was skipped for the rest of the session."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    entries = _collect_entries(adapter)
    await adapter.start()
    full = _fresh_ips()

    async def partial(target):
        (target / "MyApp-1.ips").write_text(full[:120])

    async def complete(target):
        (target / "MyApp-1.ips").write_text(full)

    assert (await _pull_with(adapter, partial)).new == []
    assert [r.process for r in (await _pull_with(adapter, complete)).new] == ["MyApp"]
    assert len(entries) == 1
    await adapter.stop()


@pytest.mark.asyncio
async def test_an_unparseable_file_is_not_reread_until_it_changes(tmp_crash_dir):
    """idevicecrashreport rewrites every file it copies (measured, 1.4.0), so
    each -k pull gives an unparseable file -- a JetsamEvent, say -- a new
    mtime and the same size. Keyed on mtime, every pull re-read them all."""
    import os

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()
    broken = tmp_crash_dir / "broken.ips"
    broken.write_text(MALFORMED_IPS)
    reads = []
    real = adapter._parse_crash_file
    adapter._parse_crash_file = lambda path, content: reads.append(path) or real(path, content)

    for i in range(3):
        broken.write_text(MALFORMED_IPS)                    # re-copied, same bytes
        os.utime(broken, (1_800_000_000 + i, 1_800_000_000 + i))
        await adapter._scan_for_new_files()
    assert len(reads) == 1

    broken.write_text(MALFORMED_IPS + " ")                  # the size changed
    await adapter._scan_for_new_files()
    assert len(reads) == 2
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_malformed_report_does_not_fail_the_pull_or_hide_the_rest(tmp_crash_dir):
    """The parsers catch malformed JSON but not the wrong shape, and the
    exception escaped the pull as a 500, leaving later files to the poll loop."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()

    async def write(target):
        (target / "A-broken.ips").write_text(MALFORMED_IPS)
        (target / "B-good.ips").write_text(_fresh_ips())

    result = await _pull_with(adapter, write)

    assert result.error is None
    assert [r.process for r in result.new] == ["MyApp"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_malformed_report_in_a_phones_directory_does_not_stop_start(tmp_crash_dir):
    """start() reads a phone's reports now; one bad file on disk stopped the
    server booting, on every boot, since the file stays."""
    phone = tmp_crash_dir / "devices" / "PHONE-UUID"
    phone.mkdir(parents=True)
    (phone / "broken.ips").write_text(MALFORMED_IPS)
    (phone / "good.ips").write_text((FIXTURES / "crash_sample.ips").read_text())

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()

    assert [(r.process, r.device_id) for r in adapter.crash_reports] == [("MyApp", "PHONE-UUID")]
    await adapter.stop()


@pytest.mark.asyncio
async def test_an_old_report_the_poll_loop_finds_in_a_phones_directory_is_not_replayed(
    tmp_crash_dir, tmp_path,
):
    """Any file there came from a pull. One the poll loop reached first --
    after a cancelled pull, say -- was logged as new with a hook run apiece."""
    import asyncio

    marker = tmp_path / "hook-ran"
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60,
                           on_crash_hook=f"cat >> {marker}")
    entries = _collect_entries(adapter)
    await adapter.start()
    phone = tmp_crash_dir / "devices" / "PHONE-UUID"
    phone.mkdir(parents=True)
    (phone / "old.ips").write_text((FIXTURES / "crash_sample.ips").read_text())   # 2026-02-08

    async with adapter._scan_lock:
        await adapter._scan_for_new_files()
    await asyncio.sleep(0.3)

    assert [(r.process, r.device_id) for r in adapter.crash_reports] == [("MyApp", "PHONE-UUID")]
    assert entries == [] and not marker.exists()
    await adapter.stop()


@pytest.mark.parametrize("name, content", [
    ("undated.ips", '{"bug_type":"309"}\n{"procName":"MyApp"}'),
    ("undated.crash", "Process:  MyApp [1]\nException Type:  EXC_CRASH (SIGABRT)\n"),
])
@pytest.mark.asyncio
async def test_a_report_with_no_readable_time_takes_its_files(tmp_path, name, content):
    """For a report written on this Mac -- a simulator's, in DiagnosticReports
    -- the mtime is when it was written, which beats now. (A pulled file's is
    the copy time: idevicecrashreport rewrites each file it copies.)"""
    import os

    reports_dir = tmp_path / "DiagnosticReports"
    reports_dir.mkdir()
    adapter = CrashAdapter(watch_dir=tmp_path / "crashes", poll_interval=60,
                           extra_watch_dirs=[reports_dir])
    await adapter.start()
    f = reports_dir / name
    f.write_text(content)
    os.utime(f, (1_700_000_000, 1_700_000_000))                 # 2023-11-14

    await adapter._scan_for_new_files()

    [report] = adapter.crash_reports
    assert report.timestamp.timestamp() == 1_700_000_000
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_cut_short_crash_text_report_is_read_when_complete(tmp_crash_dir):
    """.crash, what iOS 14 and older write. The text parser never failed, so a
    partial copy was logged as a crash named after its file, with no exception,
    and marked seen: the complete copy was never read."""
    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    entries = _collect_entries(adapter)
    await adapter.start()
    full = (FIXTURES / "crash_sample.crash").read_text()
    assert full.index("Exception Type:") > 200             # cut before it

    async def partial(target):
        (target / "MyApp-1.crash").write_text(full[:200])

    async def complete(target):
        (target / "MyApp-1.crash").write_text(full)

    assert (await _pull_with(adapter, partial)).new == []
    assert entries == []
    [report] = (await _pull_with(adapter, complete)).new
    assert report.exception_type == "EXC_BAD_ACCESS (SIGSEGV)"
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_cancelled_pull_does_not_leave_the_tool_running(tmp_crash_dir):
    import asyncio

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()
    proc = AsyncMock()
    proc.returncode = None
    killed = []
    proc.kill = lambda: killed.append(True)

    async def hang():
        await asyncio.sleep(3600)

    proc.communicate = hang
    with (
        patch("shutil.which", return_value="/usr/local/bin/idevicecrashreport"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        task = asyncio.create_task(adapter.pull_from_device("HW", device_id="PHONE-UUID"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert killed == [True]
    assert not adapter._scan_lock.locked()
    await adapter.stop()


@pytest.mark.asyncio
async def test_the_poll_loop_does_not_queue_behind_a_pull(tmp_crash_dir):
    """A pull holds the lock for up to its 30s timeout. The loop waiting on it
    would only have made a redundant scan afterwards -- the pull's own scan
    covers every directory -- so it skips the turn instead of queueing."""
    import asyncio

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=0.01)
    await adapter.start()
    async with adapter._scan_lock:
        await asyncio.sleep(0.1)                     # many poll turns
        assert not adapter._scan_lock._waiters       # none of them waiting
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_partial_copy_left_from_before_a_restart_is_read_when_completed(tmp_crash_dir):
    """The server stopped (or the pull timed out) mid-copy; start() finds the
    partial file. The next pull's complete copy must still be read."""
    phone = tmp_crash_dir / "devices" / "PHONE-UUID"
    phone.mkdir(parents=True)
    full = _fresh_ips()
    (phone / "MyApp-1.ips").write_text(full[:120])

    adapter = CrashAdapter(watch_dir=tmp_crash_dir, poll_interval=60)
    await adapter.start()
    assert adapter.crash_reports == []

    async def complete(target):
        (target / "MyApp-1.ips").write_text(full)

    assert [r.process for r in (await _pull_with(adapter, complete)).new] == ["MyApp"]
    await adapter.stop()
