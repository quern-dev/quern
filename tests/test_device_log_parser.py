"""Parser tests for physical-device syslog lines.

`device_log.py` documents its regex against a sample line from LogTester —
an app that lived outside version control for months while this parser shipped
against it. Nothing exercised the parser at all: no test in the suite referenced
it before this file.

The fixture is checked in so these run without a device. `tools/probe-app`'s
Logs tab emits the same set of shapes, so a live capture and this fixture stay
comparable, and the probe can regenerate the fixture when the format moves.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from server.models import LogLevel, LogSource
from server.sources.device_log import (
    PMD3_SYSLOG_PATTERN,
    PhysicalDeviceLogAdapter,
    host_local_to_utc,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pmd3_syslog_quernprobe.txt"


@pytest.fixture
def adapter() -> PhysicalDeviceLogAdapter:
    return PhysicalDeviceLogAdapter(udid="TESTUDID0000", device_id="test-device")


def lines() -> list[str]:
    return [ln for ln in FIXTURE.read_text().splitlines() if ln.strip()]


def test_the_documented_sample_line_still_parses(adapter):
    """The exact line device_log.py's docstring is written against."""
    entry = adapter._parse_line(
        "2026-02-21 21:22:45.272141 LogTester{Foundation}[2915] <NOTICE>: message text"
    )
    assert entry.process == "LogTester"
    # The braces hold the sending library, not an os_log subsystem.
    assert entry.sender == "Foundation" and entry.subsystem == ""
    assert entry.pid == 2915
    assert entry.level == LogLevel.NOTICE
    assert entry.message == "message text"
    assert entry.timestamp.year == 2026


def test_every_probe_log_shape_parses(adapter):
    """No line from the probe's Logs tab may fall through to the raw fallback.

    An unmatched line is not dropped — it becomes an INFO entry whose message is
    the whole raw line, process unset. That degrades quietly: logs still appear,
    so nobody notices the process and level have stopped being populated.
    """
    unmatched = [ln for ln in lines() if not PMD3_SYSLOG_PATTERN.match(ln)]
    assert unmatched == ["this line has no timestamp and should still surface"], (
        f"unexpectedly unmatched: {unmatched}"
    )


@pytest.mark.parametrize(
    ("level_token", "expected"),
    [
        ("<DEBUG>", LogLevel.DEBUG),
        ("<INFO>", LogLevel.INFO),
        ("<NOTICE>", LogLevel.NOTICE),
        ("<WARNING>", LogLevel.WARNING),
        ("<ERROR>", LogLevel.ERROR),
        ("<FAULT>", LogLevel.FAULT),
    ],
)
def test_each_level_token_maps(adapter, level_token, expected):
    line = f"2026-09-02 07:14:02.100000 QuernProbe[4410] {level_token}: body"
    assert adapter._parse_line(line).level == expected


def test_an_unknown_level_does_not_crash_the_reader(adapter):
    """A source adapter must never take the server down over one odd line."""
    entry = adapter._parse_line(
        "2026-09-02 07:14:02.100000 QuernProbe[4410] <MADEUP>: body"
    )
    assert entry.level == LogLevel.INFO
    assert entry.message == "body"


def test_a_sender_is_optional(adapter):
    with_sub = adapter._parse_line(
        "2026-09-02 07:14:02.100000 QuernProbe{com.quern.probe}[4410] <NOTICE>: x"
    )
    without = adapter._parse_line("2026-09-02 07:14:02.100000 QuernProbe[4410] <NOTICE>: x")
    assert with_sub.sender == "com.quern.probe"
    assert without.sender == "" and without.subsystem == ""
    assert with_sub.process == without.process == "QuernProbe"


def test_braces_in_the_message_are_not_read_as_a_subsystem(adapter):
    """The subsystem group is optional and non-greedy, so a message containing
    braces is the case most likely to be mis-split."""
    entry = adapter._parse_line(
        "2026-09-02 07:14:02.100000 QuernProbe[4410] <NOTICE>: has {braces} inline"
    )
    assert entry.subsystem == ""
    assert entry.message == "has {braces} inline"


