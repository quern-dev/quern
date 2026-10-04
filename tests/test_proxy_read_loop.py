"""The proxy's read loop survives what mitmdump writes, and never leaves it half-alive.

A CI run lost its proxy twice: mitmproxy logged an error with its traceback to
stdout -- `--quiet` keeps errors -- and one traceback line was only a string
literal, which `json.loads` returns as a str. `data.get("type")` raised, the
exception ended `_read_loop`, and mitmdump kept running with nobody reading it.
Local capture went on routing the simulator into it, the pipe filled, and every
request failed until the proxy was restarted by hand. Restarted, it still
reported the old error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import textwrap
import time
from unittest.mock import MagicMock, patch

import pytest

from server.sources import proxy as proxy_mod
from server.sources.proxy import ProxyAdapter


def _rejection(sni: str) -> bytes:
    return json.dumps({
        "type": "tls_rejected", "sni": sni, "client_ip": "10.0.0.1",
        "error": "tlsv1 alert unknown ca", "timestamp": time.time(),
    }).encode() + b"\n"


class _Lines:
    """mitmdump's output as the loop sees it without a pipe: an async iterable."""

    def __init__(self, *lines: bytes):
        self.lines = lines

    def __aiter__(self):
        async def gen():
            for line in self.lines:
                yield line
        return gen()


def _adapter_reading(*lines: bytes) -> ProxyAdapter:
    a = ProxyAdapter()
    proc = MagicMock()
    proc.stdout = _Lines(*lines)
    proc.returncode = 0
    a._process = proc
    a._running = True
    return a


@pytest.fixture(autouse=True)
def _no_state_file():
    """The loop records an unexpected stop in state.json; not the real one."""
    with patch.object(proxy_mod, "update_state"):
        yield


class TestALineCannotEndTheLoop:
    async def test_the_reported_sequence(self, caplog):
        """The report's own reproduction: values that are JSON but not objects,
        then an event, then garbage, then another event."""
        a = _adapter_reading(
            b'"abc"\n', b"123\n", b"null\n", b"[1, 2]\n",
            _rejection("one.example"),
            b"not json at all\n",
            _rejection("two.example"),
        )
        with caplog.at_level(logging.WARNING, logger="server.sources.proxy"):
            await a._read_loop()

        assert [r.sni for r in a._tls_rejections] == ["one.example", "two.example"], (
            "an event after a bad line was lost: the loop stopped at it"
        )
        assert a._error is None or "Read loop error" not in a._error
        logged = caplog.text
        for raw in ('"abc"', "123", "null", "not json at all"):
            assert raw in logged, f"the bad line {raw} was not quoted in the log"

    async def test_a_traceback_from_mitmproxy(self):
        """What actually reached the loop: mitmproxy's error log, on stdout."""
        traceback_lines = [
            b"[14:21:39.052] Addon error: boom\n",
            b"Traceback (most recent call last):\n",
            b'  File "x.py", line 5, in request\n',
            b'        "addon blew up while handling "\n',
            b'        "a request"\n',
            b"ValueError: boom\n",
        ]
        a = _adapter_reading(*traceback_lines, _rejection("after.example"))
        await a._read_loop()
        assert [r.sni for r in a._tls_rejections] == ["after.example"]

    async def test_a_handler_that_raises_costs_its_event_only(self, caplog):
        a = _adapter_reading(_rejection("bad.example"), _rejection("good.example"))
        real = a._handle_tls_rejected

        def flaky(data):
            if data["sni"] == "bad.example":
                raise RuntimeError("handler bug")
            real(data)

        a._handle_tls_rejected = flaky
        with caplog.at_level(logging.ERROR, logger="server.sources.proxy"):
            await a._read_loop()
        assert [r.sni for r in a._tls_rejections] == ["good.example"]
        assert "bad.example" in caplog.text, "the failing event was not named"

    async def test_an_oversized_line_is_skipped_not_fatal(self):
        """StreamReader raises for a line past its limit. An `async for` over it
        ended the loop -- the same death by another road (see the 1.95 MB
        alert in test_addon_intercept)."""
        reader = asyncio.StreamReader(limit=1024)
        reader.feed_data(b'{"type": "flow", "pad": "' + b"x" * 5000 + b'"}\n')
        reader.feed_data(_rejection("after.example"))
        reader.feed_eof()

        a = ProxyAdapter()
        proc = MagicMock()
        proc.returncode = 0
        a._process = proc
        a._events = reader
        a._running = True
        await a._read_loop()
        assert [r.sni for r in a._tls_rejections] == ["after.example"]

    async def test_the_warnings_are_bounded(self, caplog):
        a = _adapter_reading(*([b'"spew"\n'] * 500))
        with caplog.at_level(logging.WARNING, logger="server.sources.proxy"):
            await a._read_loop()
        quoted = [r for r in caplog.records if "spew" in r.getMessage()]
        assert proxy_mod.BAD_LINES_LOGGED <= len(quoted) < 40


