"""Tests for system proxy snapshot, configure, and restore logic."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from server.proxy.system_proxy import (
    SystemProxySnapshot,
    _sanitize_stale_snapshot,
    configure_system_proxy,
    detect_and_configure,
    parse_networksetup_output,
    restore_from_state,
    restore_from_state_dict,
    restore_system_proxy,
    snapshot_system_proxy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_run(stdout: str = "", returncode: int = 0):
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


GETWEBPROXY_ENABLED = (
    "Enabled: Yes\n"
    "Server: 192.168.1.100\n"
    "Port: 8080\n"
    "Authenticated Proxy Enabled: 0\n"
)

GETWEBPROXY_DISABLED = (
    "Enabled: No\n"
    "Server: \n"
    "Port: 0\n"
    "Authenticated Proxy Enabled: 0\n"
)

GETSECUREWEBPROXY_ENABLED = (
    "Enabled: Yes\n"
    "Server: 192.168.1.100\n"
    "Port: 8443\n"
    "Authenticated Proxy Enabled: 0\n"
)

GETSECUREWEBPROXY_DISABLED = (
    "Enabled: No\n"
    "Server: \n"
    "Port: 0\n"
    "Authenticated Proxy Enabled: 0\n"
)


# ---------------------------------------------------------------------------
# parse_networksetup_output
# ---------------------------------------------------------------------------


class TestParseNetworksetupOutput:
    def test_enabled(self):
        enabled, server, port = parse_networksetup_output(GETWEBPROXY_ENABLED)
        assert enabled is True
        assert server == "192.168.1.100"
        assert port == 8080

    def test_disabled(self):
        enabled, server, port = parse_networksetup_output(GETWEBPROXY_DISABLED)
        assert enabled is False
        assert server == ""
        assert port == 0

    def test_malformed(self):
        enabled, server, port = parse_networksetup_output("garbage output\n")
        assert enabled is False
        assert server == ""
        assert port == 0

    def test_empty(self):
        enabled, server, port = parse_networksetup_output("")
        assert enabled is False
        assert server == ""
        assert port == 0

    def test_port_non_numeric(self):
        output = "Enabled: Yes\nServer: 1.2.3.4\nPort: abc\n"
        enabled, server, port = parse_networksetup_output(output)
        assert enabled is True
        assert server == "1.2.3.4"
        assert port == 0


# ---------------------------------------------------------------------------
# snapshot_system_proxy
# ---------------------------------------------------------------------------


class TestSnapshotSystemProxy:
    def test_reads_both_http_and_https(self):
        def side_effect(cmd, **kwargs):
            if "-getwebproxy" in cmd:
                return _mock_run(GETWEBPROXY_ENABLED)
            elif "-getsecurewebproxy" in cmd:
                return _mock_run(GETSECUREWEBPROXY_DISABLED)
            return _mock_run()

        with patch("server.proxy.system_proxy.subprocess.run", side_effect=side_effect):
            snap = snapshot_system_proxy("Wi-Fi")

        assert snap.interface == "Wi-Fi"
        assert snap.http_proxy_enabled is True
        assert snap.http_proxy_server == "192.168.1.100"
        assert snap.http_proxy_port == 8080
        assert snap.https_proxy_enabled is False
        assert snap.https_proxy_server == ""
        assert snap.https_proxy_port == 0
        assert snap.timestamp  # non-empty

    def test_both_disabled(self):
        def side_effect(cmd, **kwargs):
            return _mock_run(GETWEBPROXY_DISABLED)

        with patch("server.proxy.system_proxy.subprocess.run", side_effect=side_effect):
            snap = snapshot_system_proxy("Ethernet")

        assert snap.interface == "Ethernet"
        assert snap.http_proxy_enabled is False
        assert snap.https_proxy_enabled is False


# ---------------------------------------------------------------------------
# configure_system_proxy
# ---------------------------------------------------------------------------


class TestConfigureSystemProxy:
    def test_calls_both_set_commands(self):
        mock_run = MagicMock(return_value=_mock_run())
        with patch("server.proxy.system_proxy.subprocess.run", mock_run):
            configure_system_proxy("Wi-Fi", "127.0.0.1", 9101)

        assert mock_run.call_count == 2
        calls = mock_run.call_args_list
        assert calls[0][0][0] == [
            "networksetup", "-setwebproxy", "Wi-Fi", "127.0.0.1", "9101",
        ]
        assert calls[1][0][0] == [
            "networksetup", "-setsecurewebproxy", "Wi-Fi", "127.0.0.1", "9101",
        ]


# ---------------------------------------------------------------------------
# restore_system_proxy
# ---------------------------------------------------------------------------


class TestRestoreSystemProxy:
    def test_restore_both_disabled(self):
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False,
            http_proxy_server="",
            http_proxy_port=0,
            https_proxy_enabled=False,
            https_proxy_server="",
            https_proxy_port=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        mock_run = MagicMock(return_value=_mock_run())
        with patch("server.proxy.system_proxy.subprocess.run", mock_run):
            restore_system_proxy(snap)

        assert mock_run.call_count == 2
        calls = mock_run.call_args_list
        assert calls[0][0][0] == [
            "networksetup", "-setwebproxystate", "Wi-Fi", "off",
        ]
        assert calls[1][0][0] == [
            "networksetup", "-setsecurewebproxystate", "Wi-Fi", "off",
        ]

    def test_restore_both_enabled(self):
        snap = SystemProxySnapshot(
            interface="Ethernet",
            http_proxy_enabled=True,
            http_proxy_server="10.0.0.1",
            http_proxy_port=3128,
            https_proxy_enabled=True,
            https_proxy_server="10.0.0.1",
            https_proxy_port=3129,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        mock_run = MagicMock(return_value=_mock_run())
        with patch("server.proxy.system_proxy.subprocess.run", mock_run):
            restore_system_proxy(snap)

        assert mock_run.call_count == 2
        calls = mock_run.call_args_list
        assert calls[0][0][0] == [
            "networksetup", "-setwebproxy", "Ethernet", "10.0.0.1", "3128",
        ]
        assert calls[1][0][0] == [
            "networksetup", "-setsecurewebproxy", "Ethernet", "10.0.0.1", "3129",
        ]

    def test_restore_mixed(self):
        """HTTP enabled, HTTPS disabled."""
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=True,
            http_proxy_server="proxy.corp.com",
            http_proxy_port=8080,
            https_proxy_enabled=False,
            https_proxy_server="",
            https_proxy_port=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        mock_run = MagicMock(return_value=_mock_run())
        with patch("server.proxy.system_proxy.subprocess.run", mock_run):
            restore_system_proxy(snap)

        calls = mock_run.call_args_list
        assert calls[0][0][0] == [
            "networksetup", "-setwebproxy", "Wi-Fi", "proxy.corp.com", "8080",
        ]
        assert calls[1][0][0] == [
            "networksetup", "-setsecurewebproxystate", "Wi-Fi", "off",
        ]


# ---------------------------------------------------------------------------
# SystemProxySnapshot serialization
# ---------------------------------------------------------------------------


class TestSnapshotSerialization:
    def test_roundtrip(self):
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=True,
            http_proxy_server="1.2.3.4",
            http_proxy_port=8080,
            https_proxy_enabled=False,
            https_proxy_server="",
            https_proxy_port=0,
            timestamp="2026-02-15T00:00:00+00:00",
        )
        d = snap.to_dict()
        assert isinstance(d, dict)
        restored = SystemProxySnapshot.from_dict(d)
        assert restored == snap

    def test_json_roundtrip(self):
        snap = SystemProxySnapshot(
            interface="Ethernet",
            http_proxy_enabled=False,
            http_proxy_server="",
            http_proxy_port=0,
            https_proxy_enabled=False,
            https_proxy_server="",
            https_proxy_port=0,
            timestamp="2026-02-15T12:00:00+00:00",
        )
        serialized = json.dumps(snap.to_dict())
        deserialized = SystemProxySnapshot.from_dict(json.loads(serialized))
        assert deserialized == snap


# ---------------------------------------------------------------------------
# _sanitize_stale_snapshot
# ---------------------------------------------------------------------------


class TestSanitizeStaleSnapshot:
    def test_stale_quern_config_treated_as_disabled(self):
        """If snapshot shows Quern's own proxy, treat as disabled."""
        snap = SystemProxySnapshot(
            interface="Ethernet",
            http_proxy_enabled=True,
            http_proxy_server="127.0.0.1",
            http_proxy_port=9101,
            https_proxy_enabled=True,
            https_proxy_server="127.0.0.1",
            https_proxy_port=9101,
            timestamp="t",
        )
        result = _sanitize_stale_snapshot(snap, 9101)
        assert result.http_proxy_enabled is False
        assert result.http_proxy_server == ""
        assert result.http_proxy_port == 0
        assert result.https_proxy_enabled is False
        assert result.https_proxy_server == ""
        assert result.https_proxy_port == 0
        assert result.interface == "Ethernet"

    def test_clean_snapshot_unchanged(self):
        """Non-Quern proxy settings should be preserved."""
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=True,
            http_proxy_server="10.0.0.1",
            http_proxy_port=3128,
            https_proxy_enabled=False,
            https_proxy_server="",
            https_proxy_port=0,
            timestamp="t",
        )
        result = _sanitize_stale_snapshot(snap, 9101)
        assert result is snap  # unchanged, same object

    def test_disabled_snapshot_unchanged(self):
        """Already-disabled proxy should pass through."""
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False,
            http_proxy_server="",
            http_proxy_port=0,
            https_proxy_enabled=False,
            https_proxy_server="",
            https_proxy_port=0,
            timestamp="t",
        )
        result = _sanitize_stale_snapshot(snap, 9101)
        assert result is snap

    def test_different_port_not_treated_as_stale(self):
        """127.0.0.1 on a different port is not Quern's config."""
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=True,
            http_proxy_server="127.0.0.1",
            http_proxy_port=8080,
            https_proxy_enabled=True,
            https_proxy_server="127.0.0.1",
            https_proxy_port=8080,
            timestamp="t",
        )
        result = _sanitize_stale_snapshot(snap, 9101)
        assert result is snap


