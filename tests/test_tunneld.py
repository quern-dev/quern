"""Tests for tunneld module — binary discovery, health check, plist generation, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from server.device.tunneld import (
    TUNNELD_LABEL,
    TUNNELD_URL,
    _tunnel_udid_cache,
    cli_tunneld,
    find_pymobiledevice3_binary,
    generate_plist,
    get_tunneld_devices,
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

    async def test_single_tunnel_auto_maps(self):
        """With only one tunneled device, it maps automatically."""
        devices = {"00008130-AAAA": [{"tunnel-address": "fd35::1"}]}
        with patch("server.device.tunneld.get_tunneld_devices", return_value=devices):
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

        with patch("server.device.tunneld.get_tunneld_devices", return_value=devices), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
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

        with patch("server.device.tunneld.get_tunneld_devices", return_value=devices), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
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
        assert "tunneld.log" in plist


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
        with patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=None), \
             patch("server.device.tunneld.PLIST_PATH") as mock_plist:
            mock_plist.exists.return_value = False
            with patch("urllib.request.urlopen", side_effect=Exception("refused")):
                result = cli_tunneld(["status"])
                assert result == 0
