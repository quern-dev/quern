"""Tests for the idevicesyslog line parser."""

import pytest

from server.models import LogLevel, LogSource
from server.sources.syslog import SyslogAdapter


@pytest.fixture
def adapter() -> SyslogAdapter:
    return SyslogAdapter(device_id="test-device")


def test_parse_standard_line(adapter: SyslogAdapter):
    line = 'Feb  7 14:23:01 iPhone MyApp(CoreFoundation)[1234] <Notice>: viewDidLoad called'
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.process == "MyApp"
    assert entry.subsystem == "CoreFoundation"
    assert entry.pid == 1234
    assert entry.level == LogLevel.NOTICE
    assert entry.message == "viewDidLoad called"
    assert entry.source == LogSource.SYSLOG
    assert entry.device_id == "test-device"


def test_parse_line_without_subsystem(adapter: SyslogAdapter):
    line = 'Feb  7 14:23:01 iPhone MyApp[1234] <Error>: something failed'
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.process == "MyApp"
    assert entry.subsystem == ""
    assert entry.level == LogLevel.ERROR
    assert entry.message == "something failed"


def test_parse_abbreviated_level(adapter: SyslogAdapter):
    line = 'Feb  7 14:23:01 iPhone MyApp[1234] <e>: error message'
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.level == LogLevel.ERROR


def test_parse_unparseable_line(adapter: SyslogAdapter):
    line = "some garbage that doesn't match the pattern"
    entry = adapter._parse_line(line)

    # Should still capture it as a raw entry
    assert entry is not None
    assert entry.level == LogLevel.INFO
    assert entry.message == line
    assert entry.raw == line


def test_parse_empty_message(adapter: SyslogAdapter):
    line = 'Feb  7 14:23:01 iPhone MyApp[1234] <Info>: '
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.message == ""
    assert entry.level == LogLevel.INFO


# ---------------------------------------------------------------------------
# Lines without device name (some idevicesyslog versions omit it)
# ---------------------------------------------------------------------------


def test_parse_no_device_with_subsystem(adapter: SyslogAdapter):
    line = 'Feb  9 20:26:11 backboardd(CoreBrightness)[72] <Notice>: Ammolite: Lux 475.7'
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.process == "backboardd"
    assert entry.subsystem == "CoreBrightness"
    assert entry.pid == 72
    assert entry.level == LogLevel.NOTICE
    assert entry.message == "Ammolite: Lux 475.7"


def test_parse_no_device_without_subsystem(adapter: SyslogAdapter):
    line = 'Feb  9 20:26:12 powerexperienced[125] <Notice>: Evaluating power mode'
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.process == "powerexperienced"
    assert entry.subsystem == ""
    assert entry.pid == 125
    assert entry.level == LogLevel.NOTICE
    assert entry.message == "Evaluating power mode"


def test_parse_no_device_error(adapter: SyslogAdapter):
    line = 'Feb  9 20:26:12 locationd[78] <Error>: GPS signal lost'
    entry = adapter._parse_line(line)

    assert entry is not None
    assert entry.process == "locationd"
    assert entry.level == LogLevel.ERROR
    assert entry.message == "GPS signal lost"