# ---------------------------------------------------------------------------
# detect_and_configure
# ---------------------------------------------------------------------------


class TestDetectAndConfigure:
    def test_success(self):
        with patch("server.proxy.system_proxy.detect_active_interface", return_value="Wi-Fi"), \
             patch("server.proxy.system_proxy.snapshot_system_proxy") as mock_snap, \
             patch("server.proxy.system_proxy.configure_system_proxy") as mock_config:
            snap = SystemProxySnapshot(
                interface="Wi-Fi",
                http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
                https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
                timestamp="t",
            )
            mock_snap.return_value = snap

            result = detect_and_configure(9101)

        assert result is snap
        mock_snap.assert_called_once_with("Wi-Fi")
        mock_config.assert_called_once_with("Wi-Fi", "127.0.0.1", 9101)

    def test_interface_override(self):
        with patch("server.proxy.system_proxy.detect_active_interface") as mock_detect, \
             patch("server.proxy.system_proxy.snapshot_system_proxy") as mock_snap, \
             patch("server.proxy.system_proxy.configure_system_proxy"):
            snap = SystemProxySnapshot(
                interface="Ethernet",
                http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
                https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
                timestamp="t",
            )
            mock_snap.return_value = snap

            result = detect_and_configure(9101, interface="Ethernet")

        mock_detect.assert_not_called()
        mock_snap.assert_called_once_with("Ethernet")
        assert result is snap

    def test_detection_failure_returns_none(self):
        with patch("server.proxy.system_proxy.detect_active_interface", return_value=None):
            result = detect_and_configure(9101)
        assert result is None


