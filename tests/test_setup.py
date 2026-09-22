"""Tests for server/lifecycle/setup.py — environment checks and setup logic."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from server.lifecycle.setup import (
    PYTHON_MAX,
    PYTHON_MIN,
    CheckResult,
    CheckStatus,
    SetupReport,
    _diagnose_developer_dir,
    _fix_developer_dir_for_setup,
    _home_is_on_external,
    _is_apple_silicon,
    _read_manifest,
    _record_install,
    _sim_bridge_supported,
    _write_manifest,
    _xcode_major_version,
    check_booted_simulators,
    check_homebrew,
    check_libimobiledevice,
    check_mitmdump,
    check_mitmproxy_cert,
    check_node,
    check_platform,
    check_pymobiledevice3,
    check_python,
    check_venv,
    check_vpn,
    check_xcode_cli_tools,
    create_venv,
    install_cert_simulator,
    run_uninstall,
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
        with (
            patch("server.lifecycle.setup.platform.system", return_value="Darwin"),
            patch(
                "server.lifecycle.setup.platform.mac_ver", return_value=("15.2", ("", "", ""), "")
            ),
        ):
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
        with (
            patch("server.lifecycle.setup.sys") as mock_sys,
            patch("server.lifecycle.setup._find_project_root", return_value=tmp_path),
        ):
            mock_sys.prefix = "/usr/local"
            mock_sys.base_prefix = "/usr/local"
            result = check_venv()
            assert result.status == CheckStatus.WARNING
            assert "Not activated" in result.message
            assert not result.fixable

    def test_not_in_venv_no_existing(self, tmp_path):
        # No .venv at all
        (tmp_path / "pyproject.toml").write_text("")
        with (
            patch("server.lifecycle.setup.sys") as mock_sys,
            patch("server.lifecycle.setup._find_project_root", return_value=tmp_path),
        ):
            mock_sys.prefix = "/usr/local"
            mock_sys.base_prefix = "/usr/local"
            result = check_venv()
            assert result.status == CheckStatus.WARNING
            assert result.fixable

    def test_not_in_venv_no_project_root(self):
        with (
            patch("server.lifecycle.setup.sys") as mock_sys,
            patch("server.lifecycle.setup._find_project_root", return_value=None),
        ):
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

        with (
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup.sys") as mock_sys,
        ):
            mock_sys.executable = "/usr/bin/python3"
            assert create_venv(tmp_path) is True
            assert call_count == 2  # venv creation + pip install

    def test_venv_creation_fails(self, tmp_path):
        with (
            _patch_run(_mock_run(stderr="error", returncode=1)),
            patch("server.lifecycle.setup.sys") as mock_sys,
        ):
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

        with (
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup.sys") as mock_sys,
        ):
            mock_sys.executable = "/usr/bin/python3"
            assert create_venv(tmp_path) is False


# ── Homebrew check ───────────────────────────────────────────────────────


class TestCheckHomebrew:
    def test_installed(self):
        with (
            _patch_which({"brew": "/opt/homebrew/bin/brew"}),
            _patch_run(_mock_run(stdout="Homebrew 4.2.0")),
        ):
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
        with (
            _patch_which({"idevicesyslog": "/opt/homebrew/bin/idevicesyslog"}),
            _patch_run(_mock_run(stdout="idevicesyslog 1.3.0")),
        ):
            result = check_libimobiledevice()
            assert result.status == CheckStatus.OK

    def test_not_installed(self):
        with _patch_which({"idevicesyslog": None}):
            result = check_libimobiledevice()
            assert result.status == CheckStatus.MISSING
            assert result.fixable


# ── Xcode CLI Tools check ───────────────────────────────────────────────


class TestXcodeMajorVersion:
    def test_parses_xcode_26(self):
        out = "Xcode 26.0\nBuild version 17A1234\n"
        with _patch_run(_mock_run(stdout=out, returncode=0)):
            assert _xcode_major_version() == 26

    def test_parses_older_xcode(self):
        out = "Xcode 15.4\nBuild version 15F31d\n"
        with _patch_run(_mock_run(stdout=out, returncode=0)):
            assert _xcode_major_version() == 15

    def test_missing_xcodebuild_returns_none(self):
        with _patch_run(_mock_run(stdout="", returncode=127)):
            assert _xcode_major_version() is None

    def test_unrecognized_output_returns_none(self):
        with _patch_run(_mock_run(stdout="something else\n", returncode=0)):
            assert _xcode_major_version() is None


class TestSimBridgeSupported:
    def test_xcode_26_apple_silicon(self):
        with patch("server.lifecycle.setup._is_apple_silicon", return_value=True):
            with patch(
                "server.lifecycle.setup._xcode_major_version", return_value=26
            ):
                assert _sim_bridge_supported() is True

    def test_xcode_25_apple_silicon(self):
        with patch("server.lifecycle.setup._is_apple_silicon", return_value=True):
            with patch(
                "server.lifecycle.setup._xcode_major_version", return_value=25
            ):
                assert _sim_bridge_supported() is False

    def test_intel_mac_with_xcode_26(self):
        with patch("server.lifecycle.setup._is_apple_silicon", return_value=False):
            with patch(
                "server.lifecycle.setup._xcode_major_version", return_value=26
            ):
                assert _sim_bridge_supported() is False

    def test_xcodebuild_missing(self):
        with patch("server.lifecycle.setup._is_apple_silicon", return_value=True):
            with patch(
                "server.lifecycle.setup._xcode_major_version", return_value=None
            ):
                assert _sim_bridge_supported() is False

    def test_real_platform_machine_call_does_not_crash(self):
        # Sanity check on the actual host — we just want this not to raise.
        assert isinstance(_is_apple_silicon(), bool)


class TestCheckXcodeCliTools:
    def test_installed_with_simctl(self):
        with (
            _patch_which({"xcrun": "/usr/bin/xcrun"}),
            _patch_run(_mock_run(stdout="usage: simctl...")),
        ):
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

        with (
            _patch_which({"xcrun": "/usr/bin/xcrun"}),
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup._diagnose_developer_dir", return_value=None),
        ):
            result = check_xcode_cli_tools()
            assert result.status == CheckStatus.WARNING

    def test_xcrun_stale_developer_dir(self):
        """When Xcode is renamed, simctl fails and we diagnose the stale path."""
        def side_effect(cmd, **kwargs):
            return _mock_run(stdout="", returncode=1)

        diagnosis = (
            "xcode-select points to '/Applications/Xcode.app/Contents/Developer' "
            "which does not exist. Found Xcode at '/Applications/Xcode 26.3.app'. "
            "Fix with: sudo xcode-select -s '/Applications/Xcode 26.3.app/Contents/Developer'"
        )
        with (
            _patch_which({"xcrun": "/usr/bin/xcrun"}),
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup._diagnose_developer_dir", return_value=diagnosis),
        ):
            result = check_xcode_cli_tools()
            assert result.status == CheckStatus.ERROR
            assert "developer dir mismatch" in result.message
            assert "xcode-select -s" in result.detail


class TestDiagnoseDeveloperDir:
    def test_valid_developer_dir(self, tmp_path):
        """When developer dir exists, returns None."""
        dev_dir = tmp_path / "Xcode.app" / "Contents" / "Developer"
        dev_dir.mkdir(parents=True)

        def side_effect(cmd, **kwargs):
            return _mock_run(stdout=str(dev_dir))

        with patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect):
            assert _diagnose_developer_dir() is None

    def test_stale_developer_dir_with_renamed_xcode(self, tmp_path):
        """When Xcode is renamed, suggests xcode-select -s with correct path."""
        stale_dir = str(tmp_path / "Xcode.app" / "Contents" / "Developer")
        # Create the renamed Xcode with valid Contents/Developer
        renamed_xcode = tmp_path / "Xcode 26.3.app"
        (renamed_xcode / "Contents" / "Developer").mkdir(parents=True)

        def side_effect(cmd, **kwargs):
            if cmd == ["xcode-select", "-p"]:
                return _mock_run(stdout=stale_dir)
            return _mock_run(returncode=1)

        # Patch /Applications glob to return our tmp_path renamed xcode
        with (
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup.Path.glob",
                  return_value=sorted([renamed_xcode])),
        ):
            result = _diagnose_developer_dir()
            assert result is not None
            assert "does not exist" in result
            assert "xcode-select -s" in result
            assert "Xcode 26.3.app" in result

    def test_xcode_select_fails(self):
        """When xcode-select -p fails, returns None."""
        def side_effect(cmd, **kwargs):
            return _mock_run(returncode=1)

        with patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect):
            assert _diagnose_developer_dir() is None

    def test_commandlinetools_with_xcode_available(self, tmp_path):
        """When pointing to CommandLineTools, suggests the found Xcode."""
        clt_dir = "/Library/Developer/CommandLineTools"

        renamed_xcode = tmp_path / "Xcode 26.3.app"
        (renamed_xcode / "Contents" / "Developer").mkdir(parents=True)

        def side_effect(cmd, **kwargs):
            if cmd == ["xcode-select", "-p"]:
                return _mock_run(stdout=clt_dir)
            return _mock_run(returncode=1)

        with (
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup.Path.glob",
                  return_value=sorted([renamed_xcode])),
        ):
            result = _diagnose_developer_dir()
            assert result is not None
            assert "Command Line Tools" in result
            assert "xcode-select -s" in result


class TestFixDeveloperDirForSetup:
    def test_simctl_already_works(self):
        """Returns None when simctl works fine."""
        def side_effect(cmd, **kwargs):
            return _mock_run(stdout="usage: simctl...")

        with (
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch.dict(os.environ, {}, clear=False),
        ):
            # Ensure DEVELOPER_DIR is not set
            os.environ.pop("DEVELOPER_DIR", None)
            assert _fix_developer_dir_for_setup() is None

    def test_fixes_renamed_xcode(self, tmp_path):
        """Sets DEVELOPER_DIR and returns message when Xcode is renamed."""
        renamed_xcode = tmp_path / "Xcode 26.3.app"
        candidate = renamed_xcode / "Contents" / "Developer"
        candidate.mkdir(parents=True)

        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if cmd[:3] == ["xcrun", "simctl", "help"]:
                # First call: fails (before fix). Second call: succeeds (after fix).
                if call_count <= 1:
                    return _mock_run(returncode=1)
                return _mock_run(stdout="usage: simctl...")
            if cmd == ["xcode-select", "-p"]:
                return _mock_run(stdout="/Applications/Xcode.app/Contents/Developer")
            return _mock_run(returncode=1)

        with (
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
            patch("server.lifecycle.setup.Path.glob",
                  return_value=sorted([renamed_xcode])),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("DEVELOPER_DIR", None)
            result = _fix_developer_dir_for_setup()
            assert result is not None
            assert "Xcode 26.3.app" in result
            assert "xcode-select -s" in result
            assert os.environ.get("DEVELOPER_DIR") == str(candidate)
            # Clean up
            del os.environ["DEVELOPER_DIR"]


# ── mitmdump check ───────────────────────────────────────────────────────


class TestCheckMitmdump:
    def test_installed(self):
        with (
            _patch_which({"mitmdump": "/usr/local/bin/mitmdump"}),
            _patch_run(_mock_run(stdout="Mitmproxy: 10.2.4")),
        ):
            result = check_mitmdump()
            assert result.status == CheckStatus.OK

    def test_not_installed(self):
        with _patch_which({"mitmdump": None}):
            result = check_mitmdump()
            assert result.status == CheckStatus.MISSING
            assert "mitmproxy" in result.detail


# ── Node.js check ────────────────────────────────────────────────────────


def _sites(**status_by_place):
    """Four NodeSites, OK unless named: `_sites(gui=("missing", None))`."""
    from server.lifecycle import node_env

    keys = {"here": "this command", "login": "login shell",
            "script": "non-interactive shell", "gui": "GUI apps",
            "app": "the Quern app"}
    out = []
    for key, place in keys.items():
        status, version = status_by_place.get(key, (node_env.OK, "v22.1.0"))
        path = None if status == node_env.MISSING else f"/x/{key}/node"
        out.append(node_env.NodeSite(place, "someone", status, path, version))
    return out


class TestCheckNode:
    """#214: the version and the other three places now count, and nothing
    here can make setup fail for an install that has been working."""

    def test_installed_everywhere(self):
        result = check_node(_sites())
        assert result.status == CheckStatus.OK
        assert "v22" in result.message

    def test_not_installed(self):
        result = check_node(_sites(here=("missing", None)))
        assert result.status == CheckStatus.MISSING
        assert result.fixable

    def test_node_20_is_a_warning_not_a_pass(self):
        """The old test asserted v20 was OK; the MCP wrapper refuses it."""
        result = check_node(_sites(here=("too_old", "v20.10.0")))
        assert result.status == CheckStatus.WARNING
        assert "22" in result.message

    def test_a_gui_with_no_node_is_named(self):
        result = check_node(_sites(gui=("missing", None)))
        assert result.status == CheckStatus.WARNING
        assert "GUI apps" in result.message
        assert "absolute path" in result.detail

    def test_the_quern_apps_own_row_is_named_separately(self):
        """Two PATHs, two rows: a Homebrew node is invisible to a Dock-launched
        client and visible to the app."""
        result = check_node(_sites(app=("missing", None)))
        assert result.status == CheckStatus.WARNING
        assert "the Quern app" in result.message
        assert "brew install node" in result.detail

    def test_an_unsupported_shell_alone_is_not_a_warning(self):
        result = check_node(_sites(login=("skipped", None), script=("skipped", None)))
        assert result.status == CheckStatus.OK

    def test_the_wrapper_still_builds_on_a_warning(self):
        """Node 20 builds `mcp/dist`. Gating the build on OK would leave it
        stale on exactly the machines the warning is about."""
        from server.lifecycle.setup import _node_can_build

        assert _node_can_build(check_node(_sites(here=("too_old", "v20.10.0"))))
        assert _node_can_build(check_node(_sites(gui=("missing", None))))
        assert not _node_can_build(check_node(_sites(here=("missing", None))))


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

        with (
            patch("server.lifecycle.setup.platform.system", return_value="Darwin"),
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
        ):
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

        with (
            patch("server.lifecycle.setup.platform.system", return_value="Darwin"),
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
        ):
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

        with (
            patch("server.lifecycle.setup.platform.system", return_value="Darwin"),
            patch("server.lifecycle.setup.subprocess.run", side_effect=side_effect),
        ):
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
        json_output = '{"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-2": [{"name": "iPhone 15", "udid": "AAAA-BBBB", "state": "Booted"}, {"name": "iPhone 14", "udid": "CCCC-DDDD", "state": "Shutdown"}]}}'  # noqa: E501
        with _patch_run(_mock_run(stdout=json_output)):
            booted = check_booted_simulators()
            assert len(booted) == 1
            assert booted[0]["name"] == "iPhone 15"

    def test_none_booted(self):
        json_output = '{"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-2": [{"name": "iPhone 15", "udid": "AAAA-BBBB", "state": "Shutdown"}]}}'  # noqa: E501
        with _patch_run(_mock_run(stdout=json_output)):
            booted = check_booted_simulators()
            assert booted == []

    def test_command_fails(self):
        with _patch_run(_mock_run(returncode=1)):
            assert check_booted_simulators() == []


# ── Home-on-external detection + pymobiledevice3 location check ─────────


class TestHomeOnExternal:
    def test_standard_home_is_not_external(self):
        with patch(
            "server.lifecycle.setup.Path.home",
            return_value=Path("/Users/alice"),
        ):
            assert _home_is_on_external() is False

    def test_volumes_home_is_external(self):
        # The motivating case: macOS user with home moved to an external drive.
        fake_home = MagicMock()
        fake_home.resolve.return_value = Path("/Volumes/Home/jham")
        with patch("server.lifecycle.setup.Path.home", return_value=fake_home):
            assert _home_is_on_external() is True


class TestCheckPymobiledevice3:
    def test_not_installed_returns_warning(self):
        with patch(
            "server.device.tunneld.find_pymobiledevice3_binary",
            return_value=None,
        ):
            result = check_pymobiledevice3()
            assert result.status == CheckStatus.WARNING
            assert "Not installed" in result.message

    def test_installed_under_normal_home_is_ok(self):
        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path(
                    "/Users/alice/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
                ),
            ),
            patch("server.lifecycle.setup._run", return_value=(0, "9.15.1\n", "")),
            patch(
                "server.lifecycle.setup._home_is_on_external",
                return_value=False,
            ),
        ):
            result = check_pymobiledevice3()
            assert result.status == CheckStatus.OK

    def test_installed_under_external_home_is_flagged(self):
        # Regression: a binary on /Volumes/... is unreachable pre-login,
        # so the tunneld LaunchDaemon can't reach it at boot. Setup needs
        # to surface this so it can offer `sudo pipx install --global`.
        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path(
                    "/Volumes/Home/jham/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
                ),
            ),
            patch("server.lifecycle.setup._run", return_value=(0, "9.15.1\n", "")),
            patch(
                "server.lifecycle.setup._home_is_on_external",
                return_value=True,
            ),
        ):
            result = check_pymobiledevice3()
            assert result.status == CheckStatus.WARNING
            assert "external home" in result.message
            assert "--global" in (result.detail or "")

    def test_installed_under_usr_local_is_ok_even_with_external_home(self):
        # `sudo pipx install --global` lands at /usr/local — always on the
        # internal disk. That's the resolution we want users to land on,
        # so the check must not flag it.
        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/local/bin/pymobiledevice3"),
            ),
            patch("server.lifecycle.setup._run", return_value=(0, "9.15.1\n", "")),
            patch(
                "server.lifecycle.setup._home_is_on_external",
                return_value=True,
            ),
        ):
            result = check_pymobiledevice3()
            assert result.status == CheckStatus.OK


# ── Simulator cert install ───────────────────────────────────────────────


class TestInstallCertSimulator:
    def test_success(self, tmp_path):
        cert = tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        cert.parent.mkdir(parents=True)
        cert.write_text("fake cert")

        async def mock_is_installed(ctrl, udid):
            return False

        async def mock_install(ctrl, udid, force=False):
            return True

        with (
            patch("server.lifecycle.setup.Path.home", return_value=tmp_path),
            patch("server.proxy.cert_manager.is_cert_installed", side_effect=mock_is_installed),
            patch("server.proxy.cert_manager.install_cert", side_effect=mock_install),
        ):
            result = install_cert_simulator("AAAA-BBBB", "iPhone 15")
            assert result.status == CheckStatus.OK

    def test_already_installed(self, tmp_path):
        cert = tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        cert.parent.mkdir(parents=True)
        cert.write_text("fake cert")

        async def mock_is_installed(ctrl, udid):
            return True

        with (
            patch("server.lifecycle.setup.Path.home", return_value=tmp_path),
            patch("server.proxy.cert_manager.is_cert_installed", side_effect=mock_is_installed),
        ):
            result = install_cert_simulator("AAAA-BBBB", "iPhone 15")
            assert result.status == CheckStatus.OK
            assert "already" in result.message.lower()

    def test_no_cert(self, tmp_path):
        with patch("server.lifecycle.setup.Path.home", return_value=tmp_path):
            result = install_cert_simulator("AAAA-BBBB", "iPhone 15")
            assert result.status == CheckStatus.SKIPPED

    def test_install_fails(self, tmp_path):
        cert = tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        cert.parent.mkdir(parents=True)
        cert.write_text("fake cert")

        async def mock_is_installed(ctrl, udid):
            raise RuntimeError("simctl failed")

        with (
            patch("server.lifecycle.setup.Path.home", return_value=tmp_path),
            patch("server.proxy.cert_manager.is_cert_installed", side_effect=mock_is_installed),
        ):
            result = install_cert_simulator("AAAA-BBBB", "iPhone 15")
            assert result.status == CheckStatus.ERROR


# ── Install manifest ───────────────────────────────────────────────────


class TestInstallManifest:
    def test_read_missing_manifest(self, tmp_path):
        with patch("server.lifecycle.setup.INSTALL_MANIFEST", tmp_path / "nope.json"):
            data = _read_manifest()
            assert data == {"brew": [], "pip": [], "pipx": [], "pipx_global": []}

    def test_write_and_read(self, tmp_path):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        with patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path):
            _write_manifest({"brew": ["node"], "pip": [], "pipx": []})
            data = _read_manifest()
            assert data["brew"] == ["node"]

    def test_record_install_deduplicates(self, tmp_path):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        with patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path):
            _record_install("brew", "node")
            _record_install("brew", "node")
            data = _read_manifest()
            assert data["brew"] == ["node"]

    def test_record_install_multiple(self, tmp_path):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        with patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path):
            _record_install("brew", "node")
            _record_install("brew", "pipx")
            _record_install("pipx", "pymobiledevice3")
            data = _read_manifest()
            assert data["brew"] == ["node", "pipx"]
            assert data["pipx"] == ["pymobiledevice3"]


# ── brew_install records to manifest ───────────────────────────────────


class TestBrewInstallTracking:
    def test_successful_install_recorded(self, tmp_path):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        with (
            patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path),
            patch("server.lifecycle.setup.subprocess.run", return_value=_mock_run(returncode=0)),
        ):
            from server.lifecycle.setup import _brew_install

            assert _brew_install("libimobiledevice") is True
            data = _read_manifest()
            assert "libimobiledevice" in data["brew"]

    def test_failed_install_not_recorded(self, tmp_path):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        with (
            patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path),
            patch("server.lifecycle.setup.subprocess.run", return_value=_mock_run(returncode=1)),
        ):
            from server.lifecycle.setup import _brew_install

            assert _brew_install("node") is False
            data = _read_manifest()
            assert "node" not in data.get("brew", [])


# ── Prompt TTY fallback ───────────────────────────────────────────────


class TestPromptYn:
    def test_no_terminal_declines_and_says_so(self, capsys):
        """`quern update` runs setup, and the menu bar's "Restart to Update"
        runs `quern update` — so setup runs with no controlling terminal, where
        /dev/tty cannot be opened. It used to decline every question silently."""
        from server.lifecycle.setup import _UNASKED, _prompt_yn

        _UNASKED.clear()
        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", side_effect=OSError("no tty")),
        ):
            mock_stdin.isatty.return_value = False
            assert _prompt_yn("Install the thing?", default=True) is False

        out = capsys.readouterr().out
        assert "Install the thing?" in out, "the question must be shown, not swallowed"
        assert "no terminal" in out
        assert _UNASKED == ["Install the thing?"], "it must be recallable at the end"

    def test_the_menu_bar_is_told_where_it_can_answer(self, capsys, monkeypatch):
        """The path the menu bar can actually reach today: `quern update` calls
        run_setup, whose prompts are then declined. "No terminal attached" is
        the diagnosis; where to go is the useful part."""
        from server.lifecycle.invocation import INVOKED_BY, MENUBAR
        from server.lifecycle.setup import _UNASKED, run_setup

        monkeypatch.setenv(INVOKED_BY, MENUBAR)
        monkeypatch.setattr("server.lifecycle.setup._can_prompt", lambda: False)
        monkeypatch.setattr("server.lifecycle.setup.check_homebrew", lambda: CheckResult(
            name="Homebrew", status=CheckStatus.MISSING, message="not found"))

        _UNASKED.clear()
        run_setup()

        out = capsys.readouterr().out
        assert "open a terminal" in out.lower(), (
            "a caller that identified itself as the menu bar should be told "
            "where it can answer, not only that it cannot here"
        )

    def test_a_declined_default_is_not_taken_as_a_yes(self, capsys):
        """Several of these install things. Answering the default would have
        setup say yes on the user's behalf, which is worse than doing less."""
        from server.lifecycle.setup import _UNASKED, _prompt_yn

        _UNASKED.clear()
        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", side_effect=OSError("no tty")),
        ):
            mock_stdin.isatty.return_value = False
            assert _prompt_yn("Install it?", default=True) is False

