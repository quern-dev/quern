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

from pathlib import Path

import pytest

from server.models import LogLevel, LogSource
from server.sources.device_log import PMD3_SYSLOG_PATTERN, PhysicalDeviceLogAdapter

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


def test_a_json_line_carries_subsystem_category_and_sender(adapter):
    entry = adapter._parse_line(JSON_LINE)
    assert entry.subsystem == "com.apple.CFNetwork" and entry.category == "Default"
    assert entry.sender == "CFNetwork"
    assert entry.process == "Geocaching" and entry.pid == 7556
    assert entry.level == LogLevel.ERROR
    assert entry.message.startswith("Connection 13: default TLS Trust")
    assert entry.source == LogSource.DEVICE and entry.raw == JSON_LINE


def test_a_json_timestamp_is_host_local_like_the_text_one(adapter):
    """Both forms come from `datetime.fromtimestamp()`, naive local time."""
    from datetime import datetime

    from server.sources.device_log import host_local_to_utc
    entry = adapter._parse_line(JSON_LINE)
    assert entry.timestamp == host_local_to_utc(datetime(2026, 10, 2, 16, 15, 7, 210223))


def test_a_json_line_without_a_label_has_no_subsystem(adapter):
    """About 3% of device lines carry none; the library is all they have."""
    import json
    d = json.loads(JSON_LINE)
    d["label"] = None
    entry = adapter._parse_line(json.dumps(d))
    assert entry.subsystem == "" and entry.category == "" and entry.sender == "CFNetwork"


@pytest.mark.parametrize("line", ['{"not": "a log line"}', '{"message": ', "[1, 2]", "{}"])
def test_a_line_that_is_not_json_log_is_never_dropped(adapter, line):
    entry = adapter._parse_line(line)
    assert entry is not None and entry.raw == line


async def test_json_is_asked_for_only_when_offered(monkeypatch, adapter):
    from server.sources import device_log
    helps = {"new": b"  --format   Output format. 'json' emits one JSON object",
             "old": b"  --match  filter only logs matching"}

    class Proc:
        def __init__(self, out): self.out = out
        async def communicate(self): return self.out, b""

    async def spawn(binary, *args, **kw):
        return Proc(helps[binary])
    monkeypatch.setattr(device_log.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(device_log, "_JSON_SUPPORT", {})
    assert await device_log._supports_json("new") is True
    assert await device_log._supports_json("old") is False

    async def no_tunnel(udid): return None
    monkeypatch.setattr(device_log, "resolve_tunnel_udid", no_tunnel)
    monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: "new")
    assert (await adapter._build_command())[-2:] == ["--format", "json"]
    monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: "old")
    assert "--format" not in await adapter._build_command()


async def test_help_that_cannot_be_read_falls_back_and_is_asked_again(monkeypatch):
    from server.sources import device_log
    monkeypatch.setattr(device_log, "_JSON_SUPPORT", {})

    async def broken(*a, **kw):
        raise OSError("no such file")
    monkeypatch.setattr(device_log.asyncio, "create_subprocess_exec", broken)
    assert await device_log._supports_json("pmd3") is False
    assert "pmd3" not in device_log._JSON_SUPPORT, "a failure to ask is not an answer"


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
    assert seen["limit"] >= 1024 * 1024

