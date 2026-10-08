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
        "xcrun",
        "simctl",
        "spawn",
        SAMPLE_UDID,
        "log",
        "stream",
        "--style",
        "json",
        "--level",
        "debug",
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
    assert "process ==" in predicate
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
    # A stream that stays open, as a live one does. The mock this replaced
    # made the read loop crash at once, and the test passed only because it
    # looked before the loop ran and never looked at the error.
    mock_proc, _ = _stream(eof=False)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID)
        await adapter.start()
        import asyncio

        await asyncio.sleep(0.05)
        assert adapter.is_running
        assert adapter._error is None
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


def _stream(*chunks: bytes, eof: bool = True, stderr: bytes = b"", code: int = 0,
            on_terminate=None):
    """A process whose stdout and stderr are real StreamReaders, as asyncio
    gives them.

    Terminating it ends the stream, as terminating `log stream` closes its
    stdout -- or runs `on_terminate(reader)` instead, for a stream that still
    has output to deliver. Chunks are written as-is, newline or not.
    """
    import asyncio

    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    err = asyncio.StreamReader()
    err.feed_data(stderr)
    err.feed_eof()

    proc = MagicMock()
    proc.returncode = None
    proc.stdout = reader
    proc.stderr = err

    def terminate():
        if on_terminate is not None:
            on_terminate(reader)
        elif not reader.at_eof():
            reader.feed_eof()

    proc.terminate = MagicMock(side_effect=terminate)
    proc.wait = AsyncMock(return_value=code)
    proc.kill = MagicMock()
    return proc, reader


def _pretty(message: str) -> bytes:
    """One entry as `simctl spawn ... log stream --style json` writes it: the
    closing brace has no newline after it."""
    return (
        "{\n"
        f'  "eventMessage" : "{message}",\n'
        '  "eventType" : "logEvent",\n'
        '  "subsystem" : "com.test",\n'
        '  "category" : "test",\n'
        '  "timestamp" : "2026-02-07 14:23:01.000000-0800",\n'
        '  "messageType" : "Default",\n'
        '  "processID" : 42,\n'
        '  "processImagePath" : "/path/to/TestApp"\n'
        "}"
    ).encode()


async def _collect(proc, settle: float = 0.1):
    import asyncio

    emitted = []

    async def on_entry(entry):
        emitted.append(entry)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, on_entry=on_entry)
        await adapter.start()
        await asyncio.sleep(settle)
    return adapter, emitted


@pytest.mark.asyncio
async def test_read_loop_emits_entries():
    """Read loop parses compact JSON and emits entries via callback."""
    proc, _ = _stream(
        b'{"traceID":1,"eventMessage":"hello from sim","eventType":"logEvent",'
        b'"subsystem":"com.test","category":"test",'
        b'"timestamp":"2026-02-07 14:23:01.000000-0800",'
        b'"messageType":"Default","processID":42,'
        b'"processImagePath":"/path/to/TestApp"}\n'
    )
    adapter, emitted = await _collect(proc)
    await adapter.stop()

    assert len(emitted) == 1
    assert emitted[0].message == "hello from sim"
    assert emitted[0].source == LogSource.SIMULATOR
    assert emitted[0].process == "TestApp"


@pytest.mark.asyncio
async def test_read_loop_pretty_printed_json():
    """Read loop handles pretty-printed multi-line JSON from simctl spawn."""
    proc, _ = _stream(
        b'Filtering the log data using "process == \\"TestApp\\""\n',
        b"[" + _pretty("hello pretty") + b"]\n",
    )
    adapter, emitted = await _collect(proc)
    await adapter.stop()

    assert len(emitted) == 1
    assert emitted[0].message == "hello pretty"
    assert emitted[0].source == LogSource.SIMULATOR


@pytest.mark.asyncio
async def test_the_newest_entry_is_not_held_for_the_next_one():
    """`log stream` writes an entry's closing brace with no newline; the `,`
    and newline come with the next entry. A line reader therefore held the
    newest entry until another arrived -- for good once the app went quiet."""
    proc, reader = _stream(b"[" + _pretty("first") + b",\n" + _pretty("newest"), eof=False)
    adapter, emitted = await _collect(proc)

    assert [e.message for e in emitted] == ["first", "newest"], \
        "the newest entry waited for one that never came"
    await adapter.stop()


@pytest.mark.asyncio
async def test_stopping_keeps_what_was_already_written():
    """Stopping a recording or logging must not drop an entry the stream had
    already written but the reader had not got to yet."""
    import asyncio

    proc, reader = _stream(eof=False)
    emitted = []

    async def on_entry(entry):
        emitted.append(entry)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, on_entry=on_entry)
        await adapter.start()
        await asyncio.sleep(0)
        # Written, and stop called, before the reader runs again.
        reader.feed_data(b"[" + _pretty("logged just before stop"))
        await adapter.stop()

    assert [e.message for e in emitted] == ["logged just before stop"]


@pytest.mark.asyncio
async def test_a_character_split_across_reads_is_kept_whole():
    """A read returns whatever bytes are waiting, so one can end inside a
    multi-byte character. Decoded per read, both halves became U+FFFD."""
    import asyncio

    entry = b"[" + _pretty("caf\u00e9 \u2713")
    cut = entry.index("\u00e9".encode()) + 1          # inside the two-byte é
    proc, reader = _stream(entry[:cut], eof=False)
    adapter, emitted = await _collect(proc)            # the first half is read
    reader.feed_data(entry[cut:])                      # and only then the rest
    await asyncio.sleep(0.1)
    await adapter.stop()

    assert [e.message for e in emitted] == ["caf\u00e9 \u2713"]