class TestAssumeYes:
    """`-y`, the way `apt-get -y` means it: answer with the default rather
    than decline. It exists because an unattended install declined the venv
    and stopped with a tree it could not run."""

    @pytest.fixture(autouse=True)
    def _restore(self):
        import server.lifecycle.setup as setup_mod
        before = setup_mod._ASSUME_YES
        yield
        setup_mod._ASSUME_YES = before

    def test_it_takes_the_default_without_a_terminal(self, capsys, monkeypatch):
        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod, "_ASSUME_YES", True)
        # No terminal at all: the case -y is for.
        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", side_effect=OSError("no tty")),
        ):
            mock_stdin.isatty.return_value = False
            assert setup_mod._prompt_yn("Install it?", default=True) is True
            assert setup_mod._prompt_yn("Wipe it?", default=False) is False

        out = capsys.readouterr().out
        assert "Install it?" in out and "(-y)" in out, (
            "an answer given on the user's behalf must still be shown"
        )

    def test_it_does_not_answer_a_deliberate_prompt(self, capsys, monkeypatch):
        """Installing a MITM certificate authority outlives the session that
        wanted it, and the user has to know it happened to undo it. No flag
        answers that one -- which is also why the flag cannot simply be "yes
        to anything that does not need sudo": this needs none."""
        from server.lifecycle import setup as setup_mod

        setup_mod._UNASKED.clear()
        monkeypatch.setattr(setup_mod, "_ASSUME_YES", True)
        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", side_effect=OSError("no tty")),
        ):
            mock_stdin.isatty.return_value = False
            assert setup_mod._prompt_yn("Install the CA?", default=True,
                                        deliberate=True) is False

        assert setup_mod._UNASKED == ["Install the CA?"], (
            "it must still be reported as unasked, not silently skipped"
        )

    # Each entry is a token unique to the action a prompt leads to. The test
    # walks back from it to the `_prompt_yn(` that guards it, so it pins the
    # marking at the call site rather than trusting the flag's unit test --
    # which passes happily while a prompt goes unmarked.
    DELIBERATE_SITES = [
        # A MITM root CA, in a trust store, outliving the capture window.
        ("the capture CA", "Install mitmproxy CA cert into booted simulators?"),
        # A LaunchDaemon running as root at boot, installed with sudo. Larger
        # than the CA on CONTRIBUTING's own test, not smaller -- and `-y`
        # cannot answer the password prompt that follows, so saying yes on the
        # user's behalf buys a hang.
        ("the tunneld daemon", "from server.device.tunneld import install_daemon"),
        # sudo, writing outside $HOME.
        ("a system-wide pipx install", 'pipx_bin, "install", "--global"'),
        # A user-wide macOS setting that `quern uninstall` never reverts.
        ("the crash dialog", '"DialogType", "none"'),
        # Hands off to a macOS dialog somebody has to click, and an unattended
        # run has nobody.
        ("the Xcode CLT installer", "app). Open the installer?"),
    ]

    @pytest.mark.parametrize("name, anchor", DELIBERATE_SITES)
    def test_the_privileged_prompts_are_marked_deliberate(self, name, anchor):
        import inspect

        from server.lifecycle import setup as setup_mod

        src = inspect.getsource(setup_mod)
        assert src.count(anchor) == 1, (
            f"anchor for {name} is not unique ({src.count(anchor)} matches): "
            f"{anchor!r} — an ambiguous one walks back to the wrong call, "
            "and this test then passes against an unmarked prompt"
        )
        at = src.index(anchor)
        opened = src.rfind("_prompt_yn(", 0, at + len(anchor))
        assert opened != -1, f"{name}: no _prompt_yn guarding it"
        # The whole call, by balancing parens: the marking can sit either side
        # of the anchor -- after the prompt text, or before the action it
        # guards -- and a fixed window catches one and misses the other.
        depth, end = 0, len(src)
        for i in range(opened + len("_prompt_yn"), len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        assert "deliberate" in src[opened:end], (
            f"{name} is answered by -y, which either installs something "
            "persistent and privileged without being asked, or waits forever "
            "on a prompt no flag can answer"
        )

    def test_run_setup_sets_and_clears_nothing_behind_it(self, monkeypatch):
        """The flag is module state, so it must be written by run_setup rather
        than left from whatever ran last."""
        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod, "_ASSUME_YES", True)
        monkeypatch.setattr(setup_mod, "_can_prompt", lambda: False)
        monkeypatch.setattr(setup_mod, "check_homebrew", lambda: CheckResult(
            name="Homebrew", status=CheckStatus.MISSING, message="not found"))
        setup_mod.run_setup()
        assert setup_mod._ASSUME_YES is False, (
            "a run without -y must not inherit a previous run's yes"
        )


