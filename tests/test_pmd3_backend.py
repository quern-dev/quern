"""Tests for Pmd3Backend — mock subprocess calls to pymobiledevice3."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.pmd3 import Pmd3Backend, _no_tunnel_hint, _recover_tunneld_if_wedged
from server.models import DeviceError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Create a mock subprocess result."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available(self):
        backend = Pmd3Backend()
        with patch(
            "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
        ):
            assert await backend.is_available() is True

    async def test_not_available(self):
        backend = Pmd3Backend()
        with patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=None):
            assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    async def test_binary_not_found(self):
        backend = Pmd3Backend()

        with patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=None):
            with pytest.raises(DeviceError, match="pymobiledevice3 not found"):
                await backend.screenshot("UUID")

    async def test_tunnel_route_success(self, tmp_path):
        """iOS 17+ path: tunneld running, tunnel found → uses --tunnel flag."""
        backend = Pmd3Backend()
        fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=True),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("server.device.tunneld.resolve_tunnel_udid", return_value="00008130-AAAA"),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(fake_png)
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(0)

            result = await backend.screenshot("UUID")
            assert result == fake_png

            # Verify --tunnel flag is used
            call_args = mock_exec.call_args[0]
            assert "--tunnel" in call_args
            assert "00008130-AAAA" in call_args

    async def test_fallback_no_tunneld(self, tmp_path):
        """iOS 16- path: tunneld not running → direct usbmuxd (no --tunnel)."""
        backend = Pmd3Backend()
        fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=False),
            patch("server.device.pmd3._recover_tunneld_if_wedged", return_value=False),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(fake_png)
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(0)

            result = await backend.screenshot("UUID")
            assert result == fake_png

            # Verify no --tunnel flag, but --udid is passed
            call_args = mock_exec.call_args[0]
            assert "--tunnel" not in call_args
            assert "--udid" in call_args
            assert "UUID" in call_args
            assert "developer" in call_args
            assert "screenshot" in call_args

    async def test_fallback_no_tunnel_for_device(self, tmp_path):
        """Tunneld running but device not in tunnel list → falls back to direct."""
        backend = Pmd3Backend()
        fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=True),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("server.device.tunneld.resolve_tunnel_udid", return_value=None),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(fake_png)
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(0)

            result = await backend.screenshot("UUID")
            assert result == fake_png

            call_args = mock_exec.call_args[0]
            assert "--tunnel" not in call_args
            assert "--udid" in call_args

    async def test_screenshot_failure(self, tmp_path):
        backend = Pmd3Backend()

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=True),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("server.device.tunneld.resolve_tunnel_udid", return_value="00008130-AAAA"),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(b"")
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(1, stderr=b"capture error")

            with pytest.raises(DeviceError, match="pymobiledevice3 screenshot failed"):
                await backend.screenshot("UUID")

    async def test_developer_disk_error(self, tmp_path):
        """Older iOS without DeveloperDiskImage gives a helpful error."""
        backend = Pmd3Backend()

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=False),
            patch("server.device.pmd3._recover_tunneld_if_wedged", return_value=False),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(b"")
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(
                1,
                stderr=b"DeveloperDiskImage is not mounted",
            )

            with pytest.raises(DeviceError, match="Developer disk image not mounted"):
                await backend.screenshot("UUID")


class TestNoTunnelHint:
    """#73: a wedged tunneld is alive, so it looks installed and running to
    anyone who checks the obvious way. When the usbmuxd fallback then fails,
    the error has to name that as the likely cause -- pymobiledevice3's own
    message describes the symptom and never the reason."""

    def test_silent_when_a_tunnel_was_used(self):
        assert _no_tunnel_hint("00008130-AAAA", tunneld_serving=True) == ""

    def test_names_tunneld_when_not_serving(self):
        hint = _no_tunnel_hint(None, tunneld_serving=False)
        assert "tunneld is not serving" in hint
        assert "wedged" in hint
        assert "tunneld restart" in hint

    def test_distinguishes_serving_but_no_tunnel_for_device(self):
        """A serving daemon with no tunnel for this device is a pairing
        problem, not a wedge, and points somewhere else entirely."""
        hint = _no_tunnel_hint(None, tunneld_serving=True)
        assert "no tunnel for this device" in hint
        assert "wedged" not in hint


class TestScreenshotFailureNamesTheCause:
    async def test_usbmuxd_failure_blames_tunneld(self, tmp_path):
        backend = Pmd3Backend()

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=False),
            patch("server.device.pmd3._recover_tunneld_if_wedged", return_value=False),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file
            mock_exec.return_value = _make_proc(1, stderr=b"Device is not connected")

            with pytest.raises(DeviceError, match="tunneld is not serving"):
                await backend.screenshot("UUID")

    async def test_tunnel_route_failure_carries_no_hint(self, tmp_path):
        """The hint is about the fallback. A failure on the tunnel path has a
        tunnel, so blaming tunneld there would be wrong."""
        backend = Pmd3Backend()

        with (
            patch("server.device.tunneld.is_tunneld_running", return_value=True),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")
            ),
            patch("server.device.tunneld.resolve_tunnel_udid", return_value="00008130-AAAA"),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file
            mock_exec.return_value = _make_proc(1, stderr=b"boom")

            with pytest.raises(DeviceError) as excinfo:
                await backend.screenshot("UUID")
            assert "tunneld" not in str(excinfo.value)


class TestAutoRecoveryGate:
    """Healing in passing is only safe when it is certain what is broken and
    certain nobody has to be asked."""

    @staticmethod
    def _health(status, pid=949):
        return MagicMock(status=status, pid=pid)

    async def test_recovers_a_wedge_when_authorised(self):
        with (
            patch("server.device.tunneld.tunneld_health", return_value=self._health("wedged")),
            patch("server.device.tunneld.can_recover_unattended", return_value=True),
            patch("server.device.tunneld.recover_wedged_tunneld", return_value=True) as recover,
        ):
            assert await _recover_tunneld_if_wedged() is True
        recover.assert_called_once()

    async def test_leaves_a_stopped_daemon_alone(self):
        """Signalling a job that is not running fixes nothing, and would trade
        a clear failure for a confusing one."""
        with (
            patch("server.device.tunneld.tunneld_health", return_value=self._health("stopped")),
            patch("server.device.tunneld.can_recover_unattended", return_value=True),
            patch("server.device.tunneld.recover_wedged_tunneld") as recover,
        ):
            assert await _recover_tunneld_if_wedged() is False
        recover.assert_not_called()

    async def test_never_prompts_without_the_grant(self, caplog):
        """A screenshot is the wrong moment to raise an auth dialog: the
        caller is usually a script, and a blocked prompt looks like a hang."""
        with (
            patch("server.device.tunneld.tunneld_health", return_value=self._health("wedged")),
            patch("server.device.tunneld.can_recover_unattended", return_value=False),
            patch("server.device.tunneld.recover_wedged_tunneld") as recover,
        ):
            assert await _recover_tunneld_if_wedged() is False
        recover.assert_not_called()