def test_an_empty_message_is_preserved(adapter):
    entry = adapter._parse_line("2026-09-02 07:14:02.100000 QuernProbe[4410] <NOTICE>:")
    assert entry.message == ""
    assert entry.process == "QuernProbe"


def test_an_unparseable_line_is_surfaced_rather_than_dropped(adapter):
    entry = adapter._parse_line("total gibberish with no structure")
    assert entry.message == "total gibberish with no structure"
    assert entry.level == LogLevel.INFO
    assert entry.source == LogSource.DEVICE


def test_timestamps_are_timezone_aware(adapter):
    """A naive datetime here compares wrongly against every other source."""
    entry = adapter._parse_line("2026-09-02 07:14:02.100000 QuernProbe[4410] <NOTICE>: x")
    assert entry.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# --format json: the real os_log subsystem, with the library beside it
# ---------------------------------------------------------------------------

#: A line as pymobiledevice3 11.19.4 writes it with `--format json`.
JSON_LINE = (
    '{"pid": 7556, "procid": 7556, "thread_id": 2801165, '
    '"timestamp": "2026-10-02T16:15:07.210223", "level": "ERROR", '
    '"image_name": "/System/Library/Frameworks/CFNetwork.framework/CFNetwork", '
    '"image_offset": 56968, "image_uuid": null, "process_image_uuid": null, '
    '"filename": "/private/var/containers/Bundle/Application/F44/Geocaching.app/Geocaching", '
    '"mach_timestamp": 1, "message": "Connection 13: default TLS Trust evaluation failed(-9807)", '
    '"label": {"subsystem": "com.apple.CFNetwork", "category": "Default"}}'
)


@pytest.fixture
def json_adapter() -> PhysicalDeviceLogAdapter:
    a = PhysicalDeviceLogAdapter(udid="TESTUDID0000", device_id="test-device")
    a.output_format = "json"
    return a


def _with(**changes) -> str:
    d = json.loads(JSON_LINE)
    d.update(changes)
    return json.dumps(d)


def test_a_json_line_carries_subsystem_category_and_sender(json_adapter):
    entry = json_adapter._parse_line(JSON_LINE)
    assert entry.subsystem == "com.apple.CFNetwork" and entry.category == "Default"
    assert entry.sender == "CFNetwork"
    assert entry.process == "Geocaching" and entry.pid == 7556
    assert entry.level == LogLevel.ERROR
    assert entry.message.startswith("Connection 13: default TLS Trust")
    assert entry.source == LogSource.DEVICE


def test_raw_is_the_text_form_not_the_json_object(json_adapter):
    """The object's UUIDs, offsets and paths doubled every log query's size."""
    entry = json_adapter._parse_line(JSON_LINE)
    assert entry.raw == ("2026-10-02T16:15:07.210223 Geocaching{CFNetwork}[7556] <ERROR>: "
                         "Connection 13: default TLS Trust evaluation failed(-9807) "
                         "[com.apple.CFNetwork][Default]")
    assert len(entry.raw) < len(JSON_LINE) / 2


def test_a_json_timestamp_is_host_local_like_the_text_one(json_adapter):
    """Both forms come from `datetime.fromtimestamp()`, naive local time."""
    entry = json_adapter._parse_line(JSON_LINE)
    assert entry.timestamp == host_local_to_utc(datetime(2026, 10, 2, 16, 15, 7, 210223))


def test_a_json_line_without_a_label_has_no_subsystem(json_adapter):
    """About 3% of device lines carry none; the library is all they have."""
    entry = json_adapter._parse_line(_with(label=None))
    assert entry.subsystem == "" and entry.category == "" and entry.sender == "CFNetwork"
    assert entry.raw.endswith("evaluation failed(-9807)"), "no empty label in raw"