class TestTheFlagIsActuallyWired:
    """The mutation that disconnected `-y` entirely -- `_ASSUME_YES =
    assume_yes` becoming `= False` -- left the whole suite green, because
    every other test here patches the module variable instead of going
    through `run_setup`. A class named for a flag, passing with the flag
    unplugged, is this repo's signature defect.
    """

    def test_run_setup_carries_the_flag_to_the_prompts(self, monkeypatch, tmp_path):
        from server.lifecycle import setup as setup_mod

        _stub_the_checks_before_the_venv(monkeypatch)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup_mod, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup_mod.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup_mod.sys, "base_prefix", "/usr/local", raising=False)

        answers = []

        def asks_then_fails(*a, **k):
            # Asked from inside the run, with no terminal anywhere: without the
            # flag this is False, with it the default.
            with (
                patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
                patch("builtins.open", side_effect=OSError("no tty")),
            ):
                mock_stdin.isatty.return_value = False
                answers.append(setup_mod._prompt_yn("Install the thing?", default=True))
                answers.append(setup_mod._prompt_yn("Wipe it?", default=False))
            return False        # stop the run here

        monkeypatch.setattr(setup_mod, "create_venv", asks_then_fails)
        setup_mod.run_setup(assume_yes=True)

        assert answers == [True, False], (
            "the flag did not reach _prompt_yn — run_setup is not wiring it"
        )

    def test_without_the_flag_the_same_prompts_decline(self, monkeypatch, tmp_path):
        """The other half: the test above passes if `_prompt_yn` simply
        returns the default always."""
        from server.lifecycle import setup as setup_mod

        _stub_the_checks_before_the_venv(monkeypatch)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup_mod, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup_mod.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup_mod.sys, "base_prefix", "/usr/local", raising=False)

        answers = []

        def asks_then_fails(*a, **k):
            with (
                patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
                patch("builtins.open", side_effect=OSError("no tty")),
            ):
                mock_stdin.isatty.return_value = False
                answers.append(setup_mod._prompt_yn("Install the thing?", default=True))
            return False

        monkeypatch.setattr(setup_mod, "create_venv", asks_then_fails)
        setup_mod.run_setup()

        assert answers == [False], "a run without -y answered a prompt anyway"

    def test_the_flag_survives_the_venv_re_exec(self, monkeypatch, tmp_path):
        """Almost every prompt is *after* the re-exec, and a fresh install --
        the flag's whole reason for existing -- is precisely the run that has
        no venv and therefore re-execs. A child started without `-y` answered
        one prompt out of a dozen."""
        from server.lifecycle import setup as setup_mod

        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(setup_mod, "_ASSUME_YES", True)
        setup_mod._reexec_in_venv(venv)

        assert "--yes" in seen["argv"], (
            f"-y was dropped at the re-exec: {seen['argv']}"
        )

    def test_a_run_without_the_flag_does_not_pass_it_on(self, monkeypatch, tmp_path):
        from server.lifecycle import setup as setup_mod

        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")

        seen = {}
        monkeypatch.setattr(setup_mod.subprocess, "run",
                            lambda argv, **k: seen.update(argv=argv)
                            or SimpleNamespace(returncode=0))
        monkeypatch.setattr(setup_mod, "_ASSUME_YES", False)
        setup_mod._reexec_in_venv(venv)

        assert "--yes" not in seen["argv"]


class TestTheStandingCertAnswer:
    """`auto_install_cert` answers the CA question once. Setup's own prompt
    ignored it, which is the one place CONTRIBUTING says it must not be
    ignored -- so a user who had turned it on was asked anyway, and one who
    had turned it off was asked again."""

    def test_on_installs_without_asking(self):
        from server.lifecycle.setup import _cert_install_decision

        asked = []
        install, note = _cert_install_decision(True, lambda: asked.append(1) or False)
        assert install is True
        assert asked == [], "it asked a question the user had already answered"
        assert "auto_install_cert is on" in note

    def test_off_skips_without_asking(self):
        """The half that matters most: re-asking is how a considered no
        becomes a tired yes."""
        from server.lifecycle.setup import _cert_install_decision

        asked = []
        install, note = _cert_install_decision(False, lambda: asked.append(1) or True)
        assert install is False
        assert asked == [], "it re-asked a question the user had declined"
        assert "not installing" in note
        assert "set-auto-install-cert" in note, "it does not say how to change it"

    def test_unset_asks(self):
        from server.lifecycle.setup import _cert_install_decision

        assert _cert_install_decision(None, lambda: True) == (True, None)
        assert _cert_install_decision(None, lambda: False) == (False, None)

    def test_setup_never_writes_the_setting(self):
        """Saying yes once at a prompt is not choosing a standing policy."""
        import inspect

        from server.lifecycle import setup as setup_mod

        assert "set_auto_install_cert" not in inspect.getsource(setup_mod), (
            "setup writes auto_install_cert, turning one answer into a policy"
        )


class TestTheCertChoiceIsThreeWay:
    def test_unset_is_not_a_no(self, monkeypatch):
        from server import config

        monkeypatch.setattr(config, "read_user_config", lambda: {})
        assert config.auto_install_cert_choice() is None
        assert config.get_auto_install_cert() is False, (
            "the two-way reader must still fold unset into no — silence is "
            "not permission for the callers that install"
        )

    def test_a_literal_boolean_is_the_answer(self, monkeypatch):
        from server import config

        for stored in (True, False):
            monkeypatch.setattr(config, "read_user_config",
                                lambda stored=stored: {"auto_install_cert": stored})
            assert config.auto_install_cert_choice() is stored

    def test_a_typo_reads_as_never_answered(self, monkeypatch):
        """Same rule as the two-way reader: a typo means "ask me", never
        consent — and never a silent no either."""
        from server import config

        for junk in ("yes", "true", 1, None, [], {}):
            monkeypatch.setattr(config, "read_user_config",
                                lambda junk=junk: {"auto_install_cert": junk})
            assert config.auto_install_cert_choice() is None, junk