class TestNeverHalfAlive:
    async def test_mitmdump_is_stopped_when_the_loop_ends_under_it(self):
        """Its events ended while it still ran: nobody would read it again, and
        local capture would hang the apps it routes. So it is stopped."""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
        )
        try:
            reader = asyncio.StreamReader()
            reader.feed_eof()
            a = ProxyAdapter()
            a._process = process
            a._events = reader
            a._running = True
            await a._read_loop()

            assert process.returncode is not None, "mitmdump was left running unread"
            assert a.status().status == "error"
            assert a._error
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_mitmdump_exiting_on_its_own_is_an_error_not_stopped(self):
        a = _adapter_reading()
        a._process.returncode = 1
        await a._read_loop()
        assert a.status().status == "error"
        assert "code 1" in a._error

    async def test_stop_is_not_reported_as_a_failure(self):
        a = _adapter_reading(_rejection("x.example"))
        a._running = False   # what stop() sets before the loop sees the next line
        await a._read_loop()
        assert a._error is None


FAKE_MITMDUMP = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    # mitmproxy's logger, on stdout: a traceback line that is valid JSON.
    print('"addon blew up while handling "', flush=True)
    fd = int(os.environ["QUERN_EVENT_FD"])
    os.write(fd, (json.dumps({{"type": "tls_rejected", "sni": "piped.example",
                               "client_ip": "10.0.0.1", "error": "unknown ca",
                               "timestamp": time.time()}}) + "\\n").encode())
    time.sleep(60)
""")


class TestTheEventPipe:
    async def test_events_arrive_on_their_own_pipe_and_stdout_is_only_logged(
        self, tmp_path, caplog,
    ):
        """End to end through `start()`, with a stand-in mitmdump that writes an
        event to the descriptor it was given and a JSON string to stdout."""
        fake = tmp_path / "mitmdump"
        fake.write_text(FAKE_MITMDUMP.format(python=sys.executable))
        fake.chmod(0o755)

        a = ProxyAdapter(listen_port=1)
        a._error = "Read loop error: 'str' object has no attribute 'get'"
        with (
            patch.object(a, "_find_mitmdump", return_value=str(fake)),
            patch.object(a, "_kill_stale_mitmdump"),
            caplog.at_level(logging.WARNING, logger="server.sources.proxy"),
        ):
            await a.start()
            try:
                assert a._error is None, "a successful start kept the last run's error"
                assert a.started_at is not None
                for _ in range(100):
                    if a._tls_rejections:
                        break
                    await asyncio.sleep(0.05)
                assert [r.sni for r in a._tls_rejections] == ["piped.example"], (
                    "the event written to QUERN_EVENT_FD never arrived"
                )
                for _ in range(100):
                    if "addon blew up" in caplog.text:
                        break
                    await asyncio.sleep(0.05)
                assert "addon blew up" in caplog.text, "mitmdump's stdout was not logged"
                assert a.status().status == "proxying"
            finally:
                await a.stop()
        assert a._process is None


class TestTheAddonWriter:
    def test_it_writes_to_the_descriptor_it_was_given(self, monkeypatch):
        from server.proxy import addon

        read_fd, write_fd = os.pipe()
        try:
            monkeypatch.setenv(addon.EVENT_FD_ENV, str(write_fd))
            monkeypatch.setattr(addon, "_event_fd", None)
            stdout_writes = []
            monkeypatch.setattr(addon.sys, "stdout", MagicMock())
            addon.sys.stdout.buffer.write.side_effect = stdout_writes.append

            addon._write_json({"type": "status", "event": "started"})

            assert json.loads(os.read(read_fd, 4096)) == {"type": "status", "event": "started"}
            assert stdout_writes == [], "the event went to stdout as well"
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_without_one_it_falls_back_to_stdout(self, monkeypatch):
        """An older server, still running across an update, starts this addon
        without a descriptor; its events must still arrive."""
        from server.proxy import addon

        monkeypatch.delenv(addon.EVENT_FD_ENV, raising=False)
        monkeypatch.setattr(addon, "_event_fd", None)
        monkeypatch.setattr(addon.sys, "stdout", MagicMock())
        addon._write_json({"type": "status", "event": "started"})
        (written,), _ = addon.sys.stdout.buffer.write.call_args
        assert json.loads(written) == {"type": "status", "event": "started"}