@pytest.mark.parametrize("changes, check", [
    ({"pid": "7556"}, lambda e: e.pid is None),
    ({"level": "USER_ACTION"}, lambda e: e.level == LogLevel.INFO),
    ({"level": None}, lambda e: e.level == LogLevel.INFO),
    ({"message": 404}, lambda e: e.message == "404"),
    ({"timestamp": "not a time"}, lambda e: e.subsystem == "com.apple.CFNetwork"),
    ({"filename": ""}, lambda e: e.process == ""),
])
def test_odd_values_are_read_not_trusted(json_adapter, changes, check):
    entry = json_adapter._parse_line(_with(**changes))
    assert check(entry), entry


@pytest.mark.parametrize("line", [
    '{"not": "a log line"}',
    '{"message": "Invalid token", "code": 401}',
    '{"message": ',
    "[1, 2]",
    "{}",
    _with(label="not a dict"),
])
def test_a_line_that_is_not_a_json_log_line_is_kept_whole(json_adapter, line):
    entry = json_adapter._parse_line(line)
    assert entry is not None and entry.message == line and entry.raw == line


def test_a_text_capture_never_reads_a_line_as_json(adapter):
    """A multi-line message's later lines come on their own, and one can be
    an API error body: it is a line of text, kept whole (review)."""
    assert adapter.output_format is None
    entry = adapter._parse_line(JSON_LINE)
    assert entry.message == JSON_LINE and entry.subsystem == ""


class _Proc:
    def __init__(self, out=b"", code=0, hang=False):
        self.out, self.returncode, self.hang = out, code, hang
        self.killed = False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self.out, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _spawner(monkeypatch, outcomes):
    from server.sources import device_log
    calls = []

    async def spawn(binary, *args, **kw):
        calls.append(kw.get("env") or {})
        return outcomes[binary] if not callable(outcomes[binary]) else outcomes[binary]()
    monkeypatch.setattr(device_log.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(device_log, "_JSON_SUPPORT", {})
    return calls


HELP_NEW = b"  --format   Output format. 'json' emits one JSON object per line"


async def test_json_is_asked_for_only_when_offered(monkeypatch, adapter):
    from server.sources import device_log
    _spawner(monkeypatch, {"new": _Proc(HELP_NEW),
                           "old": _Proc(b"  --match  filter only logs matching"),
                           "fmt-only": _Proc(b"  --format   text output"),
                           "json-only": _Proc(b"  --json-out   write json")})
    assert await device_log._supports_json("new") is True
    for binary in ("old", "fmt-only", "json-only"):
        assert await device_log._supports_json(binary) is False, binary

    async def no_tunnel(udid): return None
    monkeypatch.setattr(device_log, "resolve_tunnel_udid", no_tunnel)
    monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: "new")
    assert (await adapter._build_command())[-2:] == ["--format", "json"]
    assert adapter.output_format == "json" and adapter.status().note is None
    monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: "old")
    assert "--format" not in await adapter._build_command()
    assert adapter.output_format == "text"
    assert "device-quiet" in adapter.status().note, "said where a caller looks"


async def test_the_help_is_read_plainly(monkeypatch):
    """FORCE_COLOR made rich split `--format` with colour codes, and the
    probe read a JSON-capable pymobiledevice3 as text-only (review)."""
    from server.sources import device_log
    coloured = b"\x1b[1;36m-\x1b[0m\x1b[1;36m-format\x1b[0m  Output format. 'json'"
    calls = _spawner(monkeypatch, {"pmd3": _Proc(coloured)})
    assert await device_log._supports_json("pmd3") is True
    assert calls[0]["NO_COLOR"] == "1" and calls[0]["COLUMNS"] == "200"


async def test_asked_once_until_the_binary_changes(monkeypatch, tmp_path):
    from server.sources import device_log
    binary = tmp_path / "pmd3"
    binary.write_text("#!/bin/sh\n")
    spawned = []

    def proc():
        spawned.append(1)
        return _Proc(HELP_NEW)
    _spawner(monkeypatch, {str(binary): proc})
    assert await device_log._supports_json(str(binary)) is True
    assert await device_log._supports_json(str(binary)) is True
    assert len(spawned) == 1, "asked once"
    import os
    st = binary.stat()
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))      # upgraded
    await device_log._supports_json(str(binary))
    assert len(spawned) == 2, "an upgrade is asked again"