@pytest.mark.asyncio
async def test_stopping_drains_output_still_arriving():
    """The stream can still be delivering when stop() is called. Everything up
    to its end is kept, not only what one read happened to get."""
    import asyncio

    async def trickle(reader):
        for message in ("one", "two", "three"):
            reader.feed_data(b"," + _pretty(message))
            await asyncio.sleep(0.02)
        reader.feed_eof()

    pending = []
    proc, reader = _stream(b"[", eof=False,
                           on_terminate=lambda r: pending.append(asyncio.ensure_future(trickle(r))))
    adapter, emitted = await _collect(proc)
    await adapter.stop()

    assert [e.message for e in emitted] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_two_stops_at_once_both_return():
    """/logs/filter restarts the adapter without the logging lock, so a stop
    can overlap another. The second used to re-read the task the first had
    already cleared, and raised AttributeError -- a 500."""
    import asyncio

    proc, _ = _stream(b"[" + _pretty("x"), eof=False)
    adapter, _ = await _collect(proc)
    results = await asyncio.gather(adapter.stop(), adapter.stop(), return_exceptions=True)
    assert results == [None, None]


@pytest.mark.asyncio
async def test_an_escape_split_across_reads_is_kept():
    """A read can end on the backslash of an escaped quote. Forgetting the
    escape across the read takes the quote for the string's end, and the brace
    after it then closes the object early."""
    import asyncio

    entry = b"[" + _pretty('say \\"}\\" ok')
    cut = entry.index(b"\\") + 1                     # right after the backslash
    proc, reader = _stream(entry[:cut], eof=False)
    adapter, emitted = await _collect(proc)
    reader.feed_data(entry[cut:])
    await asyncio.sleep(0.1)
    await adapter.stop()

    assert [e.message for e in emitted] == ['say "}" ok']


@pytest.mark.asyncio
async def test_a_stream_that_ends_on_its_own_says_why():
    """simctl exiting -- a simulator that is not booted -- read as a clean
    stop: status "stopped", no error."""
    proc, _ = _stream(stderr=b"Unable to locate device set\n", code=148)
    adapter, _ = await _collect(proc)

    assert not adapter.is_running
    assert adapter.status().status == "error"
    assert "exited (148)" in adapter._error
    assert "Unable to locate device set" in adapter._error


@pytest.mark.asyncio
async def test_a_failure_while_draining_is_logged(caplog):
    """The drain runs after _running is cleared, where errors used to be
    recorded only while running -- so one there vanished."""
    import asyncio
    import logging

    proc, reader = _stream(eof=False)

    async def on_entry(_entry):
        raise RuntimeError("downstream broke")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, on_entry=on_entry)
        await adapter.start()
        await asyncio.sleep(0)
        reader.feed_data(b"[" + _pretty("late"))
        with caplog.at_level(logging.ERROR, logger="server.sources.simulator_log"):
            await adapter.stop()

    assert any("read loop failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconfigure_updates_filters_and_restarts():
    """reconfigure() stops, updates filters, restarts with new command."""
    import asyncio

    # A fresh, open stream per spawn, as a real restart gets. The mock this
    # replaced crashed the read loop at once; the test looked too early to see.
    with patch("asyncio.create_subprocess_exec",
               side_effect=lambda *a, **k: _stream(eof=False)[0]) as mock_exec:
        adapter = SimulatorLogAdapter(
            udid=SAMPLE_UDID,
            process_filter="OldApp",
            subsystem_filter="com.old",
        )
        await adapter.start()
        assert adapter.is_running

        await adapter.reconfigure(process_filter="NewApp")
        await asyncio.sleep(0.05)

        assert adapter.process_filter == "NewApp"
        # subsystem_filter unchanged (sentinel default)
        assert adapter.subsystem_filter == "com.old"
        assert adapter.entries_captured == 0
        assert adapter.is_running and adapter._error is None

        # The restart spawned the updated command.
        predicate = mock_exec.call_args_list[-1][0][-1]
        assert 'process == "NewApp"' in predicate
        assert 'subsystem == "com.old"' in predicate

        await adapter.stop()


@pytest.mark.asyncio
async def test_reconfigure_can_clear_filter():
    """reconfigure() with explicit None clears a filter."""
    adapter = SimulatorLogAdapter(
        udid=SAMPLE_UDID,
        process_filter="MyApp",
        subsystem_filter="com.myapp",
    )

    # Not running — reconfigure should just update filters without start/stop
    await adapter.reconfigure(process_filter=None, subsystem_filter=None)

    assert adapter.process_filter is None
    assert adapter.subsystem_filter is None
    cmd = adapter._build_command()
    assert "--predicate" not in cmd


@pytest.mark.asyncio
async def test_reconfigure_noop_when_stopped():
    """reconfigure() on a stopped adapter updates filters but doesn't start."""
    adapter = SimulatorLogAdapter(udid=SAMPLE_UDID, process_filter="Old")

    await adapter.reconfigure(process_filter="New")

    assert adapter.process_filter == "New"
    assert not adapter.is_running


def test_a_simulator_entry_names_its_sender(adapter: SimulatorLogAdapter, sample_lines: list[str]):
    entry = adapter._parse_json_line(sample_lines[0])
    assert entry.sender == "libsystem_trace.dylib" and entry.subsystem == "com.myapp.networking"

