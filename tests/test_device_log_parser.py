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
    assert entry.subsystem == "Foundation"
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


def test_a_subsystem_is_optional(adapter):
    with_sub = adapter._parse_line(
        "2026-09-02 07:14:02.100000 QuernProbe{com.quern.probe}[4410] <NOTICE>: x"
    )
    without = adapter._parse_line("2026-09-02 07:14:02.100000 QuernProbe[4410] <NOTICE>: x")
    assert with_sub.subsystem == "com.quern.probe"
    assert without.subsystem == ""
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
