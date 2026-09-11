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
    # True = both probes completed, which is what the real one returns when it
    # could look. Sections signal "could not check", not "found nothing wrong".
    def services(fix):
        print("SERVICES")
        return True

    monkeypatch.setattr("server.main._report_service_health", services)


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


class TestDoctorExitContract:
    """Two contracts, because `--fix` is an action and plain doctor is a report."""

    def _server(self, monkeypatch, tools=None):
        monkeypatch.setattr("server.main.read_state", lambda: {"server_port": 9100})
        monkeypatch.setattr("server.main.is_server_healthy", lambda port: True)
        monkeypatch.setattr(
            "server.main.fetch_tools", lambda port: {"tools": tools if tools is not None else {}}
        )

    def test_a_check_that_ran_and_found_a_problem_still_exits_clean(
        self, stub_sections, monkeypatch, capsys
    ):
        """A stale venv is doctor working, not doctor failing. The finding is in
        the output; the status says whether doctor could look."""
        self._server(monkeypatch, {"adb": False})
        assert _run() == 0

    def test_a_service_probe_that_threw_reaches_the_exit_code(self, monkeypatch, capsys):
        """The counterpart: "could not be checked" is doctor failing to look,
        and it must not read the same as a clean pass."""
        monkeypatch.setattr("server.main._report_python_deps", lambda fix: None)
        monkeypatch.setattr("server.main._report_external_tools", lambda fix: None)
        monkeypatch.setattr("server.main._report_service_health", lambda fix: False)
        self._server(monkeypatch, {"adb": True})
        assert _run() != 0

    def test_fix_reports_the_repair_not_the_diagnostics(self, monkeypatch, capsys):
        """`quern doctor --fix && quern start` is the sequence this exists for,
        and it is run precisely when the server is down — so an unreachable
        device-tool section must not veto a repair that worked."""
        monkeypatch.setattr("server.main._report_python_deps", lambda fix: True)
        monkeypatch.setattr("server.main._report_external_tools", lambda fix: None)
        monkeypatch.setattr("server.main._report_service_health", lambda fix: True)
        monkeypatch.setattr("server.main.read_state", lambda: None)
        assert _run(args_fix=True) == 0

    def test_fix_fails_when_the_repair_fails(self, monkeypatch, capsys):
        monkeypatch.setattr("server.main._report_python_deps", lambda fix: False)
        monkeypatch.setattr("server.main._report_external_tools", lambda fix: None)
        monkeypatch.setattr("server.main._report_service_health", lambda fix: True)
        self._server(monkeypatch, {"adb": True})
        assert _run(args_fix=True) != 0

    def test_a_failed_repair_still_reports_the_other_sections(self, monkeypatch, capsys):
        """It used to sys.exit(1) inside the deps section, so the run where most
        had gone wrong printed the least."""
        monkeypatch.setattr("server.main._report_python_deps", lambda fix: False)
        monkeypatch.setattr("server.main._report_external_tools", lambda fix: print("EXTERNAL"))
        def services(fix):
            print("SERVICES")
            return True

        monkeypatch.setattr("server.main._report_service_health", services)
        self._server(monkeypatch, {"adb": True})
        _run(args_fix=True)
        out = capsys.readouterr().out
        assert "EXTERNAL" in out and "SERVICES" in out

    def test_a_null_tool_list_is_not_reported_as_an_unreachable_server(
        self, stub_sections, monkeypatch, capsys
    ):
        """`.get(k, {})` defaults only on a missing key, so an explicit null
        produced a dangling "not checked — " with no reason at all."""
        monkeypatch.setattr("server.main.read_state", lambda: {"server_port": 9100})
        monkeypatch.setattr("server.main.is_server_healthy", lambda port: True)
        monkeypatch.setattr("server.main.fetch_tools", lambda port: {"tools": None})
        code = _run()
        out = capsys.readouterr().out
        assert "without a tool list" in out
        assert "not checked — \n" not in out
        assert code != 0
