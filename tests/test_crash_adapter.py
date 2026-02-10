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