@pytest.mark.parametrize("outcome", [
    _Proc(b"Traceback (most recent call last): ...", code=1),
    "oserror",
    "timeout",
])
async def test_a_help_that_cannot_be_read_is_no_for_now_and_not_kept(monkeypatch, outcome):
    from server.sources import device_log
    hung = _Proc(hang=True)
    if outcome == "oserror":
        def raise_oserror():
            raise OSError("no such file")
        target = raise_oserror
    elif outcome == "timeout":
        monkeypatch.setattr(device_log, "PROBE_TIMEOUT", 0.05)
        target = hung
    else:
        target = outcome
    _spawner(monkeypatch, {"pmd3": target})
    assert await device_log._supports_json("pmd3") is False
    assert device_log._JSON_SUPPORT == {}, "a failure to ask is not an answer"
    if outcome == "timeout":
        assert hung.killed


async def test_a_long_line_does_not_end_the_capture(monkeypatch, adapter):
    """asyncio's 64 KiB default ends the read loop on one long message."""
    from server.sources import device_log
    seen = {}

    async def spawn(*cmd, **kw):
        seen.update(kw)
        raise OSError("stop here")
    monkeypatch.setattr(device_log.asyncio, "create_subprocess_exec", spawn)

    async def cmd():
        return ["pmd3", "syslog", "live"]
    monkeypatch.setattr(adapter, "_build_command", cmd)
    await adapter.start()
    assert seen["limit"] == device_log._LINE_LIMIT == 4 * 1024 * 1024


class _Stream:
    def __init__(self, lines=()):
        self.lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def readline(self):
        await asyncio.sleep(0)
        return self.lines.pop(0) if self.lines else b""


class _Exited:
    def __init__(self, code, stderr_lines, exits=True):
        self.returncode = code
        self.stdout = _Stream()
        self.stderr = _Stream(stderr_lines)
        self.exits = exits

    async def wait(self):
        if not self.exits:
            await asyncio.sleep(3600)
        return self.returncode


async def _run(adapter, proc):
    adapter._process = proc
    adapter._running = True
    adapter._stderr_task = asyncio.create_task(adapter._drain_stderr())
    await adapter._read_loop()


async def test_a_capture_that_exits_by_itself_says_why(adapter):
    """A downgraded pymobiledevice3 rejects `--format` at once; the capture
    must not just stop (review)."""
    await _run(adapter, _Exited(2, [b"Usage: pymobiledevice3 syslog live [OPTIONS]\n",
                                    b"Error: No such option: --format\n"]))
    assert adapter.status().status == "error"
    assert "exited (2)" in adapter._error and "No such option: --format" in adapter._error


async def test_stderr_is_drained_and_its_last_lines_kept(adapter):
    """Unread, a full stderr pipe stalls the capture; read once at the end,
    only its first 4 KiB were seen (CodeRabbit)."""
    lines = [f"warning {i}\n".encode() for i in range(500)] + [b"fatal: tunnel lost\n"]
    await _run(adapter, _Exited(1, lines))
    assert "fatal: tunnel lost" in adapter._error and "warning 0" not in adapter._error
    assert len(adapter._stderr_tail) <= 20


async def test_output_that_closes_without_an_exit_is_said_as_such(adapter, monkeypatch):
    from server.sources import device_log
    real_wait_for = device_log.asyncio.wait_for

    async def quick(aw, timeout):
        return await real_wait_for(aw, min(timeout, 0.05))
    monkeypatch.setattr(device_log.asyncio, "wait_for", quick)
    await _run(adapter, _Exited(None, [], exits=False))
    assert "closed its output but has not exited" in adapter._error
    assert "None" not in adapter._error