# ---------------------------------------------------------------------------
# restore_from_state / restore_from_state_dict
# ---------------------------------------------------------------------------


class TestRestoreFromState:
    def test_no_state(self):
        with patch("server.lifecycle.state.read_state", return_value=None):
            assert restore_from_state() is False

    def test_not_configured(self):
        with patch("server.lifecycle.state.read_state", return_value={"system_proxy_configured": False}):
            assert restore_from_state() is False

    def test_configured_but_no_snapshot(self):
        state = {"system_proxy_configured": True, "system_proxy_snapshot": None}
        with patch("server.lifecycle.state.read_state", return_value=state):
            assert restore_from_state() is False

    def test_configured_with_snapshot(self):
        snap_dict = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        ).to_dict()
        state = {
            "system_proxy_configured": True,
            "system_proxy_snapshot": snap_dict,
        }
        with patch("server.lifecycle.state.read_state", return_value=state), \
             patch("server.proxy.system_proxy.restore_system_proxy") as mock_restore:
            assert restore_from_state() is True
        mock_restore.assert_called_once()

    def test_restore_from_state_dict_configured(self):
        snap_dict = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        ).to_dict()
        state = {
            "system_proxy_configured": True,
            "system_proxy_snapshot": snap_dict,
        }
        with patch("server.proxy.system_proxy.restore_system_proxy") as mock_restore:
            assert restore_from_state_dict(state) is True
        mock_restore.assert_called_once()

    def test_restore_from_state_dict_not_configured(self):
        assert restore_from_state_dict({"system_proxy_configured": False}) is False
        assert restore_from_state_dict({}) is False

    def test_restore_from_state_dict_exception_returns_false(self):
        state = {
            "system_proxy_configured": True,
            "system_proxy_snapshot": {"bad": "data"},
        }
        # from_dict will fail with TypeError due to missing fields
        assert restore_from_state_dict(state) is False


