"""Tests for the SimulatorLogAdapter."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.models import LogLevel, LogSource
from server.sources.simulator_log import SimulatorLogAdapter


SAMPLE_UDID = "43B500A9-1234-5678-9ABC-DEF012345678"


@pytest.fixture
def adapter() -> SimulatorLogAdapter:
    return SimulatorLogAdapter(udid=SAMPLE_UDID, device_id="test-device")


@pytest.fixture
def sample_lines() -> list[str]:
    fixture = Path(__file__).parent / "fixtures" / "oslog_sample.json"
    return fixture.read_text().strip().splitlines()


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def test_build_command_no_filters(adapter: SimulatorLogAdapter):
    """Basic command without filters."""
    cmd = adapter._build_command()
    assert cmd == [
        "xcrun", "simctl", "spawn", SAMPLE_UDID,
        "log", "stream", "--style", "json", "--level", "debug",
    ]


def test_build_command_with_process_filter():
    """Process filter adds a predicate."""
    adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, process_filter="MyApp")
    cmd = adapter._build_command()
    assert "--predicate" in cmd
    assert 'process == "MyApp"' in cmd[-1]


def test_build_command_with_subsystem_filter():
    """Subsystem filter adds a predicate."""
    adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, subsystem_filter="com.example.app")
    cmd = adapter._build_command()
    assert "--predicate" in cmd
    assert 'subsystem == "com.example.app"' in cmd[-1]


def test_build_command_with_both_filters():
    """Both filters combined with AND."""
    adapter = SimulatorLogAdapter(
        udid=SAMPLE_UDID,
        process_filter="MyApp",
        subsystem_filter="com.example.app",
    )
    cmd = adapter._build_command()
    assert "--predicate" in cmd
    predicate = cmd[-1]
    assert 'process ==' in predicate
    assert "subsystem ==" in predicate
    assert " AND " in predicate


def test_build_command_custom_level():
    """Custom level is passed through."""
    adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, level="error")
    cmd = adapter._build_command()
    assert "--level" in cmd
    level_idx = cmd.index("--level")
    assert cmd[level_idx + 1] == "error"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_json_line(adapter: SimulatorLogAdapter, sample_lines: list[str]):
    """Valid JSON line produces LogEntry with source=SIMULATOR."""
    entry = adapter._parse_json_line(sample_lines[0])

    assert entry is not None
    assert entry.level == LogLevel.INFO
    assert entry.message == "Request completed in 234ms"
    assert entry.subsystem == "com.myapp.networking"
    assert entry.category == "performance"
    assert entry.process == "MyApp"
    assert entry.pid == 1234
    assert entry.source == LogSource.SIMULATOR
    assert entry.device_id == "test-device"


def test_parse_json_line_error(adapter: SimulatorLogAdapter, sample_lines: list[str]):
    """Error messageType maps to ERROR level."""
    entry = adapter._parse_json_line(sample_lines[1])
    assert entry is not None
    assert entry.level == LogLevel.ERROR
    assert entry.source == LogSource.SIMULATOR


def test_parse_json_line_skip_non_log(adapter: SimulatorLogAdapter):
    """activityEvent lines are skipped."""
    line = '{"eventType":"activityCreateEvent","eventMessage":"some activity"}'
    assert adapter._parse_json_line(line) is None


def test_parse_json_line_invalid(adapter: SimulatorLogAdapter):
    """Invalid JSON returns None."""
    assert adapter._parse_json_line("not json") is None
    assert adapter._parse_json_line("[") is None
    assert adapter._parse_json_line("") is None


def test_parse_json_line_with_leading_comma(adapter: SimulatorLogAdapter, sample_lines: list[str]):
    """Lines with leading comma from JSON array format still parse."""
    entry = adapter._parse_json_line("," + sample_lines[0])
    assert entry is not None
    assert entry.message == "Request completed in 234ms"


# ---------------------------------------------------------------------------
# Adapter identity
# ---------------------------------------------------------------------------


def test_adapter_id_includes_udid():
    """Adapter ID includes first 8 chars of UDID for uniqueness."""
    adapter = SimulatorLogAdapter(udid=SAMPLE_UDID)
    assert adapter.adapter_id == "simlog-43B500A9"
    assert adapter.adapter_type == "simctl_log_stream"


def test_different_udids_get_different_ids():
    """Different UDIDs produce different adapter IDs."""
    a1 = SimulatorLogAdapter(udid="AAAA0000-1111-2222-3333-444455556666")
    a2 = SimulatorLogAdapter(udid="BBBB0000-1111-2222-3333-444455556666")
    assert a1.adapter_id != a2.adapter_id


# ---------------------------------------------------------------------------
# Lifecycle (mocked subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    """Start spawns subprocess, stop terminates it."""
    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.__aiter__ = MagicMock(return_value=iter([]))
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID)
        await adapter.start()

        assert adapter.is_running
        assert adapter.started_at is not None
        mock_exec.assert_called_once()

        # Verify the command includes simctl spawn
        call_args = mock_exec.call_args[0]
        assert "xcrun" in call_args
        assert "simctl" in call_args
        assert "spawn" in call_args
        assert SAMPLE_UDID in call_args

        mock_proc.returncode = None
        await adapter.stop()
        assert not adapter.is_running
        mock_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_start_xcrun_not_found():
    """FileNotFoundError sets error state without crashing."""
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID)
        await adapter.start()

        assert not adapter.is_running
        assert adapter._error is not None
        assert "xcrun" in adapter._error


@pytest.mark.asyncio
async def test_read_loop_emits_entries():
    """Read loop parses lines and emits entries via callback."""
    sample_line = (
        b'{"traceID":1,"eventMessage":"hello from sim","eventType":"logEvent",'
        b'"subsystem":"com.test","category":"test",'
        b'"timestamp":"2026-02-07 14:23:01.000000-0800",'
        b'"messageType":"Default","processID":42,'
        b'"processImagePath":"/path/to/TestApp"}\n'
    )

    emitted = []

    async def on_entry(entry):
        emitted.append(entry)

    mock_proc = AsyncMock()
    mock_proc.returncode = None

    # Create an async iterator that yields one line
    class MockStdout:
        def __init__(self):
            self._lines = [sample_line]
            self._index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._lines):
                raise StopAsyncIteration
            line = self._lines[self._index]
            self._index += 1
            return line

    mock_proc.stdout = MockStdout()
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, on_entry=on_entry)
        await adapter.start()

        # Give the read loop a moment to process
        import asyncio
        await asyncio.sleep(0.1)

        await adapter.stop()

    assert len(emitted) == 1
    assert emitted[0].message == "hello from sim"
    assert emitted[0].source == LogSource.SIMULATOR
    assert emitted[0].process == "TestApp"


@pytest.mark.asyncio
async def test_read_loop_pretty_printed_json():
    """Read loop handles pretty-printed multi-line JSON from simctl spawn."""
    # Simulate the pretty-printed output from simctl spawn
    lines = [
        b'Filtering the log data using "process == \\"TestApp\\""\n',
        b'[{\n',
        b'  "eventMessage" : "hello pretty",\n',
        b'  "eventType" : "logEvent",\n',
        b'  "subsystem" : "com.test",\n',
        b'  "category" : "test",\n',
        b'  "timestamp" : "2026-02-07 14:23:01.000000-0800",\n',
        b'  "messageType" : "Default",\n',
        b'  "processID" : 42,\n',
        b'  "processImagePath" : "/path/to/TestApp"\n',
        b'}]\n',
    ]

    emitted = []

    async def on_entry(entry):
        emitted.append(entry)

    mock_proc = AsyncMock()
    mock_proc.returncode = None

    class MockStdout:
        def __init__(self):
            self._lines = lines
            self._index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._lines):
                raise StopAsyncIteration
            line = self._lines[self._index]
            self._index += 1
            return line

    mock_proc.stdout = MockStdout()
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, on_entry=on_entry)
        await adapter.start()

        import asyncio
        await asyncio.sleep(0.1)

        await adapter.stop()

    assert len(emitted) == 1
    assert emitted[0].message == "hello pretty"
    assert emitted[0].source == LogSource.SIMULATOR