class TestPromptYnMore:
    def test_tty_stdin(self):
        """Normal TTY stdin reads via input()."""
        from server.lifecycle.setup import _prompt_yn

        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.input", return_value="y"),
        ):
            mock_stdin.isatty.return_value = True
            assert _prompt_yn("Install?") is True

    def test_piped_stdin_opens_tty(self, tmp_path):
        """When stdin is a pipe, _prompt_yn opens /dev/tty."""
        from io import StringIO

        from server.lifecycle.setup import _prompt_yn

        mock_tty = StringIO("y\n")
        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", return_value=mock_tty),
        ):
            mock_stdin.isatty.return_value = False
            assert _prompt_yn("Install?") is True

    def test_piped_stdin_default_on_empty_yes(self, tmp_path):
        """Empty answer uses the default=True."""
        from io import StringIO

        from server.lifecycle.setup import _prompt_yn

        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", return_value=StringIO("\n")),
        ):
            mock_stdin.isatty.return_value = False
            assert _prompt_yn("Install?", default=True) is True

    def test_piped_stdin_default_on_empty_no(self, tmp_path):
        """Empty answer uses the default=False."""
        from io import StringIO

        from server.lifecycle.setup import _prompt_yn

        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", return_value=StringIO("\n")),
        ):
            mock_stdin.isatty.return_value = False
            assert _prompt_yn("Install?", default=False) is False

    def test_piped_stdin_no_tty_returns_false(self):
        """If /dev/tty can't be opened, returns False."""
        from server.lifecycle.setup import _prompt_yn

        with (
            patch("server.lifecycle.setup.sys.stdin") as mock_stdin,
            patch("builtins.open", side_effect=OSError("no tty")),
        ):
            mock_stdin.isatty.return_value = False
            assert _prompt_yn("Install?") is False


# ── Uninstall ──────────────────────────────────────────────────────────


class TestRunUninstall:
    @pytest.fixture(autouse=True)
    def _wrapper_in_a_sandbox(self, tmp_path, monkeypatch):
        """Redirect the wrapper `run_uninstall` removes.

        It was not redirected, so these three tests deleted the developer's own
        `~/.local/bin/quern` on every run -- and on a machine where that is the
        only way `quern` resolves, that is the CLI gone until setup is re-run.
        Patching the module constant rather than `Path.home` keeps the redirect
        in one place and next to the other sandboxing these tests already do.
        """
        wrapper = tmp_path / "sandbox-bin" / "quern"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\n")
        monkeypatch.setattr("server.lifecycle.setup.WRAPPER_PATH", wrapper)
        return wrapper

    def test_abort_on_decline(self, tmp_path):
        """Declining the confirmation aborts cleanly."""
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        with (
            patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path),
            patch("server.lifecycle.setup._prompt_yn", return_value=False),
            patch("server.lifecycle.setup._find_project_root", return_value=tmp_path),
        ):
            assert run_uninstall() == 0

    def test_removes_tracked_brew_packages(self, tmp_path):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"
        manifest_path.parent.mkdir(parents=True)
        import json

        manifest_path.write_text(
            json.dumps(
                {
                    "brew": ["libimobiledevice", "idb-companion"],
                    "pip": [],
                    "pipx": [],
                }
            )
        )

        uninstalled = []

        prompt_calls = [0]

        def mock_prompt(q, default=True):
            prompt_calls[0] += 1
            if prompt_calls[0] == 1:
                return True  # main confirmation
            return False  # decline tunneld

        with (
            patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path),
            patch("server.lifecycle.setup._prompt_yn", side_effect=mock_prompt),
            patch("server.lifecycle.setup._find_project_root", return_value=tmp_path),
            patch("server.lifecycle.setup._which", return_value="/opt/homebrew/bin/brew"),
            patch(
                "server.lifecycle.setup._brew_uninstall",
                side_effect=lambda f: (uninstalled.append(f), True)[-1],
            ),
            patch("server.lifecycle.state.read_state", return_value=None),
            patch("server.lifecycle.setup._remove_mcp_registrations"),
        ):
            result = run_uninstall()
            assert result == 0
            assert "libimobiledevice" in uninstalled
            assert "idb-companion" in uninstalled

    def test_skips_brew_when_nothing_tracked(self, tmp_path, capsys):
        manifest_path = tmp_path / ".quern" / "installed-by-setup.json"

        prompt_calls = [0]

        def mock_prompt(q, default=True):
            prompt_calls[0] += 1
            if prompt_calls[0] == 1:
                return True
            return False

        with (
            patch("server.lifecycle.setup.INSTALL_MANIFEST", manifest_path),
            patch("server.lifecycle.setup._prompt_yn", side_effect=mock_prompt),
            patch("server.lifecycle.setup._find_project_root", return_value=tmp_path),
            patch("server.lifecycle.setup._which", return_value=None),
            patch("server.lifecycle.state.read_state", return_value=None),
            patch("server.lifecycle.setup._remove_mcp_registrations"),
        ):
            run_uninstall()
            output = capsys.readouterr().out
            assert "none were installed by setup" in output


# ---------------------------------------------------------------------------
# Xcode-availability gate
# ---------------------------------------------------------------------------


class TestXcodeGate:
    """On a no-Xcode Mac, the setup checks short-circuit without invoking
    xcrun — otherwise the macOS install dialog fires during ./quern setup."""

    def test_check_xcode_cli_tools_reports_missing_without_invoking_xcrun(
        self, monkeypatch,
    ):
        import server.lifecycle.setup as setup_mod
        monkeypatch.setattr("server.lifecycle.setup.xcode_available", lambda: False)
        # /usr/bin/xcrun ships on every modern macOS as an install-prompt
        # stub, so `which xcrun` returns a path even on no-CLT machines —
        # let the test see that path, then verify we don't actually run it.
        monkeypatch.setattr(
            "server.lifecycle.setup._which",
            lambda name: "/usr/bin/xcrun" if name == "xcrun" else None,
        )
        with patch.object(setup_mod, "_run") as run_mock:
            result = setup_mod.check_xcode_cli_tools()
        assert result.status.name == "MISSING"
        assert run_mock.call_count == 0

    def test_check_booted_simulators_returns_empty_without_invoking_xcrun(
        self, monkeypatch,
    ):
        import server.lifecycle.setup as setup_mod
        monkeypatch.setattr("server.lifecycle.setup.xcode_available", lambda: False)
        with patch.object(setup_mod, "_run") as run_mock:
            assert setup_mod.check_booted_simulators() == []
        assert run_mock.call_count == 0

    def test_fix_developer_dir_skips_simctl_probe_when_no_xcode(
        self, monkeypatch,
    ):
        """Without a developer dir, the initial `xcrun simctl help` probe
        would trigger the install dialog. We should jump straight to
        scanning /Applications instead."""
        import server.lifecycle.setup as setup_mod
        monkeypatch.delenv("DEVELOPER_DIR", raising=False)
        monkeypatch.setattr("server.lifecycle.setup.xcode_available", lambda: False)

        # /Applications has no Xcode → function returns None without ever
        # running xcrun.
        monkeypatch.setattr("pathlib.Path.glob", lambda self, pattern: [])

        # _run returns (rc, stdout, stderr); xcode-select -p is called
        # for the diagnostic message — stub it with a non-xcrun result.
        with patch.object(setup_mod, "_run", return_value=(1, "", "")) as run_mock:
            result = setup_mod._fix_developer_dir_for_setup()
        assert result is None
        # No xcrun invocation — the initial probe was gated.
        for call in run_mock.call_args_list:
            args = call.args[0]
            assert args[0] != "xcrun", f"unexpected xcrun call: {args}"


class TestBuildPreviewApp:
    """The screen-mirror app is built during setup, not on first use.

    Lazy building left the menu-bar app's "Screen Mirror…" item hidden on a
    fresh install: the item only appears when the bundle exists, and nothing
    created it until someone had already driven a preview from the API or an
    MCP tool. That is the opposite of who the menu bar is for.
    """

    def test_builds_when_swiftc_is_available(self):
        from server.lifecycle.setup import build_preview_app

        with patch("server.lifecycle.setup._which", return_value="/usr/bin/swiftc"), \
             patch("server.device.preview.build_preview_bundle") as build:
            result = build_preview_app()

        build.assert_called_once()
        assert result.status == CheckStatus.OK

    def test_missing_command_line_tools_is_skipped_not_failed(self):
        """A machine without swiftc has a missing convenience, not a broken
        install -- the same call scrcpy gets for Android preview."""
        from server.lifecycle.setup import build_preview_app

        with patch("server.lifecycle.setup._which", return_value=None), \
             patch("server.lifecycle.setup._prompt_yn", return_value=False):
            result = build_preview_app()

        assert result.status == CheckStatus.SKIPPED
        assert result.fixable is True
        assert "xcode-select --install" in (result.detail or "")

    def test_accepting_the_prompt_opens_the_installer(self):
        """`xcode-select --install` hands off to a macOS dialog rather than
        installing inline, so setup can only open it and say what comes next."""
        from server.lifecycle.setup import build_preview_app

        ran: list[list[str]] = []
        with patch("server.lifecycle.setup._which", return_value=None), \
             patch("server.lifecycle.setup._prompt_yn", return_value=True), \
             patch("server.lifecycle.setup._run",
                   side_effect=lambda cmd, **kw: (ran.append(cmd), (0, "", ""))[1]):
            result = build_preview_app()

        assert ran == [["xcode-select", "--install"]]
        # Still skipped: the dialog is asynchronous, so nothing was built.
        assert result.status == CheckStatus.SKIPPED

    def test_declining_the_prompt_runs_nothing(self):
        from server.lifecycle.setup import build_preview_app

        with patch("server.lifecycle.setup._which", return_value=None), \
             patch("server.lifecycle.setup._prompt_yn", return_value=False), \
             patch("server.lifecycle.setup._run") as run:
            build_preview_app()

        run.assert_not_called()

    def test_a_filesystem_failure_warns_rather_than_stopping_setup(self):
        """The build stats files, makes directories, writes a plist, copies an
        icon and launches a process. An unwritable ~/.quern raises OSError, and
        catching only RuntimeError would end setup over an optional extra."""
        from server.lifecycle.setup import build_preview_app

        with patch("server.lifecycle.setup._which", return_value="/usr/bin/swiftc"), \
             patch("server.device.preview.build_preview_bundle",
                   side_effect=PermissionError("~/.quern is not writable")):
            result = build_preview_app()

        assert result.status == CheckStatus.WARNING
        assert "not writable" in (result.detail or "")

    def test_a_failed_build_warns_rather_than_stopping_setup(self):
        """Setup continues: everything else about the install is still fine."""
        from server.lifecycle.setup import build_preview_app

        with patch("server.lifecycle.setup._which", return_value="/usr/bin/swiftc"), \
             patch("server.device.preview.build_preview_bundle",
                   side_effect=RuntimeError("swiftc exploded")):
            result = build_preview_app()

        assert result.status == CheckStatus.WARNING
        assert "swiftc exploded" in (result.detail or "")