# ---------------------------------------------------------------------------
# API endpoint tests (mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_proxy_adapter():
    adapter = MagicMock()
    adapter.is_running = True
    adapter.listen_port = 9101
    adapter.listen_host = "0.0.0.0"
    adapter.started_at = datetime.now(timezone.utc)
    adapter._error = None
    adapter._intercept_pattern = None
    adapter._held_flows = {}
    adapter._mock_rules = []
    return adapter


@pytest.fixture
def mock_flow_store():
    store = MagicMock()
    store.size = 0
    return store


@pytest.fixture
def app_with_proxy(mock_proxy_adapter, mock_flow_store):
    """Create a minimal FastAPI app with proxy adapter for testing."""
    from fastapi import FastAPI
    from server.api.proxy import router

    app = FastAPI()
    app.include_router(router)
    app.state.proxy_adapter = mock_proxy_adapter
    app.state.flow_store = mock_flow_store
    return app


@pytest.fixture
def client(app_with_proxy):
    from fastapi.testclient import TestClient
    return TestClient(app_with_proxy)


class TestStartProxySystemProxy:
    def test_start_with_system_proxy_true(self, client, mock_proxy_adapter):
        mock_proxy_adapter.is_running = False

        async def fake_start():
            mock_proxy_adapter.is_running = True

        mock_proxy_adapter.start = fake_start

        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        )
        with patch("server.api.proxy.detect_and_configure", return_value=snap), \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/start", json={"system_proxy": True})

        assert resp.status_code == 200
        data = resp.json()
        assert data["system_proxy"]["configured"] is True
        assert data["system_proxy"]["interface"] == "Wi-Fi"

    def test_start_with_system_proxy_false(self, client, mock_proxy_adapter):
        mock_proxy_adapter.is_running = False

        async def fake_start():
            mock_proxy_adapter.is_running = True

        mock_proxy_adapter.start = fake_start

        with patch("server.api.proxy.detect_and_configure") as mock_detect, \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/start", json={"system_proxy": False})

        assert resp.status_code == 200
        mock_detect.assert_not_called()
        assert resp.json()["system_proxy"] is None

    def test_start_default_no_system_proxy(self, client, mock_proxy_adapter):
        """Test that start_proxy() without params does NOT configure system proxy."""
        mock_proxy_adapter.is_running = False

        async def fake_start():
            mock_proxy_adapter.is_running = True

        mock_proxy_adapter.start = fake_start

        with patch("server.api.proxy.detect_and_configure") as mock_detect, \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/start")  # No body

        assert resp.status_code == 200
        mock_detect.assert_not_called()  # Should NOT be called
        assert resp.json()["system_proxy"] is None


