"""Tests for server/lifecycle/setup.py — environment checks and setup logic."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from server.lifecycle.setup import (
    PYTHON_MAX,
    PYTHON_MIN,
    CheckResult,
    CheckStatus,
    SetupReport,
    check_booted_simulators,
    check_homebrew,
    check_libimobiledevice,
    check_mitmproxy_cert,
    check_mitmdump,
    check_node,
    check_platform,
    check_python,
    check_venv,
    check_vpn,
    check_xcode_cli_tools,
    create_venv,
    install_cert_simulator,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _mock_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock subprocess.run result."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def _patch_run(return_value):
    """Patch subprocess.run in the setup module."""
    return patch("server.lifecycle.setup.subprocess.run", return_value=return_value)


def _patch_which(mapping: dict[str, str | None]):
    """Patch shutil.which to return values based on a mapping."""
    def fake_which(name):
        return mapping.get(name)
    return patch("server.lifecycle.setup.shutil.which", side_effect=fake_which)


# ── SetupReport ──────────────────────────────────────────────────────────


class TestSetupReport:
    def test_empty_report(self):
        report = SetupReport()
        assert not report.has_errors
        assert not report.has_warnings

    def test_has_errors(self):
        report = SetupReport()
        report.add(CheckResult("A", CheckStatus.OK, "fine"))
        report.add(CheckResult("B", CheckStatus.MISSING, "gone"))
        assert report.has_errors

    def test_has_warnings(self):
        report = SetupReport()
        report.add(CheckResult("A", CheckStatus.OK, "fine"))
        report.add(CheckResult("B", CheckStatus.WARNING, "hmm"))
        assert report.has_warnings
        assert not report.has_errors

    def test_all_ok(self):
        report = SetupReport()
        report.add(CheckResult("A", CheckStatus.OK, "fine"))
        report.add(CheckResult("B", CheckStatus.OK, "fine"))
        assert not report.has_errors
        assert not report.has_warnings


# ── CheckResult ──────────────────────────────────────────────────────────


class TestCheckResult:
    def test_icons(self):
        assert CheckResult("x", CheckStatus.OK, "").icon == "✓"
        assert CheckResult("x", CheckStatus.WARNING, "").icon == "⚠"
        assert CheckResult("x", CheckStatus.MISSING, "").icon == "✗"
        assert CheckResult("x", CheckStatus.ERROR, "").icon == "✗"
        assert CheckResult("x", CheckStatus.SKIPPED, "").icon == "–"


# ── Platform check ───────────────────────────────────────────────────────


class TestCheckPlatform:
    def test_macos(self):
        with patch("server.lifecycle.setup.platform.system", return_value="Darwin"), \
             patch("server.lifecycle.setup.platform.mac_ver", return_value=("15.2", ("", "", ""), "")):
            result = check_platform()
            assert result.status == CheckStatus.OK
            assert "15.2" in result.message

    def test_linux(self):
        with patch("server.lifecycle.setup.platform.system", return_value="Linux"):
            result = check_platform()
            assert result.status == CheckStatus.WARNING
            assert "Linux" in result.message


# ── Python check ─────────────────────────────────────────────────────────


class TestCheckPython:
    def test_good_version(self):
        with patch.object(sys, "version_info", (3, 12, 1, "final", 0)):
            result = check_python()
            assert result.status == CheckStatus.OK
            assert "3.12.1" in result.message

    def test_minimum_version(self):
        with patch.object(sys, "version_info", (PYTHON_MIN[0], PYTHON_MIN[1], 0, "final", 0)):
            result = check_python()
            assert result.status == CheckStatus.OK

    def test_maximum_version(self):
        with patch.object(sys, "version_info", (PYTHON_MAX[0], PYTHON_MAX[1], 9, "final", 0)):
            result = check_python()
            assert result.status == CheckStatus.OK

    def test_old_version(self):
        with patch.object(sys, "version_info", (3, 10, 5, "final", 0)):
            result = check_python()
            assert result.status == CheckStatus.ERROR
            assert result.fixable

    def test_too_new_version_with_supported_available(self):
        """Above max but a supported python exists — OK with note."""
        def mock_which(name):
            if name == "python3.13":
                return None
            if name == "python3.12":
                return "/usr/bin/python3.12"
            return None

        with patch.object(sys, "version_info", (3, 99, 0, "final", 0)):
            with patch("server.lifecycle.setup._which", side_effect=mock_which):
                result = check_python()
                assert result.status == CheckStatus.OK
                assert "python3.12" in result.message

    def test_too_new_version_no_supported(self):
        """Above max and no supported python — WARNING + fixable."""
        with patch.object(sys, "version_info", (3, 99, 0, "final", 0)):
            with patch("server.lifecycle.setup._which", return_value=None):
                result = check_python()
                assert result.status == CheckStatus.WARNING
                assert result.fixable
                assert "3.11" in result.message


# ── Virtual environment check ───────────────────────────────────────────


class TestCheckVenv:
    def test_in_venv(self):
        with patch("server.lifecycle.setup.sys") as mock_sys:
            mock_sys.prefix = "/some/path/.venv"
            mock_sys.base_prefix = "/usr/local"
            result = check_venv()
            assert result.status == CheckStatus.OK
            assert ".venv" in result.message

    def test_not_in_venv_existing(self, tmp_path):
        # .venv exists but not activated
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        (tmp_path / "pyproject.toml").write_text("")
        with patch("server.lifecycle.setup.sys") as mock_sys, \
             patch("server.lifecycle.setup._find_project_root", return_value=tmp_path):
            mock_sys.prefix = "/usr/local"
            mock_sys.base_prefix = "/usr/local"
            result = check_venv()
            assert result.status == CheckStatus.WARNING
            assert "Not activated" in result.message
            assert not result.fixable

    def test_not_in_venv_no_existing(self, tmp_path):
        # No .venv at all
        (tmp_path / "pyproject.toml").write_text("")
        with patch("server.lifecycle.setup.sys") as mock_sys, \
             patch("server.lifecycle.setup._find_project_root", return_value=tmp_path):
            mock_sys.prefix = "/usr/local"
            mock_sys.base_prefix = "/usr/local"
            result = check_venv()
            assert result.status == CheckStatus.WARNING
            assert result.fixable

    def test_not_in_venv_no_project_root(self):
        with patch("server.lifecycle.setup.sys") as mock_sys, \
             patch("server.lifecycle.setup._find_project_root", return_value=None):
            mock_sys.prefix = "/usr/local"
            mock_sys.base_prefix = "/usr/local"
            result = check_venv()
            assert result.status == CheckStatus.WARNING
            assert result.fixable


class TestCreateVenv:
    def test_success(self, tmp_path):
        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return _mock_run()

        with patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect), \
             patch("server.lifecycle.setup.sys") as mock_sys:
            mock_sys.executable = "/usr/bin/python3"
            assert create_venv(tmp_path) is True
            assert call_count == 2  # venv creation + pip install

    def test_venv_creation_fails(self, tmp_path):
        with _patch_run(_mock_run(stderr="error", returncode=1)), \
             patch("server.lifecycle.setup.sys") as mock_sys:
            mock_sys.executable = "/usr/bin/python3"
            assert create_venv(tmp_path) is False

    def test_pip_install_fails(self, tmp_path):
        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_run()  # venv creation succeeds
            return _mock_run(returncode=1)  # pip install fails

        with patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect), \
             patch("server.lifecycle.setup.sys") as mock_sys:
            mock_sys.executable = "/usr/bin/python3"
            assert create_venv(tmp_path) is False


# ── Homebrew check ───────────────────────────────────────────────────────


class TestCheckHomebrew:
    def test_installed(self):
        with _patch_which({"brew": "/opt/homebrew/bin/brew"}), \
             _patch_run(_mock_run(stdout="Homebrew 4.2.0")):
            result = check_homebrew()
            assert result.status == CheckStatus.OK
            assert "Homebrew" in result.message

    def test_not_installed(self):
        with _patch_which({"brew": None}):
            result = check_homebrew()
            assert result.status == CheckStatus.MISSING
            assert "brew.sh" in result.detail


# ── libimobiledevice check ───────────────────────────────────────────────


class TestCheckLibimobiledevice:
    def test_installed(self):
        with _patch_which({"idevicesyslog": "/opt/homebrew/bin/idevicesyslog"}), \
             _patch_run(_mock_run(stdout="idevicesyslog 1.3.0")):
            result = check_libimobiledevice()
            assert result.status == CheckStatus.OK

    def test_not_installed(self):
        with _patch_which({"idevicesyslog": None}):
            result = check_libimobiledevice()
            assert result.status == CheckStatus.MISSING
            assert result.fixable


# ── Xcode CLI Tools check ───────────────────────────────────────────────


class TestCheckXcodeCliTools:
    def test_installed_with_simctl(self):
        with _patch_which({"xcrun": "/usr/bin/xcrun"}), \
             _patch_run(_mock_run(stdout="usage: simctl...")):
            result = check_xcode_cli_tools()
            assert result.status == CheckStatus.OK
            assert "simctl" in result.message

    def test_not_installed(self):
        with _patch_which({"xcrun": None}):
            result = check_xcode_cli_tools()
            assert result.status == CheckStatus.MISSING
            assert "xcode-select" in result.detail

    def test_xcrun_without_simctl(self):
        def side_effect(cmd, **kwargs):
            return _mock_run(stdout="", returncode=1)

        with _patch_which({"xcrun": "/usr/bin/xcrun"}), \
             patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect):
            result = check_xcode_cli_tools()
            assert result.status == CheckStatus.WARNING


# ── mitmdump check ───────────────────────────────────────────────────────


class TestCheckMitmdump:
    def test_installed(self):
        with _patch_which({"mitmdump": "/usr/local/bin/mitmdump"}), \
             _patch_run(_mock_run(stdout="Mitmproxy: 10.2.4")):
            result = check_mitmdump()
            assert result.status == CheckStatus.OK

    def test_not_installed(self):
        with _patch_which({"mitmdump": None}):
            result = check_mitmdump()
            assert result.status == CheckStatus.MISSING
            assert "mitmproxy" in result.detail


# ── Node.js check ────────────────────────────────────────────────────────


class TestCheckNode:
    def test_installed(self):
        with _patch_which({"node": "/opt/homebrew/bin/node"}), \
             _patch_run(_mock_run(stdout="v20.10.0")):
            result = check_node()
            assert result.status == CheckStatus.OK
            assert "v20" in result.message

    def test_not_installed(self):
        with _patch_which({"node": None}):
            result = check_node()
            assert result.status == CheckStatus.MISSING
            assert result.fixable


# ── VPN detection ────────────────────────────────────────────────────────


class TestCheckVpn:
    def test_no_vpn(self):
        scutil_out = '* (Disconnected)  "Work VPN"  [com.apple.something]\n'
        route_out = (
            "   route to: default\n"
            "destination: default\n"
            "    gateway: 192.168.1.1\n"
            "  interface: en0\n"
        )
        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "scutil" in cmd:
                return _mock_run(stdout=scutil_out)
            if "route" in cmd:
                return _mock_run(stdout=route_out)
            return _mock_run()

        with patch("server.lifecycle.setup.platform.system", return_value="Darwin"), \
             patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect):
            result = check_vpn()
            assert result.status == CheckStatus.OK

    def test_vpn_connected(self):
        scutil_out = '* (Connected)     "Corp VPN"  [com.apple.something]\n'
        route_out = "  interface: en0\n"

        def side_effect(cmd, **kwargs):
            if "scutil" in cmd:
                return _mock_run(stdout=scutil_out)
            if "route" in cmd:
                return _mock_run(stdout=route_out)
            return _mock_run()

        with patch("server.lifecycle.setup.platform.system", return_value="Darwin"), \
             patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect):
            result = check_vpn()
            assert result.status == CheckStatus.WARNING
            assert "Corp VPN" in result.message

    def test_tunnel_interface(self):
        scutil_out = '* (Disconnected)  "VPN"  [com.apple.something]\n'
        route_out = "  interface: utun3\n"

        def side_effect(cmd, **kwargs):
            if "scutil" in cmd:
                return _mock_run(stdout=scutil_out)
            if "route" in cmd:
                return _mock_run(stdout=route_out)
            return _mock_run()

        with patch("server.lifecycle.setup.platform.system", return_value="Darwin"), \
             patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect):
            result = check_vpn()
            assert result.status == CheckStatus.WARNING
            assert "tunnel" in result.message.lower()

    def test_non_macos_skipped(self):
        with patch("server.lifecycle.setup.platform.system", return_value="Linux"):
            result = check_vpn()
            assert result.status == CheckStatus.SKIPPED


# ── mitmproxy cert check ────────────────────────────────────────────────


class TestCheckMitmproxyCert:
    def test_cert_exists(self, tmp_path):
        cert = tmp_path / "mitmproxy-ca-cert.pem"
        cert.write_text("fake cert")
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path / "fake_home"):
            # Override the cert path construction
            with patch("server.lifecycle.setup.Path.__truediv__", side_effect=Path.__truediv__):
                pass
        # Simpler: just patch the path directly
        fake_cert = tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        fake_cert.parent.mkdir(parents=True)
        fake_cert.write_text("fake cert")
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path):
            result = check_mitmproxy_cert()
            assert result.status == CheckStatus.OK

    def test_cert_missing(self, tmp_path):
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path):
            result = check_mitmproxy_cert()
            assert result.status == CheckStatus.WARNING


# ── Booted simulators ───────────────────────────────────────────────────


class TestCheckBootedSimulators:
    def test_finds_booted(self):
        json_output = '{"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-2": [{"name": "iPhone 15", "udid": "AAAA-BBBB", "state": "Booted"}, {"name": "iPhone 14", "udid": "CCCC-DDDD", "state": "Shutdown"}]}}'
        with _patch_run(_mock_run(stdout=json_output)):
            booted = check_booted_simulators()
            assert len(booted) == 1
            assert booted[0]["name"] == "iPhone 15"

    def test_none_booted(self):
        json_output = '{"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-2": [{"name": "iPhone 15", "udid": "AAAA-BBBB", "state": "Shutdown"}]}}'
        with _patch_run(_mock_run(stdout=json_output)):
            booted = check_booted_simulators()
            assert booted == []

    def test_command_fails(self):
        with _patch_run(_mock_run(returncode=1)):
            assert check_booted_simulators() == []


# ── Simulator cert install ───────────────────────────────────────────────


class TestInstallCertSimulator:
    def test_success(self, tmp_path):
        cert = tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        cert.parent.mkdir(parents=True)
        cert.write_text("fake cert")
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path), \
             _patch_run(_mock_run()):
            result = install_cert_simulator("AAAA-BBBB", "iPhone 15")
            assert result.status == CheckStatus.OK

    def test_no_cert(self, tmp_path):
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path):
            result = install_cert_simulator("AAAA-BBBB", "iPhone 15")
            assert result.status == CheckStatus.SKIPPED

    def test_simctl_fails(self, tmp_path):
        cert = tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        cert.parent.mkdir(parents=True)
        cert.write_text("fake cert")
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path), \
             _patch_run(_mock_run(stderr="error: invalid udid", returncode=1)):
            result = install_cert_simulator("bad-udid", "iPhone 15")
            assert result.status == CheckStatus.ERROR