class TestTunneldDriftReporting:
    """The staleness check tests two conditions with different remedies, and
    reported the log path whichever one failed."""

    def test_a_drifted_binary_is_not_reported_as_a_log_path(self, monkeypatch):
        """The symptom that prompted this: a machine reinstalled the daemon,
        the log path became correct, and doctor went on printing the log path
        as the problem -- so the reinstall looked like it had not worked."""
        from pathlib import Path

        from server.device import tunneld

        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {"Label": "x"})
        monkeypatch.setattr(tunneld, "installed_plist_log_path", lambda: tunneld.LOG_PATH)
        monkeypatch.setattr(
            tunneld, "installed_plist_arguments",
            lambda: ["/old/pmd3", "remote", "tunneld"],
        )
        monkeypatch.setattr(tunneld, "find_pymobiledevice3_binary", lambda: Path("/new/pmd3"))

        drift = tunneld.installed_plist_drift()
        assert drift is not None
        assert "/old/pmd3" in drift and "/new/pmd3" in drift
        assert "log path" not in drift

    def test_a_stale_log_path_still_says_so(self, monkeypatch):
        from pathlib import Path

        from server.device import tunneld

        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {"Label": "x"})
        monkeypatch.setattr(
            tunneld, "installed_plist_log_path", lambda: Path("/Users/x/.quern/tunneld.log")
        )
        drift = tunneld.installed_plist_drift()
        assert drift is not None and "log path" in drift

    @pytest.mark.asyncio
    async def test_a_missing_binary_still_reports_plist_drift(self, monkeypatch, tmp_path):
        """"pymobiledevice3 not found" and "the daemon points at a binary that
        no longer exists" are the same situation from two ends, and only the
        second says the daemon is broken too. Returning early reported the
        first and hid the second."""

        from server.device import tunneld

        plist = tmp_path / "com.quern.tunneld.plist"
        plist.write_text("")
        monkeypatch.setattr(tunneld, "PLIST_PATH", plist)
        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {"Label": "x"})
        monkeypatch.setattr(tunneld, "find_pymobiledevice3_binary", lambda: None)
        monkeypatch.setattr(tunneld, "installed_plist_log_path", lambda: tunneld.LOG_PATH)
        monkeypatch.setattr(
            tunneld, "installed_plist_arguments",
            lambda: ["/gone/pmd3", "remote", "tunneld"],
        )

        health = await tunneld.tunneld_health()
        assert health.status == "no_binary"
        assert "/gone/pmd3" in health.detail, "drift was not surfaced"

    def test_wrong_trailing_arguments_are_drift(self, monkeypatch):
        """generate_plist() writes [binary, "remote", "tunneld"]. A plist with
        the right binary and different trailing arguments launches something
        other than the tunnel daemon, and passed a check that read args[0]."""
        from server.device import tunneld

        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {"Label": "x"})
        monkeypatch.setattr(tunneld, "installed_plist_log_path", lambda: tunneld.LOG_PATH)
        monkeypatch.setattr(
            tunneld, "installed_plist_arguments",
            lambda: ["/usr/bin/pmd3", "remote", "something-else"],
        )
        drift = tunneld.installed_plist_drift()
        assert drift is not None and "arguments are" in drift

    def test_a_current_plist_reports_no_drift(self, monkeypatch):
        from pathlib import Path

        from server.device import tunneld

        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {"Label": "x"})
        monkeypatch.setattr(tunneld, "installed_plist_log_path", lambda: tunneld.LOG_PATH)
        monkeypatch.setattr(
            tunneld, "installed_plist_arguments",
            lambda: ["/same/pmd3", "remote", "tunneld"],
        )
        monkeypatch.setattr(tunneld, "find_pymobiledevice3_binary", lambda: Path("/same/pmd3"))
        assert tunneld.installed_plist_drift() is None


def _forbid_network(monkeypatch):
    """Make any outbound request an immediate failure.

    These tests pass today only because an early return fires first. Reorder
    or remove that return and they would quietly start calling api.github.com
    -- slow, nondeterministic, broken offline, and exactly what CONTRIBUTING
    warns about.
    """
    def _boom(*a, **kw):
        raise AssertionError("test reached the network")

    monkeypatch.setattr("urllib.request.urlopen", _boom)


class TestFetchMenubarApp:
    """v0.15.0 reached existing users without the menu-bar app, and those
    machines cannot repair themselves: `quern update` sees the latest version
    already installed and downloads nothing. Setup is the first code of ours
    that runs on them."""

    def test_a_git_checkout_is_left_alone(self, tmp_path, monkeypatch):
        """A developer builds the app themselves; fetching a release build
        over a working tree would be wrong."""
        from server.lifecycle.setup import fetch_menubar_app

        _forbid_network(monkeypatch)
        (tmp_path / ".git").mkdir()
        assert fetch_menubar_app(tmp_path) is None

    def test_an_install_that_has_the_app_is_left_alone(self, tmp_path, monkeypatch):
        from server.lifecycle.setup import fetch_menubar_app

        _forbid_network(monkeypatch)
        (tmp_path / "Quern.app").mkdir()
        assert fetch_menubar_app(tmp_path) is None

    def test_it_does_nothing_off_macos(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Linux")
        assert setup_mod.fetch_menubar_app(tmp_path) is None

    def test_a_release_without_the_asset_is_skipped_not_failed(self, tmp_path, monkeypatch):
        """Releases cut before the asset existed have nothing to offer, and
        saying so is more useful than a download error."""
        import io as _io
        import json

        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        payload = json.dumps({"assets": []}).encode()

        class _Resp:
            def read(self):
                return payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        result = setup_mod.fetch_menubar_app(tmp_path)
        assert result.status == CheckStatus.SKIPPED
        assert _io  # keep the import meaningful for linters

    def test_a_network_failure_warns_rather_than_ending_setup(self, tmp_path, monkeypatch):
        """A missing menu-bar app is a missing convenience; failing setup over
        it would be worse than the gap it fills."""
        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(OSError("network is down")),
        )
        result = setup_mod.fetch_menubar_app(tmp_path)
        assert result.status == CheckStatus.WARNING
        assert "network is down" in (result.detail or "")
        assert "releases/tag" in (result.detail or ""), "no manual route offered"

    @pytest.mark.release_download
    def test_extraction_goes_through_macos_tar(self, tmp_path, monkeypatch):
        """Python's tarfile cannot extract this bundle correctly.

        The archive carries AppleDouble metadata. macOS tar applies those as
        extended attributes and removes them; tarfile writes them as literal
        `._Contents` files inside the bundle, which breaks the code signature
        seal and makes Gatekeeper reject the app with "a sealed resource is
        missing or invalid". Measured: 21 entries where a correct bundle has
        10, and the damaged copy still passes `stapler validate`, so nothing
        short of `spctl` notices.

        Asserting on the command is a proxy for a property no unit test can
        check without a signed artifact and a network fetch.
        """
        import json

        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        payload = json.dumps({
            "assets": [{
                "name": "quern-9.9.9.tar.gz",
                "browser_download_url": "https://github.com/quern-dev/quern/releases/download/v9.9.9/q.tar.gz",
            }]
        }).encode()

        class _Resp:
            def __init__(self, body=payload):
                self._body = body
            def read(self, n=None):
                body, self._body = self._body, b""
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        monkeypatch.setattr("server.get_version", lambda: "9.9.9")

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "stopped before extraction")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        setup_mod.fetch_menubar_app(tmp_path)

        assert calls, "nothing was executed to extract the archive"
        assert calls[0][0] == "/usr/bin/tar", f"extracted with {calls[0][0]}, not macOS tar"

    def test_a_tampered_bundle_is_refused(self, tmp_path, monkeypatch):
        """This is an executable fetched over the network and then launched,
        so "the release asset said so" is not sufficient provenance."""
        from server.lifecycle import setup as setup_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "codesign" and "--verify" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "code object is not signed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="signature is not valid"):
            setup_mod._verify_menubar_app(tmp_path / "Quern.app", "9.9.9")

    def test_a_bundle_signed_by_someone_else_is_refused(self, tmp_path, monkeypatch):
        """A valid signature says nothing about whose it is."""
        from server.lifecycle import setup as setup_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "codesign" and "-dv" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "TeamIdentifier=EVIL123456\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="expected 3QUH73KW5Q"):
            setup_mod._verify_menubar_app(tmp_path / "Quern.app", "9.9.9")

    def test_gatekeeper_rejection_is_refused(self, tmp_path, monkeypatch):
        """A bundle can be validly signed by us and still not notarized."""
        from server.lifecycle import setup as setup_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "spctl":
                return subprocess.CompletedProcess(cmd, 3, "", "rejected")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="Gatekeeper rejects it"):
            setup_mod._verify_menubar_app(tmp_path / "Quern.app", "9.9.9")

    def test_our_own_signed_bundle_passes(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "codesign" and "-dv" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "", f"TeamIdentifier={setup_mod.RELEASE_TEAM_ID}\n"
                )
            if "PlistBuddy" in cmd[0]:
                return subprocess.CompletedProcess(cmd, 0, "9.9.9\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        setup_mod._verify_menubar_app(tmp_path / "Quern.app", "9.9.9")  # must not raise

    @staticmethod
    def _endless(monkeypatch, setup_mod, chunk=b"x" * 1024, clock_step=0.0):
        """A server that never stops sending, and a clock `clock_step` apart
        per read. Returns the kwargs urlopen was called with."""
        seen = {}
        now = {"t": 0.0}

        sent = {"chunks": 0}

        class Resp:
            def read(self, _n):
                now["t"] += clock_step
                sent["chunks"] += 1
                if sent["chunks"] > 4096:
                    # The fake has to end. With the clock frozen for the
                    # size-cap test, removing the cap left the loop with no
                    # exit at all: the mutant filled the disk until CI killed
                    # the job, which is not a readable failure.
                    raise AssertionError("the download was never bounded")
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(url, **kw):
            seen.update(kw)
            return Resp()

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        monkeypatch.setattr(setup_mod.time, "monotonic", lambda: now["t"])
        return seen

    @pytest.mark.release_download
    def test_a_stalled_download_has_a_socket_timeout(self, tmp_path, monkeypatch):
        """urlretrieve takes no timeout and defaults to none, so a stalled
        transfer held setup open with no deadline. Run, not read: this used to
        grep the source of a function the download has since moved out of."""
        from server.lifecycle import setup as setup_mod

        seen = self._endless(monkeypatch, setup_mod, clock_step=10.0)
        with pytest.raises(RuntimeError):
            setup_mod.download_release_app("https://github.com/x", "0.18.4", tmp_path)
        assert seen.get("timeout"), "the transfer has no socket timeout"

    @pytest.mark.release_download
    def test_a_download_that_never_ends_hits_the_deadline(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        self._endless(monkeypatch, setup_mod, clock_step=10.0)
        with pytest.raises(RuntimeError, match="180s"):
            setup_mod.download_release_app("https://github.com/x", "0.18.4", tmp_path)

    @pytest.mark.release_download
    def test_a_download_that_never_ends_hits_the_size_cap(self, tmp_path, monkeypatch):
        """With the real 200MB cap this wrote 200MB to disk to prove it."""
        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod, "MAX_ASSET_BYTES", 256 * 1024)
        self._endless(monkeypatch, setup_mod, chunk=b"x" * (64 * 1024))
        with pytest.raises(RuntimeError, match="MB"):
            setup_mod.download_release_app("https://github.com/x", "0.18.4", tmp_path)
        written = sum(f.stat().st_size for f in tmp_path.iterdir() if f.is_file())
        assert written < 2 * 1024 * 1024, f"wrote {written} bytes"

    def test_an_older_genuine_build_is_refused(self, tmp_path, monkeypatch):
        """Signature, team and Gatekeeper are all satisfied by any genuine
        Quern app we ever signed, so a replaced asset containing an older real
        build would pass every one of them. That is a downgrade, not a
        forgery, and the asset name alone does not rule it out."""
        from server.lifecycle import setup as setup_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "codesign" and "-dv" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "", f"TeamIdentifier={setup_mod.RELEASE_TEAM_ID}\n"
                )
            if "PlistBuddy" in cmd[0]:
                return subprocess.CompletedProcess(cmd, 0, "0.14.1\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="it is v0.14.1, but v0.15.0 was requested"):
            setup_mod._verify_menubar_app(tmp_path / "Quern.app", "0.15.0")

    def test_the_matching_version_is_accepted(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "codesign" and "-dv" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "", f"TeamIdentifier={setup_mod.RELEASE_TEAM_ID}\n"
                )
            if "PlistBuddy" in cmd[0]:
                return subprocess.CompletedProcess(cmd, 0, "0.15.0\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        setup_mod._verify_menubar_app(tmp_path / "Quern.app", "0.15.0")  # must not raise


    @pytest.mark.release_download
    def test_verification_runs_before_the_app_is_installed(self, tmp_path, monkeypatch):
        """Every other test calls _verify_menubar_app directly. Deleting its
        call site left the whole suite green while setup would download,
        install and launch an unverified bundle -- and because urllib writes
        no quarantine attribute, macOS gives it no first-launch assessment
        either. This is the only trust gate in that path."""
        import json

        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        monkeypatch.setattr("server.get_version", lambda: "9.9.9")
        payload = json.dumps({
            "assets": [{
                "name": "quern-9.9.9.tar.gz",
                "browser_download_url": "https://github.com/quern-dev/quern/releases/download/v9.9.9/q.tar.gz",
            }]
        }).encode()

        class _Resp:
            def __init__(self):
                self._body = payload
            def read(self, n=None):
                body, self._body = self._body, b""
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())

        def fake_tar(cmd, **kw):
            # Stand in for a successful extraction.
            if cmd and "tar" in str(cmd[0]):
                member = Path(kw.get("cwd") or cmd[cmd.index("-C") + 1])
                (member / "quern-9.9.9" / "Quern.app").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_tar)

        called: list[str] = []

        def refusing_verify(app, version):
            called.append(str(app))
            raise setup_mod._UntrustedBundle("substituted asset")

        monkeypatch.setattr(setup_mod, "_verify_menubar_app", refusing_verify)

        result = setup_mod.fetch_menubar_app(tmp_path)

        assert called, "the bundle was installed without ever being verified"
        assert not (tmp_path / "Quern.app").exists(), "a rejected bundle was installed"
        assert result.status == CheckStatus.ERROR

    def test_an_asset_url_off_github_is_refused(self, tmp_path, monkeypatch):
        """The URL comes out of the API response. Anything not on github.com
        means the response is not what we think it is, and following it would
        fetch code from somewhere else entirely."""
        import json

        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        monkeypatch.setattr("server.get_version", lambda: "9.9.9")
        payload = json.dumps({
            "assets": [{
                "name": "quern-9.9.9.tar.gz",
                "browser_download_url": "https://evil.example/q.tar.gz",
            }]
        }).encode()

        class _Resp:
            def __init__(self):
                self._body = payload
            def read(self, n=None):
                body, self._body = self._body, b""
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        result = setup_mod.fetch_menubar_app(tmp_path)
        assert result.status == CheckStatus.ERROR
        assert "not on the release host" in (result.detail or "")