class TestStopProxySystemProxy:
    def test_stop_restores_system_proxy(self, client, mock_proxy_adapter):
        async def fake_stop():
            mock_proxy_adapter.is_running = False

        mock_proxy_adapter.stop = fake_stop

        snap_dict = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        ).to_dict()
        state = {
            "system_proxy_configured": True,
            "system_proxy_snapshot": snap_dict,
        }

        with patch("server.lifecycle.state.read_state", return_value=state), \
             patch("server.api.proxy.restore_system_proxy") as mock_restore, \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/stop")

        assert resp.status_code == 200
        mock_restore.assert_called_once()
        data = resp.json()
        assert data["system_proxy_restore"]["restored"] is True

    def test_stop_no_system_proxy_configured(self, client, mock_proxy_adapter):
        async def fake_stop():
            mock_proxy_adapter.is_running = False

        mock_proxy_adapter.stop = fake_stop

        with patch("server.lifecycle.state.read_state", return_value={"system_proxy_configured": False}), \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/stop")

        assert resp.status_code == 200
        assert resp.json()["system_proxy_restore"] is None


class TestConfigureSystemEndpoint:
    def test_configure_success(self, client, mock_proxy_adapter):
        snap = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        )
        with patch("server.lifecycle.state.read_state", return_value={"system_proxy_configured": False}), \
             patch("server.api.proxy.detect_and_configure", return_value=snap), \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/configure-system")

        assert resp.status_code == 200
        assert resp.json()["configured"] is True
        assert resp.json()["interface"] == "Wi-Fi"

    def test_configure_already_configured(self, client, mock_proxy_adapter):
        with patch("server.lifecycle.state.read_state", return_value={"system_proxy_configured": True}):
            resp = client.post("/api/v1/proxy/configure-system")

        assert resp.status_code == 409

    def test_configure_proxy_not_running(self, client, mock_proxy_adapter):
        mock_proxy_adapter.is_running = False
        resp = client.post("/api/v1/proxy/configure-system")
        assert resp.status_code == 503

    def test_configure_detection_failure(self, client, mock_proxy_adapter):
        with patch("server.lifecycle.state.read_state", return_value={"system_proxy_configured": False}), \
             patch("server.api.proxy.detect_and_configure", return_value=None):
            resp = client.post("/api/v1/proxy/configure-system")

        assert resp.status_code == 500


class TestUnconfigureSystemEndpoint:
    def test_unconfigure_success(self, client):
        snap_dict = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        ).to_dict()
        state = {
            "system_proxy_configured": True,
            "system_proxy_snapshot": snap_dict,
        }
        with patch("server.lifecycle.state.read_state", return_value=state), \
             patch("server.api.proxy.restore_system_proxy") as mock_restore, \
             patch("server.api.proxy.update_state"):
            resp = client.post("/api/v1/proxy/unconfigure-system")

        assert resp.status_code == 200
        mock_restore.assert_called_once()
        assert resp.json()["restored"] is True

    def test_unconfigure_not_configured(self, client):
        with patch("server.lifecycle.state.read_state", return_value={"system_proxy_configured": False}):
            resp = client.post("/api/v1/proxy/unconfigure-system")
        assert resp.status_code == 409

    def test_unconfigure_no_state(self, client):
        with patch("server.lifecycle.state.read_state", return_value=None):
            resp = client.post("/api/v1/proxy/unconfigure-system")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_stale_state_triggers_restore(self):
        """Simulate _cmd_start finding stale state with system proxy configured."""
        snap_dict = SystemProxySnapshot(
            interface="Wi-Fi",
            http_proxy_enabled=False, http_proxy_server="", http_proxy_port=0,
            https_proxy_enabled=False, https_proxy_server="", https_proxy_port=0,
            timestamp="t",
        ).to_dict()
        state = {
            "system_proxy_configured": True,
            "system_proxy_snapshot": snap_dict,
        }
        with patch("server.proxy.system_proxy.restore_system_proxy") as mock_restore:
            result = restore_from_state_dict(state)
        assert result is True
        mock_restore.assert_called_once()
