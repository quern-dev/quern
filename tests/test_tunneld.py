"""Tests for tunneld module — binary discovery, health check, plist generation, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from server.device.tunneld import (
    LOG_PATH,
    TUNNELD_LABEL,
    _run_sudo,
    _tunnel_udid_cache,
    cli_tunneld,
    find_pymobiledevice3_binary,
    generate_plist,
    get_tunneld_devices,
    install_daemon,
    installed_plist_is_current,
    installed_plist_log_path,
    is_tunneld_running,
    resolve_tunnel_udid,
)

# ---------------------------------------------------------------------------
# find_pymobiledevice3_binary
# ---------------------------------------------------------------------------


class TestFindBinary:
    def test_found_on_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/pymobiledevice3"):
            with patch.object(Path, "resolve", return_value=Path("/usr/local/bin/pymobiledevice3")):
                result = find_pymobiledevice3_binary()
                assert result == Path("/usr/local/bin/pymobiledevice3")

    def test_found_in_pipx(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=True):
                with patch.object(Path, "resolve", return_value=Path("/resolved/pymobiledevice3")):
                    result = find_pymobiledevice3_binary()
                    assert result == Path("/resolved/pymobiledevice3")

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = find_pymobiledevice3_binary()
                assert result is None


# ---------------------------------------------------------------------------
# is_tunneld_running
# ---------------------------------------------------------------------------


class TestIsTunneldRunning:
    async def test_running(self):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await is_tunneld_running() is True

    async def test_not_running(self):
        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await is_tunneld_running() is False


# ---------------------------------------------------------------------------
# get_tunneld_devices
# ---------------------------------------------------------------------------


class TestGetTunneldDevices:
    async def test_returns_devices(self):
        devices = {
            "00008130-AAAA": [{"tunnel-address": "fd35::1", "tunnel-port": 61952}],
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = devices

        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_tunneld_devices()
            assert result == devices
            assert "00008130-AAAA" in result

    async def test_connection_error_returns_empty(self):
        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_tunneld_devices()
            assert result == {}


# ---------------------------------------------------------------------------
# resolve_tunnel_udid
# ---------------------------------------------------------------------------


class TestResolveTunnelUdid:
    def setup_method(self):
        _tunnel_udid_cache.clear()

    async def test_cache_hit(self):
        _tunnel_udid_cache["53DA57AA-1234"] = "00008130-AAAA"
        result = await resolve_tunnel_udid("53DA57AA-1234")
        assert result == "00008130-AAAA"

    async def test_single_tunnel_maps_via_devicectl(self):
        """With one tunneled device, devicectl JSON maps the CoreDevice UUID."""
        devices = {"00008130-AAAA": [{"tunnel-address": "fd35::1"}]}
        devicectl_output = {
            "result": {
                "devices": [
                    {
                        "identifier": "53DA57AA-1234",
                        "hardwareProperties": {"udid": "00008130-AAAA"},
                    },
                ]
            }
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def fake_subprocess(*args, **kwargs):
            json_path = args[-1]
            Path(json_path).write_text(json.dumps(devicectl_output))
            return mock_proc

        with (
            patch("server.device.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1234")
            assert result == "00008130-AAAA"
            assert _tunnel_udid_cache["53DA57AA-1234"] == "00008130-AAAA"

    async def test_not_found(self):
        with patch("server.device.tunneld.get_tunneld_devices", return_value={}):
            result = await resolve_tunnel_udid("UNKNOWN-UUID")
            assert result is None

    async def test_multiple_tunnels_maps_via_devicectl(self, tmp_path):
        """With multiple tunneled devices, devicectl JSON maps CoreDevice UUIDs."""
        devices = {
            "00008130-AAAA1111": [{"tunnel-address": "fd35::1"}],
            "00008130-BBBB2222": [{"tunnel-address": "fd35::2"}],
        }
        devicectl_output = {
            "result": {
                "devices": [
                    {
                        "identifier": "53DA57AA-1111",
                        "hardwareProperties": {"udid": "00008130-AAAA1111"},
                    },
                    {
                        "identifier": "53DA57AA-2222",
                        "hardwareProperties": {"udid": "00008130-BBBB2222"},
                    },
                ]
            }
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def fake_subprocess(*args, **kwargs):
            # Write devicectl JSON to the temp file (second-to-last arg)
            json_path = args[-1]
            Path(json_path).write_text(json.dumps(devicectl_output))
            return mock_proc

        with (
            patch("server.device.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1111")
            assert result == "00008130-AAAA1111"
            assert _tunnel_udid_cache["53DA57AA-1111"] == "00008130-AAAA1111"
            assert _tunnel_udid_cache["53DA57AA-2222"] == "00008130-BBBB2222"

    async def test_multiple_tunnels_devicectl_fails_gracefully(self):
        """When devicectl fails with multiple tunnels, returns None."""
        devices = {
            "00008130-AAAA1111": [{"tunnel-address": "fd35::1"}],
            "00008130-BBBB2222": [{"tunnel-address": "fd35::2"}],
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with (
            patch("server.device.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1111")
            assert result is None


# ---------------------------------------------------------------------------
# generate_plist
# ---------------------------------------------------------------------------


class TestGeneratePlist:
    def test_contains_required_keys(self):
        plist = generate_plist(Path("/usr/bin/pymobiledevice3"))
        assert TUNNELD_LABEL in plist
        assert "/usr/bin/pymobiledevice3" in plist
        assert "<string>remote</string>" in plist
        assert "<string>tunneld</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist
        assert str(LOG_PATH) in plist

    def test_log_path_is_system_location(self):
        # Regression: the log path must NOT reference the user's home directory.
        # A user-home path baked into the plist caused launchd to pre-create
        # /Volumes/<HomeVolume>/... at boot for users whose home lived on an
        # external volume, blocking the real volume from mounting at its name.
        plist = generate_plist(Path("/usr/bin/pymobiledevice3"))
        assert "/Users/" not in plist
        assert "/Volumes/" not in plist
        assert "/.quern/" not in plist
        assert str(LOG_PATH).startswith("/Library/Logs/")


# ---------------------------------------------------------------------------
# installed_plist_is_current / installed_plist_log_path
# ---------------------------------------------------------------------------


class TestInstalledPlistFreshness:
    def test_missing_plist_reports_outdated(self, tmp_path):
        with patch("server.device.tunneld.PLIST_PATH", tmp_path / "absent.plist"):
            assert installed_plist_log_path() is None
            assert installed_plist_is_current() is False

    def test_current_plist_reports_current(self, tmp_path):
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(Path("/usr/bin/pymobiledevice3")))
        with patch("server.device.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() == LOG_PATH
            assert installed_plist_is_current() is True

    def test_legacy_plist_reports_outdated(self, tmp_path):
        # Simulate the pre-migration plist that pointed StandardOutPath at the
        # user home. After upgrade, installed_plist_is_current() should be
        # False so setup / status surface the reinstall prompt.
        plist_file = tmp_path / "com.quern.tunneld.plist"
        legacy = generate_plist(Path("/usr/bin/pymobiledevice3")).replace(
            str(LOG_PATH), "/Users/somebody/.quern/tunneld.log",
        )
        plist_file.write_text(legacy)
        with patch("server.device.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() == Path(
                "/Users/somebody/.quern/tunneld.log",
            )
            assert installed_plist_is_current() is False

    def test_unparseable_plist_reports_outdated(self, tmp_path):
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text("not a real plist")
        with patch("server.device.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() is None
            assert installed_plist_is_current() is False


# ---------------------------------------------------------------------------
# _run_sudo + install_daemon upgrade path
# ---------------------------------------------------------------------------


class TestRunSudo:
    def test_returns_true_on_zero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _run_sudo(["ls"], timeout=5) is True
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == ["sudo", "ls"]
            assert kwargs["timeout"] == 5

    def test_returns_false_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert _run_sudo(["ls"], timeout=5) is False

    def test_returns_false_on_timeout_without_raising(self, capsys):
        import subprocess as sp
        with patch(
            "subprocess.run",
            side_effect=sp.TimeoutExpired(cmd=["sudo", "ls"], timeout=5),
        ):
            # Regression: previously a hung launchctl would raise
            # TimeoutExpired all the way to main() and dump a traceback.
            assert _run_sudo(["ls"], timeout=5) is False
        assert "timed out" in capsys.readouterr().out

    def test_returns_false_on_oserror(self, capsys):
        with patch("subprocess.run", side_effect=OSError("no sudo for you")):
            assert _run_sudo(["ls"], timeout=5) is False
        assert "no sudo for you" in capsys.readouterr().out


class TestInstallDaemonUpgradePath:
    def test_unloads_existing_before_bootstrap(self, tmp_path):
        """Regression: bootstrap-over-loaded returns EIO 5 and the previous
        kickstart -k fallback hung launchctl. The upgrade path must call
        bootout *before* bootstrap and skip kickstart entirely."""
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout):
            calls.append(args)
            return True

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
        ):
            assert install_daemon() == 0

        operations = [a[0] for a in calls]
        assert operations == ["launchctl", "cp", "chown", "chmod", "launchctl"]
        # bootout must come first; bootstrap last; no kickstart anywhere.
        assert calls[0] == ["launchctl", "bootout", f"system/{TUNNELD_LABEL}"]
        assert calls[-1][:3] == ["launchctl", "bootstrap", "system"]
        for call in calls:
            assert "kickstart" not in call

    def test_chmods_plist_to_644(self, tmp_path):
        """LaunchDaemon plists should be mode 644 (Apple convention).
        NamedTemporaryFile produces 600 and `cp` preserves that."""
        calls: list[list[str]] = []

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch(
                "server.device.tunneld._run_sudo",
                side_effect=lambda args, timeout: (calls.append(args), True)[1],
            ),
        ):
            assert install_daemon() == 0

        assert ["chmod", "644", str(tmp_path / "tunneld.plist")] in calls

    def test_bootstrap_failure_returns_nonzero(self, tmp_path):
        """If bootstrap genuinely fails after bootout, surface a diagnostic
        message and return 1 instead of silently kickstarting."""
        def fake_run_sudo(args, timeout):
            # bootstrap fails; everything else succeeds
            return "bootstrap" not in args

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
        ):
            assert install_daemon() == 1


# ---------------------------------------------------------------------------
# cli_tunneld
# ---------------------------------------------------------------------------


class TestCliTunneld:
    def test_help(self, capsys):
        result = cli_tunneld(["--help"])
        assert result == 0
        captured = capsys.readouterr()
        assert "install" in captured.out
        assert "uninstall" in captured.out

    def test_no_args_shows_help(self, capsys):
        result = cli_tunneld([])
        assert result == 0
        captured = capsys.readouterr()
        assert "Usage:" in captured.out

    def test_unknown_command(self, capsys):
        result = cli_tunneld(["bogus"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Unknown command" in captured.out

    def test_status_command(self):
        with (
            patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=None),
            patch("server.device.tunneld.PLIST_PATH") as mock_plist,
        ):
            mock_plist.exists.return_value = False
            with patch("urllib.request.urlopen", side_effect=Exception("refused")):
                result = cli_tunneld(["status"])
                assert result == 0
