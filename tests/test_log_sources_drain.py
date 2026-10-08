"""Stopping a log source keeps what its stream had already written.

Every source broke out of its read loop as soon as stop() cleared `_running`,
and cancelled the read task, so lines already in the pipe were dropped -- the
second half of the simulator bug fixed in #438, in every other source. They now
run to EOF, which terminating the subprocess produces, and stop() drains them.

Driven through real StreamReaders, not async iterators over a list: what is
under test is what the loop does with a pipe that is still open.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.sources.device_log import PhysicalDeviceLogAdapter
from server.sources.logcat import LogcatAdapter
from server.sources.oslog import OslogAdapter
from server.sources.syslog import SyslogAdapter

OSLOG_LINE = (
    b'{"eventType":"logEvent","eventMessage":"MESSAGE",'
    b'"timestamp":"2026-02-07 14:23:01.000000-0800","messageType":"Default",'
    b'"processImagePath":"/path/App","subsystem":"com.test","category":"test"}\n'
)
LOGCAT_LINE = b"2026-10-08 01:02:03.456 +0000  123  456 I Tag     : MESSAGE\n"
PLAIN_LINE = b"MESSAGE\n"


def _process(*, code: int = 0, stderr: bytes = b"", exits_on_its_own: bool = False):
    """A subprocess with real stdout and stderr streams. `wait()` blocks until
    it is terminated, as a live one does; terminating ends both streams."""
    out, err = asyncio.StreamReader(), asyncio.StreamReader()
    exited = asyncio.Event()
    proc = MagicMock()
    proc.returncode = None
    proc.stdout, proc.stderr = out, err

    def finish():
        for stream in (out, err):
            if not stream.at_eof():
                stream.feed_eof()
        exited.set()

    async def wait():
        await exited.wait()
        return code

    proc.terminate = MagicMock(side_effect=finish)
    proc.kill = MagicMock(side_effect=finish)
    proc.wait = AsyncMock(side_effect=wait)
    if stderr:
        err.feed_data(stderr)
    if exits_on_its_own:
        finish()
    return proc


async def _started(name: str, proc, on_entry):
    """Each adapter started against `proc`, past whatever it checks first."""
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        if name == "oslog":
            adapter = OslogAdapter(on_entry=on_entry)
        elif name == "syslog":
            adapter = SyslogAdapter(on_entry=on_entry)
        elif name == "logcat":
            adapter = LogcatAdapter(serial="emulator-5554", on_entry=on_entry)
            with patch("shutil.which", return_value="/usr/bin/adb"), \
                    patch.object(adapter, "_api_level", AsyncMock(return_value=33)), \
                    patch("server.sources.logcat.STARTUP_GRACE_S", 0.01):
                await adapter.start()
            return adapter
        else:
            adapter = PhysicalDeviceLogAdapter(udid="00008030-TEST", on_entry=on_entry)
            adapter._build_command = AsyncMock(return_value=["pymobiledevice3", "syslog", "live"])
        await adapter.start()
    return adapter


LINES = {"oslog": OSLOG_LINE, "syslog": PLAIN_LINE, "logcat": LOGCAT_LINE,
         "device_log": PLAIN_LINE}
SOURCES = sorted(LINES)


def _line(name: str, message: str) -> bytes:
    return LINES[name].replace(b"MESSAGE", message.encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SOURCES)
async def test_a_line_written_just_before_stop_is_kept(name):
    emitted = []

    async def on_entry(entry):
        emitted.append(entry.message)

    proc = _process()
    adapter = await _started(name, proc, on_entry)
    assert adapter.is_running, adapter._error
    await asyncio.sleep(0)
    # Written, and stop called, before the read loop runs again.
    proc.stdout.feed_data(_line(name, "logged just before stop"))
    await asyncio.wait_for(adapter.stop(), timeout=10)

    assert any("logged just before stop" in m for m in emitted), (name, emitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SOURCES)
async def test_output_still_arriving_after_terminate_is_kept(name):
    """The process has exited but its pipe still holds output the loop has
    not read. Above, the loop got its turn while stop() waited for the exit;
    here it only gets it if stop() actually drains."""
    emitted = []

    async def on_entry(entry):
        emitted.append(entry.message)

    proc = _process()
    adapter = await _started(name, proc, on_entry)
    out = proc.stdout

    async def trickle():
        for message in ("one", "two", "three"):
            out.feed_data(_line(name, f"late {message}"))
            await asyncio.sleep(0.02)
        out.feed_eof()

    pending = []

    def terminate():
        pending.append(asyncio.ensure_future(trickle()))
        exited.set()

    exited = asyncio.Event()

    async def wait():
        await exited.wait()
        return 0

    proc.terminate = MagicMock(side_effect=terminate)
    proc.wait = AsyncMock(side_effect=wait)
    await asyncio.wait_for(adapter.stop(), timeout=10)

    assert sum("late three" in m for m in emitted) == 1, (name, emitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SOURCES)
async def test_two_stops_at_once_both_return(name):
    adapter = await _started(name, _process(), AsyncMock())
    results = await asyncio.wait_for(
        asyncio.gather(adapter.stop(), adapter.stop(), return_exceptions=True), timeout=10)
    assert results == [None, None], name


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SOURCES)
async def test_a_stop_overlapping_a_restart_leaves_the_new_run_alone(name):
    """A filter change restarts a source while another stop may still be
    waiting on the old process; that stop must not clear the new run's
    handles."""
    release = asyncio.Event()
    old = _process()
    original_wait = old.wait.side_effect

    async def slow_wait():
        await release.wait()
        return await original_wait()

    old.wait = AsyncMock(side_effect=slow_wait)
    adapter = await _started(name, old, AsyncMock())
    stopping = asyncio.ensure_future(adapter.stop())
    await asyncio.sleep(0.05)

    new = _process()
    with patch("asyncio.create_subprocess_exec", return_value=new):
        if name == "logcat":
            with patch("shutil.which", return_value="/usr/bin/adb"), \
                    patch.object(adapter, "_api_level", AsyncMock(return_value=33)), \
                    patch("server.sources.logcat.STARTUP_GRACE_S", 0.01):
                await adapter.start()
        else:
            await adapter.start()
    new_task = adapter._read_task
    release.set()
    await asyncio.wait_for(stopping, timeout=10)

    assert adapter._process is new, name
    assert adapter._read_task is new_task, name
    await asyncio.wait_for(adapter.stop(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "tool"), [("oslog", "log stream"), ("syslog", "idevicesyslog")])
async def test_a_stream_that_ends_on_its_own_says_why(name, tool):
    """Read as a clean stop before: status "stopped", no error. (logcat and
    the physical-device source already said why.)"""
    proc = _process(code=1, stderr=b"No device found\n")
    adapter = await _started(name, proc, AsyncMock())
    proc.terminate()                    # the process ends without stop()
    for _ in range(50):
        if not adapter.is_running:
            break
        await asyncio.sleep(0.02)

    assert adapter.status().status == "error"
    assert f"{tool} exited (1)" in adapter._error
    assert "No device found" in adapter._error


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SOURCES)
async def test_a_failure_while_draining_is_logged(name, caplog):
    import logging

    async def on_entry(_entry):
        raise RuntimeError("downstream broke")

    proc = _process()
    adapter = await _started(name, proc, on_entry)
    await asyncio.sleep(0)
    proc.stdout.feed_data(_line(name, "late"))
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(adapter.stop(), timeout=10)

    assert any("read loop failed" in r.getMessage() for r in caplog.records), name


# -- the proxy ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_proxy_dispatches_an_event_written_just_before_stop():
    """A flow that completed as the proxy stopped was cancelled away."""
    from server.sources import proxy as proxy_mod
    from server.sources.proxy import ProxyAdapter

    proc = _process()
    adapter = ProxyAdapter()
    dispatched = []

    async def dispatch(data):
        dispatched.append(data)

    adapter._dispatch = dispatch
    adapter._process = proc
    adapter._running = True
    with patch.object(proxy_mod, "update_state"):
        adapter._read_task = asyncio.create_task(adapter._read_loop())
        await asyncio.sleep(0)
        proc.stdout.feed_data(b'{"type": "flow", "id": "late"}\n')
        await asyncio.wait_for(adapter.stop(), timeout=10)

    assert {"type": "flow", "id": "late"} in dispatched
