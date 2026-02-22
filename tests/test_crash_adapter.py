"""Tests for the crash report watcher adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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

    # File was there before start — should not emit
    assert len(entries) == 0

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
    crash = json.dumps({
        "bug_type": "309",
        "procName": "TestApp",
        "exception": {"type": "EXC_CRASH", "signal": "SIGABRT"},
        "faultingThread": 0,
        "threads": [{"frames": [{"symbol": "abort"}]}],
    })
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
