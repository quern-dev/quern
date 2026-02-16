"""Tests for certificate management API endpoints."""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.main import create_app
from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType
from server.lifecycle.state import write_state, ServerState


@pytest.fixture
def app():
    """Create a test FastAPI app with test API key."""
    config = ServerConfig(api_key="test-key-12345")
    app = create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)
    # Mock the device controller
    app.state.device_controller = MagicMock()
    app.state.proxy_adapter = None
    app.state.flow_store = None
    return app


@pytest.fixture
def auth_headers():
    """Authentication headers for API requests."""
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def mock_cert_path(tmp_path, monkeypatch):
    """Mock the cert path to use tmp_path."""
    cert_path = tmp_path / "mitmproxy-ca-cert.pem"
    cert_path.write_text("FAKE CERT DATA")

    def mock_get_cert_path():
        return cert_path

    monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", mock_get_cert_path)
    return cert_path


@pytest.fixture
def mock_state_file(tmp_path, monkeypatch):
    """Mock state file location."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr("server.lifecycle.state.STATE_FILE", state_file)
    return state_file


class TestCertStatus:
    def test_cert_status_cert_exists(self, client, auth_headers, mock_cert_path, mock_state_file):
        """Test GET /cert/status when cert exists."""
        with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
            with patch("server.lifecycle.state.read_state") as mock_read_state:
                mock_read_state.return_value = {
                    "device_certs": {
                        "test-udid": {
                            "name": "iPhone 16 Pro",
                            "cert_installed": True,
                            "fingerprint": "abc123",
                            "verified_at": datetime.now(timezone.utc).isoformat(),
                        }
                    }
                }

                response = client.get("/api/v1/proxy/cert/status", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["cert_exists"] is True
        assert data["fingerprint"] == "abc123"
        assert "test-udid" in data["devices"]
        assert data["devices"]["test-udid"]["cert_installed"] is True

    def test_cert_status_cert_missing(self, client, auth_headers, tmp_path, monkeypatch, mock_state_file):
        """Test GET /cert/status when cert doesn't exist."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        def mock_get_cert_path():
            return nonexistent_cert

        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", mock_get_cert_path)

        with patch("server.lifecycle.state.read_state", return_value=None):
            response = client.get("/api/v1/proxy/cert/status", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["cert_exists"] is False
        assert data["fingerprint"] is None

    def test_cert_status_no_devices(self, client, auth_headers, mock_cert_path, mock_state_file):
        """Test GET /cert/status when no devices in state."""
        with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
            with patch("server.lifecycle.state.read_state", return_value=None):
                response = client.get("/api/v1/proxy/cert/status", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["cert_exists"] is True
        assert data["devices"] == {}


class TestCertVerify:
    @pytest.mark.asyncio
    async def test_cert_verify_specific_device(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/verify with specific UDID."""
        app.state.device_controller.list_devices = AsyncMock(return_value=[
            DeviceInfo(
                udid="test-udid",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
            )
        ])

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="iPhone 16 Pro",
                cert_installed=True,
                fingerprint="abc123",
                verified_at=datetime.now(timezone.utc).isoformat(),
            )

            response = client.post(
                "/api/v1/proxy/cert/verify",
                json={"udid": "test-udid"},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["verified"] is True
        assert len(data["devices"]) == 1
        assert data["devices"][0]["udid"] == "test-udid"
        assert data["devices"][0]["cert_installed"] is True

    @pytest.mark.asyncio
    async def test_cert_verify_all_booted(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/verify with no UDID (all booted devices)."""
        app.state.device_controller.list_devices = AsyncMock(return_value=[
            DeviceInfo(
                udid="test-udid-1",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
            ),
            DeviceInfo(
                udid="test-udid-2",
                name="iPad Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
            ),
            DeviceInfo(
                udid="test-udid-3",
                name="iPhone 15",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
            ),
        ])

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="Test Device",
                cert_installed=True,
                fingerprint="abc123",
                verified_at=datetime.now(timezone.utc).isoformat(),
            )

            response = client.post(
                "/api/v1/proxy/cert/verify",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        # Should only verify the 2 booted devices
        assert len(data["devices"]) == 2
        assert {d["udid"] for d in data["devices"]} == {"test-udid-1", "test-udid-2"}

    def test_cert_verify_no_controller(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/verify when device controller not initialized."""
        app.state.device_controller = None

        response = client.post(
            "/api/v1/proxy/cert/verify",
            json={"udid": "test-udid"},
            headers=auth_headers,
        )

        assert response.status_code == 503
        assert "Device controller not initialized" in response.json()["detail"]


class TestCertInstall:
    @pytest.mark.asyncio
    async def test_cert_install_specific_device(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/install with specific UDID."""
        app.state.device_controller.list_devices = AsyncMock()

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = True  # Newly installed

            response = client.post(
                "/api/v1/proxy/cert/install",
                json={"udid": "test-udid"},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["succeeded"] == 1
        assert data["failed"] == 0
        assert data["devices"][0]["status"] == "installed"
        mock_install.assert_called_once()

    @pytest.mark.asyncio
    async def test_cert_install_already_installed(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/install when cert already installed."""
        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = False  # Already installed

            response = client.post(
                "/api/v1/proxy/cert/install",
                json={"udid": "test-udid"},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["devices"][0]["status"] == "already_installed"

    @pytest.mark.asyncio
    async def test_cert_install_all_booted(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/install with no UDID (all booted devices)."""
        app.state.device_controller.list_devices = AsyncMock(return_value=[
            DeviceInfo(
                udid="test-udid-1",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
            ),
            DeviceInfo(
                udid="test-udid-2",
                name="iPad Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
            ),
        ])

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = True

            response = client.post(
                "/api/v1/proxy/cert/install",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["succeeded"] == 2
        assert mock_install.call_count == 2

    @pytest.mark.asyncio
    async def test_cert_install_force(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/install with force=True."""
        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = True

            response = client.post(
                "/api/v1/proxy/cert/install",
                json={"udid": "test-udid", "force": True},
                headers=auth_headers,
            )

        assert response.status_code == 200
        mock_install.assert_called_once_with(app.state.device_controller, "test-udid", force=True)

    @pytest.mark.asyncio
    async def test_cert_install_failure(self, client, auth_headers, mock_cert_path, mock_state_file, app):
        """Test POST /cert/install when installation fails."""
        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.side_effect = Exception("simctl failed")

            response = client.post(
                "/api/v1/proxy/cert/install",
                json={"udid": "test-udid"},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["succeeded"] == 0
        assert data["failed"] == 1
        assert data["devices"][0]["status"] == "failed"
        assert "simctl failed" in data["devices"][0]["error"]
