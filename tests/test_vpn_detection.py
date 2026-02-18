"""Tests for VPN and network detection helpers."""

from unittest.mock import patch, MagicMock

from server.api.proxy_certs import (
    _get_connected_vpns,
    _detect_proxy_warnings,
)
from server.proxy.system_proxy import get_default_route_device


def _mock_run(stdout: str, returncode: int = 0):
    """Create a mock subprocess.run result."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


class TestGetConnectedVpns:
    def test_no_vpns(self):
        output = (
            '* (Disconnected)  "Work VPN"          [com.apple.something]\n'
            '* (Disconnected)  "Personal VPN"       [com.apple.something]\n'
        )
        with patch("server.api.proxy_certs.subprocess.run", return_value=_mock_run(output)):
            assert _get_connected_vpns() == []

    def test_one_connected(self):
        output = (
            '* (Connected)     "Corporate VPN"      [com.apple.something]\n'
            '* (Disconnected)  "Personal VPN"       [com.apple.something]\n'
        )
        with patch("server.api.proxy_certs.subprocess.run", return_value=_mock_run(output)):
            assert _get_connected_vpns() == ["Corporate VPN"]

    def test_multiple_connected(self):
        output = (
            '* (Connected)     "VPN A"              [com.apple.something]\n'
            '* (Connected)     "VPN B"              [com.apple.something]\n'
            '* (Disconnected)  "VPN C"              [com.apple.something]\n'
        )
        with patch("server.api.proxy_certs.subprocess.run", return_value=_mock_run(output)):
            assert _get_connected_vpns() == ["VPN A", "VPN B"]

    def test_command_fails(self):
        with patch("server.api.proxy_certs.subprocess.run", return_value=_mock_run("", returncode=1)):
            assert _get_connected_vpns() == []

    def test_command_raises(self):
        with patch("server.api.proxy_certs.subprocess.run", side_effect=FileNotFoundError):
            assert _get_connected_vpns() == []


class TestGetDefaultRouteDevice:
    def test_normal_interface(self):
        output = (
            "   route to: default\n"
            "destination: default\n"
            "       mask: default\n"
            "    gateway: 192.168.1.1\n"
            "  interface: en0\n"
        )
        with patch("server.proxy.system_proxy.subprocess.run", return_value=_mock_run(output)):
            assert get_default_route_device() == "en0"

    def test_utun_interface(self):
        output = (
            "   route to: default\n"
            "destination: default\n"
            "       mask: default\n"
            "    gateway: 10.0.0.1\n"
            "  interface: utun3\n"
        )
        with patch("server.proxy.system_proxy.subprocess.run", return_value=_mock_run(output)):
            assert get_default_route_device() == "utun3"

    def test_command_fails(self):
        with patch("server.proxy.system_proxy.subprocess.run", return_value=_mock_run("", returncode=1)):
            assert get_default_route_device() is None


class TestDetectProxyWarnings:
    def test_no_issues(self):
        """No VPNs, normal interface → no warnings."""
        with patch("server.api.proxy_certs._get_connected_vpns", return_value=[]), \
             patch("server.api.proxy_certs.get_default_route_device", return_value="en0"):
            assert _detect_proxy_warnings() == []

    def test_connected_vpn(self):
        """Connected VPN → warning about traffic bypass."""
        with patch("server.api.proxy_certs._get_connected_vpns", return_value=["Work VPN"]), \
             patch("server.api.proxy_certs.get_default_route_device", return_value="en0"):
            warnings = _detect_proxy_warnings()
            assert len(warnings) == 1
            assert "Work VPN" in warnings[0]
            assert "connected" in warnings[0].lower()

    def test_utun_default_route_without_scutil_vpn(self):
        """Default route via utun but no scutil VPN → warn about tunnel + suggest disconnect."""
        with patch("server.api.proxy_certs._get_connected_vpns", return_value=[]), \
             patch("server.api.proxy_certs.get_default_route_device", return_value="utun3"):
            warnings = _detect_proxy_warnings()
            assert len(warnings) == 2
            assert "utun3" in warnings[0]
            assert "disconnect" in warnings[1].lower() or "split tunnel" in warnings[1].lower()

    def test_vpn_plus_utun_route(self):
        """VPN connected AND utun route → VPN warning + remediation advice (no redundant tunnel warning)."""
        with patch("server.api.proxy_certs._get_connected_vpns", return_value=["Corp VPN"]), \
             patch("server.api.proxy_certs.get_default_route_device", return_value="utun0"):
            warnings = _detect_proxy_warnings()
            # Should have: VPN detected warning + remediation suggestion
            assert any("Corp VPN" in w for w in warnings)
            assert any("disconnect" in w.lower() or "split tunnel" in w.lower() for w in warnings)

    def test_detection_failure_graceful(self):
        """If all detection fails, return empty list (no crash)."""
        with patch("server.api.proxy_certs._get_connected_vpns", return_value=[]), \
             patch("server.api.proxy_certs.get_default_route_device", return_value=None):
            assert _detect_proxy_warnings() == []