def _boom_oserror(*a, **kw):
    raise OSError("simulated failure")


class TestMenubarInstallLocation:
    """The app was installed into the payload directory under ~/.local, which
    Spotlight excludes, and `open` activated the running old build instead of
    starting the new one — so an update could never deliver a new app to
    anyone already running one."""

    def test_the_app_is_installed_where_a_person_can_find_it(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        root = tmp_path / "install"
        apps = tmp_path / "Applications"
        (root / "Quern.app").mkdir(parents=True)

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", apps)
        monkeypatch.setattr(setup_mod, "_run", lambda cmd, timeout=30: (0, "", ""))

        result = setup_mod.launch_menubar_app(root)

        assert (apps / "Quern.app").exists(), "app was not installed to ~/Applications"
        assert not (root / "Quern.app").exists(), "a second copy was left in the payload dir"
        assert result.status == CheckStatus.OK

    def test_a_running_instance_is_quit_before_the_new_one_opens(self, tmp_path, monkeypatch):
        """`open` activates a running instance rather than starting the new
        binary. Without a quit first, setup reports "Launched" while the old
        build keeps running — true, and describing something that did not
        happen."""
        from server.lifecycle import setup as setup_mod

        root = tmp_path / "install"
        apps = tmp_path / "Applications"
        (root / "Quern.app").mkdir(parents=True)
        calls: list[list[str]] = []
        alive = {"yes": True}

        def record(cmd, timeout=30):
            calls.append(cmd)
            if cmd[0] == "pgrep":
                # Running until the quit, gone after it.
                return (0, "4242", "") if alive["yes"] else (1, "", "")
            if cmd[0] == "osascript":
                alive["yes"] = False
            return (0, "", "")

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", apps)
        monkeypatch.setattr(setup_mod, "_run", record)
        setup_mod.launch_menubar_app(root)

        quit_at = next(i for i, c in enumerate(calls) if c[0] == "osascript")
        open_at = next(i for i, c in enumerate(calls) if c[0] == "open")
        assert quit_at < open_at, "opened the app before asking the old one to quit"
        # The bundle it is replacing, not any Quern: the quit asks by
        # application name, so a generic question stops someone else's copy.
        import re as _re

        pgrep_before_quit = next(c for c in calls if c[0] == "pgrep")
        assert _re.escape(str(apps / "Quern.app")) in pgrep_before_quit[-1], pgrep_before_quit

    def test_an_already_installed_app_is_not_refetched(self, tmp_path, monkeypatch):
        """After the first setup the app lives only in ~/Applications.
        Checking the payload directory alone would download it every run."""
        from server.lifecycle import setup as setup_mod

        root = tmp_path / "install"
        root.mkdir()
        apps = tmp_path / "Applications"
        (apps / "Quern.app").mkdir(parents=True)

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", apps)
        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        _forbid_network(monkeypatch)

        assert setup_mod.fetch_menubar_app(root) is None

    def test_nothing_is_left_behind_in_the_payload_directory(self, tmp_path, monkeypatch):
        """The delivered copy is moved, not copied, so an old app cannot sit
        forgotten under ~/.local while a newer one runs from ~/Applications."""
        from server.lifecycle import setup as setup_mod

        root = tmp_path / "install"
        apps = tmp_path / "Applications"
        (root / "Quern.app" / "Contents").mkdir(parents=True)

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", apps)
        monkeypatch.setattr(setup_mod, "_run", lambda cmd, timeout=30: (0, "", ""))
        setup_mod.launch_menubar_app(root)

        assert list(root.iterdir()) == [], "a copy was left in the payload directory"

    def test_a_failed_install_says_where_the_app_actually_is(self, tmp_path, monkeypatch):
        """The move empties the payload directory before the final rename, so
        a failure after that point left the advice pointing at a path that no
        longer existed."""
        from server.lifecycle import setup as setup_mod

        root = tmp_path / "install"
        apps = tmp_path / "Applications"
        (root / "Quern.app" / "Contents").mkdir(parents=True)

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", apps)
        monkeypatch.setattr(setup_mod, "_run", lambda cmd, timeout=30: (0, "", ""))
        monkeypatch.setattr(setup_mod.os, "replace", _boom_oserror)

        result = setup_mod.launch_menubar_app(root)

        assert result.status == CheckStatus.WARNING
        named = result.detail.split("The app is at ")[-1].rstrip(".").strip()
        assert Path(named).exists(), f"pointed at {named}, which does not exist"
        # And it must be back where the next run will find it, not stranded
        # under the temporary staging name -- which exists, so merely checking
        # existence passes while the app is somewhere nobody would look.
        assert Path(named).name == "Quern.app", f"left at {named}"
        assert (root / "Quern.app").exists(), "not restored to the payload directory"

    def test_a_source_only_install_is_left_alone(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        assert setup_mod.launch_menubar_app(tmp_path / "install") is None


class TestTheMenubarAppIsNotLeftStopped:
    """#215: setup quit the running app and reopened it on every run -- every
    run on a git install, where nothing is ever delivered -- and a failed
    reopen (`-600`) left the machine with no menu bar at all."""

    @staticmethod
    def _machine(monkeypatch, setup_mod, tmp_path, *, running, open_results):
        """`_run` for a machine whose app is (not) running and whose `open`
        answers from `open_results` in turn. Returns the commands seen."""
        apps = tmp_path / "Applications"
        (apps / "Quern.app").mkdir(parents=True)
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", apps)
        monkeypatch.setattr(setup_mod.time, "sleep", lambda _s: None)
        state = {"running": running}
        answers = list(open_results)
        calls: list[list[str]] = []

        def run(cmd, timeout=30):
            calls.append(cmd)
            if cmd[0] == "pgrep":
                return (0, "4242", "") if state["running"] else (1, "", "")
            if cmd[0] == "osascript":
                state["running"] = False
                return (0, "", "")
            if cmd[0] == "open":
                rc, err = answers.pop(0) if answers else (0, "")
                if rc == 0:
                    state["running"] = True
                return (rc, "", err)
            return (0, "", "")

        monkeypatch.setattr(setup_mod, "_run", run)
        return calls

    def test_a_running_app_with_nothing_new_is_left_alone(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        calls = self._machine(monkeypatch, setup_mod, tmp_path, running=True, open_results=[])
        (tmp_path / "install").mkdir()

        result = setup_mod.launch_menubar_app(tmp_path / "install")

        assert result.status == CheckStatus.OK
        assert not [c for c in calls if c[0] in ("osascript", "open")], (
            f"restarted an app that had nothing new to run: {calls}"
        )

    def test_a_stopped_app_with_nothing_new_is_started(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        calls = self._machine(monkeypatch, setup_mod, tmp_path, running=False,
                              open_results=[(0, "")])
        (tmp_path / "install").mkdir()

        result = setup_mod.launch_menubar_app(tmp_path / "install")

        assert result.status == CheckStatus.OK
        assert [c[0] for c in calls if c[0] in ("osascript", "open")] == ["open"]

    def test_the_error_a_just_quit_app_gives_is_retried(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        busy = "_LSOpenURLsWithCompletionHandler() failed with error -600."
        calls = self._machine(monkeypatch, setup_mod, tmp_path, running=True,
                              open_results=[(1, busy), (1, busy), (0, "")])
        (tmp_path / "install" / "Quern.app").mkdir(parents=True)

        result = setup_mod.launch_menubar_app(tmp_path / "install")

        assert result.status == CheckStatus.OK, result
        assert len([c for c in calls if c[0] == "open"]) == 3

    def test_other_failures_are_not_retried(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as setup_mod

        calls = self._machine(monkeypatch, setup_mod, tmp_path, running=False,
                              open_results=[(1, "The application cannot be opened.")] * 5)
        (tmp_path / "install").mkdir()

        result = setup_mod.launch_menubar_app(tmp_path / "install")

        assert result.status == CheckStatus.WARNING
        assert len([c for c in calls if c[0] == "open"]) == 1

    def test_stopping_it_and_failing_to_restart_says_so(self, tmp_path, monkeypatch):
        """The machine has no menu bar because setup stopped it. "Could not
        launch" hides that; the reader needs to know it was running before."""
        from server.lifecycle import setup as setup_mod

        busy = "failed with error -600."
        calls = self._machine(monkeypatch, setup_mod, tmp_path, running=True,
                              open_results=[(1, busy)] * 99)
        (tmp_path / "install" / "Quern.app").mkdir(parents=True)

        result = setup_mod.launch_menubar_app(tmp_path / "install")

        assert result.status == CheckStatus.WARNING
        assert "Stopped" in result.message, result.message
        installed = tmp_path / "Applications" / "Quern.app"
        assert f"open {installed}" in result.detail, result.detail
        opens = len([c for c in calls if c[0] == "open"])
        assert opens == setup_mod._OPEN_ATTEMPTS, f"retried {opens} times"

    def test_a_first_install_that_fails_to_launch_does_not_claim_it_stopped_one(
        self, tmp_path, monkeypatch,
    ):
        """Nothing was running, so nothing was stopped. Found in review."""
        from server.lifecycle import setup as setup_mod

        self._machine(monkeypatch, setup_mod, tmp_path, running=False,
                      open_results=[(1, "The application cannot be opened.")])
        (tmp_path / "install" / "Quern.app").mkdir(parents=True)

        result = setup_mod.launch_menubar_app(tmp_path / "install")

        assert result.status == CheckStatus.WARNING
        assert "Stopped" not in result.message, result.message
        assert result.message == "Could not launch Quern.app"


class TestOtherQuernOnPath:
    """A second `quern` on PATH is what makes a stale shell hash possible."""

    def _make(self, directory: Path, executable: bool = True) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "quern"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755 if executable else 0o644)
        return path

    def test_reports_nothing_when_ours_is_the_only_copy(self, tmp_path, monkeypatch):
        from server.lifecycle.setup import _other_quern_on_path

        ours = self._make(tmp_path / "local" / "bin")
        monkeypatch.setenv("PATH", f"{ours.parent}:{tmp_path / 'empty'}")

        assert _other_quern_on_path(ours) == []

    def test_reports_a_second_copy_in_path_order(self, tmp_path, monkeypatch):
        from server.lifecycle.setup import _other_quern_on_path

        ours = self._make(tmp_path / "local" / "bin")
        clone = self._make(tmp_path / "Dev" / "quern")
        other = self._make(tmp_path / "opt" / "bin")
        monkeypatch.setenv("PATH", f"{ours.parent}:{clone.parent}:{other.parent}")

        assert _other_quern_on_path(ours) == [clone, other]

    def test_ignores_a_non_executable_file(self, tmp_path, monkeypatch):
        from server.lifecycle.setup import _other_quern_on_path

        ours = self._make(tmp_path / "local" / "bin")
        stub = self._make(tmp_path / "Dev" / "quern", executable=False)
        monkeypatch.setenv("PATH", f"{ours.parent}:{stub.parent}")

        assert _other_quern_on_path(ours) == []

    def test_ignores_a_directory_named_quern(self, tmp_path, monkeypatch):
        from server.lifecycle.setup import _other_quern_on_path

        ours = self._make(tmp_path / "local" / "bin")
        (tmp_path / "Dev" / "quern").mkdir(parents=True)
        monkeypatch.setenv("PATH", f"{ours.parent}:{tmp_path / 'Dev'}")

        assert _other_quern_on_path(ours) == []

    def test_survives_an_empty_path_entry(self, tmp_path, monkeypatch):
        from server.lifecycle.setup import _other_quern_on_path

        ours = self._make(tmp_path / "local" / "bin")
        monkeypatch.setenv("PATH", f"{ours.parent}::")

        assert _other_quern_on_path(ours) == []

    def test_setup_warns_rather_than_reporting_a_clean_install(self, tmp_path, monkeypatch):
        """The whole point: a shadowed wrapper must not read as all-clear."""
        from server.lifecycle import setup as setup_mod

        project = tmp_path / "clone"
        (project / ".venv" / "bin").mkdir(parents=True)
        (project / ".venv" / "bin" / "python").write_text("")
        (project / "server").mkdir()

        home = tmp_path / "home"
        clone_copy = self._make(project)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        # WRAPPER_PATH is resolved at import, so patching `Path.home` no longer
        # redirects it -- this test wrote to the developer's real
        # ~/.local/bin/quern until the guard in conftest caught it. The constant
        # exists precisely so the redirect is one line and in one place.
        monkeypatch.setattr(setup_mod, "WRAPPER_PATH", home / ".local" / "bin" / "quern")
        monkeypatch.setattr(setup_mod, "_find_project_root", lambda: project)
        monkeypatch.setenv("PATH", f"{home / '.local' / 'bin'}:{project}")

        result = setup_mod.install_wrapper_script()

        assert result.status is CheckStatus.WARNING
        assert str(clone_copy) in result.detail
        assert "rehash" in result.detail


def _stub_the_checks_before_the_venv(monkeypatch):
    """Get `run_setup` as far as the venv block, on any machine.

    Two things sit in front of it and both bite.

    `check_homebrew` halts the run outright when brew is missing, so on a
    machine without it these tests never reach the code they name -- and every
    assertion about "it stopped" is satisfied by the *Homebrew* stop. That is
    how `TestAFailedVenvRecreateStopsThere` passed with its fix fully reverted.

    `check_python` is worse than a false pass: the venv tests answer yes to
    every prompt, and the Python check offers a Homebrew install. `_brew_install`
    calls `subprocess.run(["brew", "install", ...])` directly rather than through
    the patched `_run`, so nothing in the test or in conftest stops a real
    install. Latent only because the suite runs on a supported interpreter --
    which is exactly what the recreate branch under test assumes is not the case.
    """
    from server.lifecycle import setup
    from server.lifecycle.setup import CheckResult, CheckStatus

    monkeypatch.setattr(
        setup, "check_homebrew",
        lambda *a, **k: CheckResult(
            name="Homebrew", status=CheckStatus.OK, message="stubbed",
        ),
    )
    monkeypatch.setattr(
        setup, "check_python",
        lambda *a, **k: CheckResult(
            name="Python", status=CheckStatus.OK, message="stubbed",
        ),
    )
    monkeypatch.setattr(
        setup, "_brew_install",
        lambda *a, **k: pytest.fail("a test shelled out to a real `brew install`"),
    )


class TestTheVenvIsNotAQuestion:
    """A venv inside the install directory *is* the install, the way
    node_modules is `npm install`, so setup creates one rather than asking.

    It used to ask, and with no terminal `_prompt_yn` declines rather than
    hanging -- so an unattended install (the `curl | bash` one-liner in a
    provisioning script, or the menu bar's update) stopped with a tree it
    could not run. There was never a second answer either: every check past
    this point needs the venv.

    What must survive from the old behaviour is the *stop*. Falling through
    reaches a block commented "we're inside the venv" that reports the check
    OK, and the run then died at the first third-party import with
    `ModuleNotFoundError: No module named 'httpx'` -- several hundred lines
    from the cause, naming a dependency the user never mentioned.
    """

    def _no_venv(self, monkeypatch, tmp_path):
        from server.lifecycle import setup

        _stub_the_checks_before_the_venv(monkeypatch)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
        # Not in a venv.
        monkeypatch.setattr(setup.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)
        return setup

    def test_it_is_created_without_being_asked_about(self, monkeypatch, tmp_path):
        """The regression guard for the change itself: no prompt, and a venv."""
        setup = self._no_venv(monkeypatch, tmp_path)
        asked, created = [], []
        monkeypatch.setattr(setup, "_prompt_yn",
                            lambda q, *a, **k: asked.append(q) or False)
        monkeypatch.setattr(setup, "create_venv",
                            lambda *a, **k: created.append(True) or True)
        monkeypatch.setattr(setup, "_reexec_in_venv", lambda *a, **k: 0)

        setup.run_setup()

        assert created == [True], "no venv was created"
        assert not any("virtual environment" in q.lower() for q in asked), (
            "setup asked whether to create the venv, which an unattended run "
            "answers no to, leaving a tree it cannot run"
        )

    def test_a_failure_to_create_one_stops_there(self, monkeypatch, tmp_path, capsys):
        setup = self._no_venv(monkeypatch, tmp_path)
        monkeypatch.setattr(setup, "create_venv", lambda *a, **k: False)

        # The load-bearing assertion. `run_setup` returns 1 for plenty of
        # reasons in a sandbox, so an exit code alone does not show it stopped
        # *here* -- with the return removed it falls through, every later check
        # runs against an interpreter with no dependencies, and the run still
        # ends in 1. Pinning the first check past the venv block is what
        # distinguishes "stopped" from "carried on and failed anyway".
        reached = []
        monkeypatch.setattr(setup, "check_mitmdump",
                            lambda *a, **k: reached.append(True))

        rc = setup.run_setup()

        assert reached == [], (
            "setup carried on without the venv it failed to create, which is "
            "how this surfaced as ModuleNotFoundError several hundred lines "
            "later"
        )
        assert rc == 1, "a failed venv was reported as success"
        out = capsys.readouterr().out
        assert "Failed to create virtual environment" in out, (
            "the summary does not say why it stopped"
        )
        assert "httpx" not in out, (
            "the failure surfaced as a missing dependency rather than the "
            "step that caused it"
        )

    def test_a_failure_still_reports_what_was_never_asked(
        self, monkeypatch, tmp_path, capsys
    ):
        """The early return skips the block that names unanswered questions and
        says to run setup in a terminal. Other prompts run before this one, so
        a machine with no terminal still needs that report."""
        setup_mod = self._no_venv(monkeypatch, tmp_path)

        # Stands in for an earlier prompt that went unanswered, recorded at the
        # point the real no-terminal path would have recorded it. It cannot be
        # pre-seeded -- `run_setup` clears `_UNASKED` on entry -- and it cannot
        # be the venv question any more, which is the point of this change.
        def fails_after_something_went_unasked(*a, **k):
            setup_mod._UNASKED.append("Node.js not found. Install via Homebrew?")
            return False

        monkeypatch.setattr(setup_mod, "create_venv",
                            fails_after_something_went_unasked)

        setup_mod.run_setup()

        out = capsys.readouterr().out
        assert "declined without asking" in out
        assert "quern setup" in out, "it does not say how to answer the question"


class TestAFailedVenvRecreateStopsThere:
    """One branch above the declined-venv fix, the same shape.

    "Recreate venv with X?" accepted -> the old venv is deleted -> `create_venv`
    fails -> execution fell through to the branch that prints "Virtual
    environment found but not activated" about a directory that no longer
    exists, then re-execs into it. That returns -1, so `quern setup` exits 255
    with no summary, no CheckResult and no guidance, having just destroyed the
    user's environment.
    """

    def test_it_reports_instead_of_re_execing_into_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        from server.lifecycle import setup

        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        (tmp_path / "pyproject.toml").write_text("")

        reexeced = []
        _stub_the_checks_before_the_venv(monkeypatch)
        monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(setup, "create_venv", lambda *a, **k: False)
        monkeypatch.setattr(
            setup, "_reexec_in_venv", lambda *a, **k: reexeced.append(True) or -1,
        )

        # The branch only runs for a venv built with an unsupported Python when
        # a better one exists: venv on 3.14 (> PYTHON_MAX), best is 3.12.
        monkeypatch.setattr(setup, "_find_best_python", lambda *a, **k: "python3.12")

        def fake_run(cmd, *a, **k):
            if str(cmd[0]).endswith(".venv/bin/python"):
                return 0, "Python 3.14.0", ""
            return 0, "Python 3.12.0", ""

        monkeypatch.setattr(setup, "_run", fake_run)

        # Not inside a venv, so the block is reached at all.
        monkeypatch.setattr(setup.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)

        rc = setup.run_setup()

        assert reexeced == [], (
            "it re-execed into a venv it had just deleted, which exits 255 with "
            "no explanation"
        )
        assert rc == 1, "the failure did not reach the exit code"
        out = capsys.readouterr().out
        assert "Could not recreate the venv" in out, (
            "it stopped, but reported nothing -- the bug was exiting 255 with no "
            "summary, no CheckResult and no guidance, and a negative assertion "
            "alone does not pin that"
        )
        assert "found but not activated" not in out, (
            "it described a deleted directory as present"
        )


class TestTheEntryPointsParseTheirArguments:
    """Both dispatchers dropped everything after the subcommand, so a
    mistyped flag did the command's whole job with the flag discarded --
    worse than refusing, because the caller believes they opted in.

    None of this had a test: mutating the `-y` wiring out of *either* entry
    point left the suite green.
    """

    def _run_main(self, monkeypatch, argv):
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", *argv])
        called = {}
        monkeypatch.setattr(
            "server.lifecycle.setup.run_setup",
            lambda assume_yes=False: called.update(assume_yes=assume_yes) or 0,
        )
        with pytest.raises(SystemExit) as exc:
            entry.main()
        return exc.value.code, called

    def test_the_flag_reaches_run_setup(self, monkeypatch):
        for flag in ("-y", "--yes"):
            code, called = self._run_main(monkeypatch, ["setup", flag])
            assert code == 0
            assert called == {"assume_yes": True}, flag

    def test_plain_setup_does_not_assume_yes(self, monkeypatch):
        code, called = self._run_main(monkeypatch, ["setup"])
        assert called == {"assume_yes": False}

    def test_help_prints_usage_instead_of_running_setup(self, monkeypatch, capsys):
        """`quern setup --help` ran a full setup, which is how a review agent
        rewrote its own Claude hook while probing this."""
        code, called = self._run_main(monkeypatch, ["setup", "--help"])
        assert code == 0
        assert called == {}, "--help ran setup instead of printing usage"
        assert "Usage: quern setup" in capsys.readouterr().out

    def test_a_mistyped_flag_is_refused(self, monkeypatch, capsys):
        code, called = self._run_main(monkeypatch, ["setup", "--yse"])
        assert code == 2, "a typo ran setup with the flag silently discarded"
        assert called == {}
        assert "--yse" in capsys.readouterr().err

    @pytest.mark.parametrize("command", [
        "uninstall", "mcp-install", "grant-full-perms", "install-precommit-hook",
        "update",
    ])
    def test_the_siblings_refuse_a_stray_flag(self, monkeypatch, command, capsys):
        """`quern mcp-install --help` rewrote every MCP client config, and
        `quern update --help` ran a real update."""
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", command, "--badflag"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 2, f"{command} accepted a flag it does not take"
        assert "--badflag" in capsys.readouterr().err

    @pytest.mark.parametrize("command", [
        "uninstall", "mcp-install", "grant-full-perms", "install-precommit-hook",
        "update",
    ])
    def test_the_siblings_answer_help(self, monkeypatch, command, capsys):
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", command, "--help"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 0
        assert f"Usage: quern {command}" in capsys.readouterr().out

    @pytest.mark.parametrize("command", [
        "setup", "uninstall", "mcp-install", "grant-full-perms",
        "install-precommit-hook", "update",
    ])
    def test_a_stray_operand_is_refused(self, monkeypatch, command, capsys):
        """Rejecting unknown *flags* and then discarding leftover words is the
        same silent drop one level down: `quern update typo` ran a real
        update."""
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", command, "typo"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 2, f"{command} dispatched with a stray operand"
        assert "typo" in capsys.readouterr().err

    @pytest.mark.parametrize("command", [
        "set-channel", "set-auto-install-cert", "set-update-check",
    ])
    def test_a_setting_takes_one_value_and_no_more(self, monkeypatch, command, capsys):
        """Each helper reads only its first argument, so a second word was
        persisted-and-ignored: `quern set-channel stable typo` wrote stable
        and said nothing about the word it did not understand."""
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", command, "stable", "typo"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 2, f"{command} persisted a value it half-understood"
        assert "typo" in capsys.readouterr().err

    @pytest.mark.parametrize("command", [
        "set-channel", "set-auto-install-cert", "set-update-check",
    ])
    def test_a_setting_still_takes_its_one_value(self, monkeypatch, command):
        """The other half: the guard must not refuse the ordinary call."""
        import server.__main__ as entry

        seen = {}
        monkeypatch.setattr(entry, "_cmd_set_channel",
                            lambda a: seen.update(args=a) or 0)
        monkeypatch.setattr(entry, "_cmd_set_auto_install_cert",
                            lambda a: seen.update(args=a) or 0)
        monkeypatch.setattr(entry, "_cmd_set_update_check",
                            lambda a: seen.update(args=a) or 0)
        monkeypatch.setattr(entry.sys, "argv", ["quern", command, "on"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 0
        assert seen == {"args": ["on"]}, f"{command} did not receive its value"

    def test_argparse_does_not_swallow_a_stray_flag(self, monkeypatch, capsys):
        """The other entry point. `parse_known_args` kept the leftovers only
        for the no-subcommand case, and discarded them everywhere else."""
        from server import main as main_mod

        monkeypatch.setattr(main_mod.sys, "argv", ["quern", "setup", "--yse"])
        with pytest.raises(SystemExit) as exc:
            main_mod.cli()
        assert exc.value.code == 2
        assert "--yse" in capsys.readouterr().err


class TestUrlAndEnv:
    """`quern url` and `quern env` exist so a script never writes 9100 down.

    The shipped example did exactly that -- `os.getenv("QUERN_SERVER_URL",
    "http://127.0.0.1:9100")` -- which is the habit CONTRIBUTING forbids in
    the sentence "All consumers discover the server via ~/.quern/state.json.
    Never hardcode ports."
    """

    def _run(self, monkeypatch, argv, state=None, key=None):
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", *argv])
        monkeypatch.setattr(
            "server.lifecycle.state.read_state", lambda: state,
        )
        monkeypatch.setattr(
            "server.lifecycle.state.is_server_healthy", lambda port, **kw: True,
        )
        if key is not None:
            import tempfile
            from pathlib import Path
            tmp = Path(tempfile.mkdtemp()) / "api-key"
            tmp.write_text(key)
            monkeypatch.setattr("server.config.API_KEY_FILE", tmp)
        with pytest.raises(SystemExit) as exc:
            entry.main()
        return exc.value.code

    def test_url_reports_the_port_the_server_actually_took(self, monkeypatch, capsys):
        """Not the default. A server that found 9100 busy is on another port,
        and that is precisely when a hardcoded URL fails."""
        code = self._run(monkeypatch, ["url"], state={"server_port": 9137})
        assert code == 0
        assert capsys.readouterr().out.strip() == "http://127.0.0.1:9137"

    def test_a_stale_state_file_is_not_a_running_server(self, monkeypatch, capsys):
        """A crash or a SIGKILL leaves state.json behind. Without a health
        check `quern url` exits 0 and hands a script a URL that refuses
        connections — worse than the hardcoded 9100 it replaced, because it
        looks authoritative."""
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", "url"])
        monkeypatch.setattr(
            "server.lifecycle.state.read_state", lambda: {"server_port": 9137},
        )
        monkeypatch.setattr(
            "server.lifecycle.state.is_server_healthy", lambda port, **kw: False,
        )
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == "", "a script would have used this URL"
        assert "quern start" in err

    @pytest.mark.parametrize("port", [True, False, 0, 70000, -1, "9100", None, 3.5])
    def test_an_unusable_port_is_refused(self, monkeypatch, capsys, port):
        """`isinstance(port, int)` was the test, and `True` passes it —
        bool subclasses int — as do 0 and 70000."""
        import server.__main__ as entry

        monkeypatch.setattr(entry.sys, "argv", ["quern", "url"])
        monkeypatch.setattr(
            "server.lifecycle.state.read_state", lambda: {"server_port": port},
        )
        # Would pass the health check if it were ever reached, so a failure
        # here is the validation and nothing else.
        monkeypatch.setattr(
            "server.lifecycle.state.is_server_healthy", lambda p, **kw: True,
        )
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 1, f"{port!r} was accepted as a port"
        assert capsys.readouterr().out == ""

    def test_url_says_so_when_nothing_is_running(self, monkeypatch, capsys):
        code = self._run(monkeypatch, ["url"], state=None)
        assert code == 1
        out, err = capsys.readouterr()
        assert out == "", "a script would have eval'd or curl'd this"
        assert "quern start" in err

    def test_env_is_evalable(self, monkeypatch, capsys):
        code = self._run(
            monkeypatch, ["env"], state={"server_port": 9137}, key="s3cret",
        )
        assert code == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == [
            "export QUERN_SERVER_URL=http://127.0.0.1:9137",
            "export QUERN_API_KEY=s3cret",
        ]

    def test_env_quotes_what_it_exports(self, monkeypatch, capsys):
        """An API key is opaque; a shell-special character in one must not
        become shell syntax when the caller evals it."""
        code = self._run(
            monkeypatch, ["env"], state={"server_port": 9137}, key="a b;rm -rf /",
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "export QUERN_API_KEY='a b;rm -rf /'" in out

    def test_env_prints_nothing_when_there_is_no_server(self, monkeypatch, capsys):
        """A partial environment is worse than none: `eval` would set half of
        it and the script would fail later, somewhere unrelated."""
        code = self._run(monkeypatch, ["env"], state=None, key="s3cret")
        assert code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert "quern start" in err

    def test_env_does_not_emit_the_prototype_name(self, monkeypatch, capsys):
        """QUERN_DEBUG_SERVER_URL is the old name. The wrapper still honours
        it with a deprecation warning; nothing should be teaching it."""
        self._run(monkeypatch, ["env"], state={"server_port": 9137}, key="s3cret")
        assert "QUERN_DEBUG_SERVER_URL" not in capsys.readouterr().out


class TestRestartKeepsThePort:
    """`quern restart` takes no ports, so they arrived as None and the
    defaults were filled in — meaning a server on any other port came back on
    9100.

    Not hypothetical: `quern update` restarts the server for you, so an
    update silently moved it. The rehearsal caught this by starting a server
    on 9190 and watching it return on 9102.
    """

    def _resolved(self, monkeypatch, argv, state):
        """The ports `cli()` would hand to start, without starting anything."""
        from server import main as main_mod

        monkeypatch.setattr(main_mod, "read_state", lambda: state)
        monkeypatch.setattr(main_mod.sys, "argv", ["quern", *argv])
        seen = {}
        monkeypatch.setattr(main_mod, "_cmd_restart",
                            lambda args: seen.update(port=args.port,
                                                     proxy=args.proxy_port))
        # `cli()` returns for restart rather than exiting; other commands
        # exit, so both are tolerated.
        with contextlib.suppress(SystemExit):
            main_mod.cli()
        return seen

    def test_it_returns_to_the_port_it_was_on(self, monkeypatch):
        seen = self._resolved(
            monkeypatch, ["restart"],
            state={"server_port": 9190, "proxy_port": 9191},
        )
        assert seen == {"port": 9190, "proxy": 9191}, (
            "the restart moved the server to the default port"
        )

    def test_an_explicit_port_still_wins(self, monkeypatch):
        """`quern restart --port N` is a request to move, and adopting the
        running port must not override it."""
        seen = self._resolved(
            monkeypatch, ["restart", "--port", "9300"],
            state={"server_port": 9190, "proxy_port": 9191},
        )
        assert seen["port"] == 9300
        assert seen["proxy"] == 9191, "the proxy port was not asked about"

    def test_with_no_server_it_falls_back_to_the_defaults(self, monkeypatch):
        from server.lifecycle.ports import DEFAULT_PROXY_PORT, DEFAULT_SERVER_PORT

        seen = self._resolved(monkeypatch, ["restart"], state=None)
        assert seen == {"port": DEFAULT_SERVER_PORT, "proxy": DEFAULT_PROXY_PORT}

    def test_a_junk_port_in_state_does_not_become_the_port(self, monkeypatch):
        """State is a file on disk and can be anything. A non-integer must
        fall through to the default rather than reaching `bind`."""
        from server.lifecycle.ports import DEFAULT_SERVER_PORT

        seen = self._resolved(
            monkeypatch, ["restart"],
            state={"server_port": "not-a-port", "proxy_port": None},
        )
        assert seen["port"] == DEFAULT_SERVER_PORT

    def test_start_is_not_affected(self, monkeypatch):
        """Only restart adopts. `quern start` with no port means the default,
        which is how someone deliberately returns a moved server to 9100."""
        from server import main as main_mod
        from server.lifecycle.ports import DEFAULT_SERVER_PORT

        monkeypatch.setattr(main_mod, "read_state",
                            lambda: {"server_port": 9190, "proxy_port": 9191})
        monkeypatch.setattr(main_mod.sys, "argv", ["quern", "start"])
        seen = {}
        monkeypatch.setattr(main_mod, "_cmd_start",
                            lambda args: seen.update(port=args.port))
        with contextlib.suppress(SystemExit):
            main_mod.cli()
        assert seen == {"port": DEFAULT_SERVER_PORT}
