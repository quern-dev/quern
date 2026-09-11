"""Tests for `quern doctor` — what it reports when it cannot reach the server.

The point of these: only the device-tool section needs a running server, so
everything else must still be reported when there isn't one. A doctor that goes
silent when the patient is sick is no use.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from server.main import _cmd_doctor


@pytest.fixture
def stub_sections(monkeypatch):
    """Stand in for the three server-independent sections, which shell out."""
    monkeypatch.setattr("server.main._report_python_deps", lambda fix: print("PYDEPS"))
    monkeypatch.setattr("server.main._report_external_tools", lambda fix: print("EXTERNAL"))
    monkeypatch.setattr("server.main._report_service_health", lambda fix: print("SERVICES"))


def _run(args_fix: bool = False) -> int:
    with pytest.raises(SystemExit) as exc:
        _cmd_doctor(argparse.Namespace(fix=args_fix))
    return exc.value.code


class TestDoctorWithoutAServer:
    def test_reports_everything_else_when_no_server_is_running(self, stub_sections, capsys):
        with patch("server.main.read_state", return_value=None):
            code = _run()
        out = capsys.readouterr().out
        assert "not checked" in out
        assert "no server running" in out
        for section in ("PYDEPS", "EXTERNAL", "SERVICES"):
            assert section in out, f"{section} was withheld for want of a server"
        assert code != 0, "a check that never ran must not read as a pass"

    def test_an_unhealthy_server_is_reported_differently_from_a_missing_one(
        self, stub_sections, capsys
    ):
        with (
            patch("server.main.read_state", return_value={"server_port": 9100}),
            patch("server.main.is_server_healthy", return_value=False),
        ):
            code = _run()
        out = capsys.readouterr().out
        assert "not responding on /health" in out
        assert "no server running" not in out
        assert "PYDEPS" in out
        assert code != 0

    def test_a_server_that_will_not_answer_tools_is_its_own_reason(self, stub_sections, capsys):
        with (
            patch("server.main.read_state", return_value={"server_port": 9100}),
            patch("server.main.is_server_healthy", return_value=True),
            patch("server.main.fetch_tools", return_value=None),
        ):
            code = _run()
        out = capsys.readouterr().out
        assert "did not answer /tools" in out
        assert "PYDEPS" in out
        assert code != 0


class TestDoctorWithAServer:
    def test_lists_the_tools_and_exits_clean(self, stub_sections, capsys):
        with (
            patch("server.main.read_state", return_value={"server_port": 9100}),
            patch("server.main.is_server_healthy", return_value=True),
            patch("server.main.fetch_tools", return_value={"tools": {"adb": True, "idb": False}}),
        ):
            code = _run()
        out = capsys.readouterr().out
        assert "✓ adb" in out
        assert "✗ idb" in out
        assert "not checked" not in out
        assert code == 0

    def test_no_tools_reported_is_not_the_same_as_not_checked(self, stub_sections, capsys):
        """`{}` means the server answered and has no device controller. `None`
        means nobody could be asked. Reporting the first as the second would
        blame a missing server for a present one's empty answer."""
        with (
            patch("server.main.read_state", return_value={"server_port": 9100}),
            patch("server.main.is_server_healthy", return_value=True),
            patch("server.main.fetch_tools", return_value={"tools": {}}),
        ):
            code = _run()
        out = capsys.readouterr().out
        assert "none reported" in out
        assert "not checked" not in out
        assert code == 0, "the server answered; that is not a skipped check"

    def test_service_health_is_reported_even_with_no_device_controller(
        self, stub_sections, capsys
    ):
        """It probes tunneld and the system extension, neither of which is the
        quern server, so an empty tool list is no reason to skip it."""
        with (
            patch("server.main.read_state", return_value={"server_port": 9100}),
            patch("server.main.is_server_healthy", return_value=True),
            patch("server.main.fetch_tools", return_value={"tools": {}}),
        ):
            _run()
        assert "SERVICES" in capsys.readouterr().out
