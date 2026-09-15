"""Tests for certificate management API endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.main import create_app
from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType


@pytest.fixture
def app():
    """Create a test FastAPI app with test API key."""
    config = ServerConfig(api_key="test-key-12345")
    app = create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)
    # Mock the device controller. Set _active_udid to None explicitly —
    # without this, MagicMock attribute access returns a truthy mock, which
    # breaks code paths that check for an active device.
    app.state.device_controller = MagicMock()
    app.state.device_controller._active_udid = None
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
def mock_cert_state(tmp_path, monkeypatch):
    """Mock cert state file location."""
    cert_state_file = tmp_path / "cert-state.json"
    monkeypatch.setattr("server.proxy.cert_state.CERT_STATE_FILE", cert_state_file)
    monkeypatch.setattr("server.proxy.cert_state.CONFIG_DIR", tmp_path)
    return cert_state_file


class TestCertStatus:
    def test_cert_status_cert_exists(self, client, auth_headers, mock_cert_path, mock_cert_state):
        """Test GET /cert/status when cert exists."""
        with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
            with patch("server.api.proxy_certs.read_cert_state") as mock_read:
                mock_read.return_value = {
                    "test-udid": {
                        "name": "iPhone 16 Pro",
                        "cert_installed": True,
                        "fingerprint": "abc123",
                        "verified_at": datetime.now(UTC).isoformat(),
                    }
                }

                response = client.get("/api/v1/proxy/cert/status", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["cert_exists"] is True
        assert data["fingerprint"] == "abc123"
        assert "test-udid" in data["devices"]
        assert data["devices"]["test-udid"]["cert_installed"] is True

    def test_cert_status_cert_missing(
        self, client, auth_headers, tmp_path, monkeypatch, mock_cert_state
    ):
        """Test GET /cert/status when cert doesn't exist."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        def mock_get_cert_path():
            return nonexistent_cert

        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", mock_get_cert_path)

        with patch("server.api.proxy_certs.read_cert_state", return_value={}):
            response = client.get("/api/v1/proxy/cert/status", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["cert_exists"] is False
        assert data["fingerprint"] is None

    def test_cert_status_no_devices(self, client, auth_headers, mock_cert_path, mock_cert_state):
        """Test GET /cert/status when no devices in state."""
        with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
            with patch("server.api.proxy_certs.read_cert_state", return_value={}):
                response = client.get("/api/v1/proxy/cert/status", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["cert_exists"] is True
        assert data["devices"] == {}


class TestCertVerify:
    @pytest.mark.asyncio
    async def test_cert_verify_specific_device(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/verify with specific UDID."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="test-udid",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                )
            ]
        )

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="iPhone 16 Pro",
                cert_installed=True,
                fingerprint="abc123",
                verified_at=datetime.now(UTC).isoformat(),
            )
            with patch(
                "server.proxy.cert_manager.check_truststore_status", return_value="installed"
            ):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch(
                        "server.api.proxy_certs.read_cert_state_for_device", return_value=None
                    ):
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
        assert data["devices"][0]["status"] == "installed"
        assert data["erased_devices"] == []

    @pytest.mark.asyncio
    async def test_cert_verify_all_simulators(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/verify with state=None to get all simulators (booted + shutdown)."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
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
            ]
        )

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="Test Device",
                cert_installed=True,
                fingerprint="abc123",
                verified_at=datetime.now(UTC).isoformat(),
            )
            with patch(
                "server.proxy.cert_manager.check_truststore_status", return_value="installed"
            ):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch(
                        "server.api.proxy_certs.read_cert_state_for_device", return_value=None
                    ):
                        response = client.post(
                            "/api/v1/proxy/cert/verify",
                            json={"state": None, "device_type": "simulator"},
                            headers=auth_headers,
                        )

        assert response.status_code == 200
        data = response.json()
        # Should verify ALL 3 simulators (booted + shutdown) when state=None
        assert len(data["devices"]) == 3
        assert {d["udid"] for d in data["devices"]} == {"test-udid-1", "test-udid-2", "test-udid-3"}

    @pytest.mark.asyncio
    async def test_cert_verify_booted_simulators_only(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/verify with state='booted' and device_type='simulator' (defaults)."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="booted-sim",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
                DeviceInfo(
                    udid="shutdown-sim",
                    name="iPhone 15",
                    state=DeviceState.SHUTDOWN,
                    device_type=DeviceType.SIMULATOR,
                ),
                DeviceInfo(
                    udid="physical-dev",
                    name="John's iPhone",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                ),
            ]
        )

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="Test Device",
                cert_installed=True,
                fingerprint="abc123",
                verified_at=datetime.now(UTC).isoformat(),
            )
            with patch(
                "server.proxy.cert_manager.check_truststore_status", return_value="installed"
            ):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch(
                        "server.api.proxy_certs.read_cert_state_for_device", return_value=None
                    ):
                        response = client.post(
                            "/api/v1/proxy/cert/verify",
                            json={"state": "booted", "device_type": "simulator"},
                            headers=auth_headers,
                        )

        assert response.status_code == 200
        data = response.json()
        # Should only verify booted simulator, not shutdown sim or physical device
        assert len(data["devices"]) == 1
        assert data["devices"][0]["udid"] == "booted-sim"

    @pytest.mark.asyncio
    async def test_cert_verify_all_states(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/verify with state=None returns all matching device_type."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="booted-sim",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
                DeviceInfo(
                    udid="shutdown-sim",
                    name="iPhone 15",
                    state=DeviceState.SHUTDOWN,
                    device_type=DeviceType.SIMULATOR,
                ),
            ]
        )

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="Test Device",
                cert_installed=True,
                fingerprint="abc123",
                verified_at=datetime.now(UTC).isoformat(),
            )
            with patch(
                "server.proxy.cert_manager.check_truststore_status", return_value="installed"
            ):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch(
                        "server.api.proxy_certs.read_cert_state_for_device", return_value=None
                    ):
                        response = client.post(
                            "/api/v1/proxy/cert/verify",
                            json={"state": None, "device_type": "simulator"},
                            headers=auth_headers,
                        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["devices"]) == 2

    @pytest.mark.asyncio
    async def test_cert_verify_shutdown_device(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/verify works for shutdown device."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="shutdown-udid",
                    name="iPhone 15",
                    state=DeviceState.SHUTDOWN,
                    device_type=DeviceType.SIMULATOR,
                ),
            ]
        )

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="iPhone 15",
                cert_installed=False,
                fingerprint=None,
                verified_at=datetime.now(UTC).isoformat(),
            )
            with patch(
                "server.proxy.cert_manager.check_truststore_status", return_value="never_booted"
            ):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch(
                        "server.api.proxy_certs.read_cert_state_for_device", return_value=None
                    ):
                        response = client.post(
                            "/api/v1/proxy/cert/verify",
                            json={"udid": "shutdown-udid"},
                            headers=auth_headers,
                        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["devices"]) == 1
        assert data["devices"][0]["status"] == "never_booted"

    @pytest.mark.asyncio
    async def test_cert_verify_erase_detection(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/verify detects erased devices."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="erased-udid",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
            ]
        )

        # Previous state says cert was installed
        prev_state = {
            "name": "iPhone 16 Pro",
            "cert_installed": True,
            "fingerprint": "abc123",
        }

        with patch("server.proxy.cert_manager.get_device_cert_state") as mock_get_state:
            mock_get_state.return_value = DeviceCertState(
                name="iPhone 16 Pro",
                cert_installed=False,  # Now it's gone
                fingerprint=None,
                verified_at=datetime.now(UTC).isoformat(),
            )
            with patch(
                "server.proxy.cert_manager.check_truststore_status", return_value="not_installed"
            ):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch(
                        "server.api.proxy_certs.read_cert_state_for_device", return_value=prev_state
                    ):
                        response = client.post(
                            "/api/v1/proxy/cert/verify",
                            json={"udid": "erased-udid"},
                            headers=auth_headers,
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["erased_devices"] == ["erased-udid"]

    def test_cert_verify_no_controller(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
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
    async def test_cert_install_specific_device(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
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
    async def test_cert_install_already_installed(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/install when cert already installed."""
        app.state.device_controller.list_devices = AsyncMock()
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
    async def test_cert_install_all_booted(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/install with no UDID (all booted devices)."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
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
            ]
        )

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
    async def test_cert_install_force(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/install with force=True."""
        app.state.device_controller.list_devices = AsyncMock()
        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = True

            response = client.post(
                "/api/v1/proxy/cert/install",
                json={"udid": "test-udid", "force": True},
                headers=auth_headers,
            )

        assert response.status_code == 200
        mock_install.assert_called_once()

    @pytest.mark.asyncio
    async def test_cert_install_failure(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Test POST /cert/install when installation fails."""
        app.state.device_controller.list_devices = AsyncMock()
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

    @pytest.mark.asyncio
    async def test_cert_install_excludes_physical_devices(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """No-UDID install must skip physical iOS/Android in the booted set.

        Physical iOS devices report DeviceState.BOOTED whenever connected
        (including wifi-only pairing). Without filtering, the no-UDID batch
        path would fall into simctl keychain on physicals, which fails with
        a cryptic 'Invalid device'.
        """
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="sim-1",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
                DeviceInfo(
                    udid="phys-1",
                    name="J iPhone 15 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                ),
                DeviceInfo(
                    udid="emu-1",
                    name="Pixel_7_API33",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.ANDROID_EMULATOR,
                ),
                DeviceInfo(
                    udid="and-phys-1",
                    name="Pixel 8 (USB)",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.ANDROID_DEVICE,
                ),
            ]
        )

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
        installed_udids = {d["udid"] for d in data["devices"]}
        assert installed_udids == {"sim-1", "emu-1"}
        assert mock_install.call_count == 2

    @pytest.mark.asyncio
    async def test_cert_install_explicit_physical_udid_rejected(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """Explicit physical-device UDID gets a clear 400, not simctl noise."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="phys-1",
                    name="J iPhone 15 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                ),
            ]
        )

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            response = client.post(
                "/api/v1/proxy/cert/install",
                json={"udid": "phys-1"},
                headers=auth_headers,
            )

        assert response.status_code == 400
        assert "VPN & Device Management" in response.json()["detail"]
        mock_install.assert_not_called()

    @pytest.mark.asyncio
    async def test_cert_install_no_args_does_not_422(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """No-args POST must not 422 — body is optional.

        The MCP wrapper omits the body when no fields are passed; the route
        previously required a body and rejected this with HTTP 422.
        """
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="sim-1",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
            ]
        )
        # No active device set (fixture default)

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = True
            response = client.post(
                "/api/v1/proxy/cert/install",
                headers=auth_headers,
                # No json= argument at all
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_cert_install_uses_active_device(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """No-UDID call should use the active device when one is set.

        Matches the convention used by tap_element/take_screenshot/etc.
        """
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="sim-active",
                    name="iPhone 17 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
                DeviceInfo(
                    udid="sim-other",
                    name="iPhone 16",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
            ]
        )
        app.state.device_controller._active_udid = "sim-active"

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            mock_install.return_value = True
            response = client.post(
                "/api/v1/proxy/cert/install",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["devices"][0]["udid"] == "sim-active"
        # Should not have touched the other booted simulator
        mock_install.assert_called_once()

    @pytest.mark.asyncio
    async def test_cert_install_active_physical_device_rejected(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """If the active device is a physical iOS device, refuse with the
        same clear guidance as the explicit-UDID case — don't silently fall
        back to all-booted, since the user explicitly chose this target.
        """
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="phys-1",
                    name="J iPhone 15 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                ),
                DeviceInfo(
                    udid="sim-1",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                ),
            ]
        )
        app.state.device_controller._active_udid = "phys-1"

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            response = client.post(
                "/api/v1/proxy/cert/install",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 400
        assert "VPN & Device Management" in response.json()["detail"]
        mock_install.assert_not_called()

    @pytest.mark.asyncio
    async def test_cert_install_no_eligible_devices(
        self, client, auth_headers, mock_cert_path, mock_cert_state, app
    ):
        """No-UDID install with only physicals booted returns 400, not 200."""
        app.state.device_controller.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="phys-1",
                    name="J iPhone 15 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                ),
            ]
        )

        with patch("server.proxy.cert_manager.install_cert") as mock_install:
            response = client.post(
                "/api/v1/proxy/cert/install",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 400
        assert "Physical devices are not eligible" in response.json()["detail"]
        mock_install.assert_not_called()


class TestCertStatusIsVerifiedNotRecalled:
    """The two endpoints that rendered a stored record as current fact.

    Erasing a simulator recreates its TrustStore empty and leaves quern's
    record saying the cert is installed. A field report read that record,
    reasonably believed it, and spent the next hour concluding that staging
    authentication was down.

    Driven through the real endpoints, because the bug was in the call site
    rather than in anything underneath it -- a test that stubs the decision and
    asserts on the stub cannot see it.
    """

    def _erased(self, monkeypatch):
        """A device quern recorded as trusting the CA, that no longer does."""
        monkeypatch.setattr(
            "server.proxy.cert_state.read_cert_state",
            lambda: {"AAAA": {"name": "iPhone 16 Pro", "cert_installed": True}},
        )

        async def not_trusted(_c, _udid, verify=False, *, device_name=None):
            return False

        monkeypatch.setattr(
            "server.proxy.cert_manager.is_cert_installed", not_trusted
        )

    def test_the_device_filter_excludes_an_erased_simulator(
        self, client, auth_headers, app, monkeypatch
    ):
        """`?cert_installed=true` both labels and filters.

        Getting an erased device back from that query is the answer being
        wrong, not merely stale — the caller asked which devices trust the CA.
        """
        from server.models import DeviceInfo, DeviceState, DeviceType

        booted = DeviceInfo(
            udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR, os_version="iOS 18.6", runtime="",
        )
        app.state.device_controller.list_devices = AsyncMock(return_value=[booted])
        app.state.device_controller.check_tools = AsyncMock(return_value={})
        self._erased(monkeypatch)

        r = client.get("/api/v1/device/list?cert_installed=true", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["devices"] == [], "an erased simulator was reported as trusting the CA"

    def test_the_device_filter_still_finds_a_trusting_simulator(
        self, client, auth_headers, app, monkeypatch
    ):
        # The other direction, so the fix cannot be "always report false".
        from server.models import DeviceInfo, DeviceState, DeviceType

        booted = DeviceInfo(
            udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR, os_version="iOS 18.6", runtime="",
        )
        app.state.device_controller.list_devices = AsyncMock(return_value=[booted])
        app.state.device_controller.check_tools = AsyncMock(return_value={})
        monkeypatch.setattr(
            "server.proxy.cert_state.read_cert_state", lambda: {}
        )

        async def trusted(_c, _udid, verify=False, *, device_name=None):
            return True

        monkeypatch.setattr("server.proxy.cert_manager.is_cert_installed", trusted)

        r = client.get("/api/v1/device/list?cert_installed=true", headers=auth_headers)
        assert [d["udid"] for d in r.json()["devices"]] == ["AAAA"]

    def test_a_fresh_record_does_not_shield_an_erased_simulator(
        self, client, auth_headers, app, monkeypatch, tmp_path
    ):
        """The filter must ask the TrustStore, not the hour-long cache.

        Every other test in this class patches `is_cert_installed` outright,
        so none of them can see the cache at all -- and the cache is the
        defect: without `verify=True` a record written minutes ago is returned
        unchecked, and `?cert_installed=true` hands back a device erased since.
        Same bug as the preflight had, in the endpoint that *filters* on it.

        Seam is `verify_cert_in_truststore`, the ground-truth oracle.
        """
        from datetime import UTC, datetime

        from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType
        from server.proxy.cert_manager import update_cert_state

        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("contents unread: the fingerprint is stubbed")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        monkeypatch.setattr(
            "server.proxy.cert_manager.get_cert_fingerprint", lambda _p: "a" * 64,
        )

        # What quern recorded minutes ago, before the erase.
        update_cert_state("AAAA", DeviceCertState(
            name="iPhone 16 Pro", cert_installed=True, fingerprint="a" * 64,
            verified_at=datetime.now(UTC).isoformat(),
        ).model_dump())

        booted = DeviceInfo(
            udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR, os_version="iOS 18.6", runtime="",
        )
        app.state.device_controller.list_devices = AsyncMock(return_value=[booted])
        app.state.device_controller.check_tools = AsyncMock(return_value={})
        app.state.device_controller._is_android = lambda _u: False

        truststore = MagicMock(return_value=False)  # the erase
        monkeypatch.setattr(
            "server.proxy.cert_manager.verify_cert_in_truststore", truststore
        )

        r = client.get("/api/v1/device/list?cert_installed=true", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["devices"] == [], (
            "a record minutes old shielded an erased simulator from the filter"
        )
        assert truststore.called, "the TrustStore was never consulted"

    def test_a_physical_device_is_never_truststore_verified(
        self, client, auth_headers, app, monkeypatch
    ):
        """Verifying a phone against a simulator path answers false, then saves it.

        `is_cert_installed` looks in
        `CoreSimulator/Devices/<udid>/.../TrustStore.sqlite3`, which does not
        exist for a physical device — so it returns `false` for a phone that
        genuinely trusts the CA, and writes that false into cert-state.json,
        destroying the record `_verify_physical_device` reads to check traffic.

        Physical devices are proxied by their own per-network WiFi config, not
        the host's, so a network change matters for them and is irrelevant to a
        simulator. That asymmetry is why the two cannot share a verifier.
        """
        from server.models import DeviceInfo, DeviceState, DeviceType

        phone = DeviceInfo(
            udid="PHONE", name="iPhone 15 Pro", state=DeviceState.BOOTED,
            device_type=DeviceType.DEVICE, os_version="iOS 26.6", runtime="",
        )
        app.state.device_controller.list_devices = AsyncMock(return_value=[phone])
        app.state.device_controller.check_tools = AsyncMock(return_value={})
        monkeypatch.setattr(
            "server.proxy.cert_state.read_cert_state",
            lambda: {"PHONE": {"name": "iPhone 15 Pro", "cert_installed": True}},
        )

        asked = []

        async def should_not_be_called(_c, udid, verify=False, *, device_name=None):
            asked.append(udid)
            return False

        monkeypatch.setattr(
            "server.proxy.cert_manager.is_cert_installed", should_not_be_called
        )

        r = client.get("/api/v1/device/list?cert_installed=true", headers=auth_headers)
        assert asked == [], "a physical device was sent to the simulator verifier"
        assert [d["udid"] for d in r.json()["devices"]] == ["PHONE"], (
            "the recorded value should stand for a physical device"
        )


class TestLocalCaptureIsGatedToo:
    """`local_capture` routed traffic with no cert guard at all.

    The field report's reproduction: capture a simulator via local_capture,
    erase it, and every HTTPS request fails with a generic in-app error while
    quern reports the certificate installed. `configure_system` refuses in that
    situation; this path had nothing, so `auto_install_cert` had no code to fire
    in either — which is why the setting looked broken rather than absent.
    """

    def _no_trust(self, monkeypatch):
        async def missing(_controller):
            return [{"udid": "AAAA", "name": "iPhone 16 Pro"}]

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", missing
        )

    def _app_with_proxy(self, app):
        """A proxy adapter real enough to build a status response from.

        The attributes matter: a bare MagicMock hands pydantic mock objects
        where it wants strings, and the resulting validation error looks like
        the endpoint failing rather than the fixture being thin.
        """
        adapter = MagicMock()
        adapter.is_running = False
        adapter.listen_host = "0.0.0.0"
        adapter.listen_port = 9101
        adapter.started_at = None
        adapter._intercept_pattern = None
        adapter._active_filter = None
        adapter._mock_rules = []
        adapter._held_flows = {}
        adapter._error = None
        adapter.get_bypass_patterns = MagicMock(return_value=[])
        adapter.reconfigure = MagicMock()
        adapter.stop = AsyncMock()
        adapter.start = AsyncMock()
        app.state.proxy_adapter = adapter
        app.state.local_capture_processes = []
        return adapter

    def test_enabling_capture_is_refused_when_the_ca_is_not_trusted(
        self, client, auth_headers, app, monkeypatch
    ):
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post(
            "/api/v1/proxy/local-capture",
            json={"processes": ["MobileSafari"]},
            headers=auth_headers,
        )
        assert r.status_code == 428, "capture was enabled into a state that cannot work"

    def test_the_string_false_does_not_switch_the_gate_off(
        self, client, auth_headers, app, monkeypatch
    ):
        """`bool("false")` is `True`.

        With an untyped `body: dict` this endpoint read `skip_cert_check` with
        `bool(...)`, so a JSON string `"false"` disabled the cert gate --
        meaning the exact opposite of what was sent. Every other value a client
        might reasonably use for false does the same: "no", "0", "False".
        """
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        for falsey in ("false", "False", "no", "0", 0, False):
            r = client.post(
                "/api/v1/proxy/local-capture",
                json={"processes": ["MobileSafari"], "skip_cert_check": falsey},
                headers=auth_headers,
            )
            assert r.status_code == 428, (
                f"skip_cert_check={falsey!r} disabled the gate"
            )

    def test_a_genuine_skip_still_works(
        self, client, auth_headers, app, monkeypatch
    ):
        # The converse, so the test above cannot be satisfied by an endpoint
        # that ignores the field entirely.
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post(
            "/api/v1/proxy/local-capture",
            json={"processes": ["MobileSafari"], "skip_cert_check": True},
            headers=auth_headers,
        )
        assert r.status_code == 200

    def test_a_malformed_body_is_rejected_not_coerced(
        self, client, auth_headers, app, monkeypatch
    ):
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        for body in ({}, {"processes": "MobileSafari"},
                     {"processes": ["X"], "skip_cert_check": "banana"}):
            r = client.post(
                "/api/v1/proxy/local-capture", json=body, headers=auth_headers,
            )
            assert r.status_code == 422, f"{body!r} was accepted"

    def test_auto_install_fires_here_too(
        self, client, auth_headers, app, monkeypatch
    ):
        """The setting's whole promise is "handled from now on"."""
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: True)

        installed = []

        async def fake_install(_c, udid, device_name=None, **kw):
            installed.append(udid)
            return True

        monkeypatch.setattr("server.proxy.cert_manager.install_cert", fake_install)

        r = client.post(
            "/api/v1/proxy/local-capture",
            json={"processes": ["MobileSafari"]},
            headers=auth_headers,
        )
        assert installed == ["AAAA"], "auto_install_cert did not fire on this path"
        assert r.status_code == 200

    def test_disabling_capture_is_never_refused(
        self, client, auth_headers, app, monkeypatch
    ):
        """Clearing the list stops capture, so it cannot create the broken state.

        Refusing it would trap someone in exactly the situation they are trying
        to leave.
        """
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post(
            "/api/v1/proxy/local-capture",
            json={"processes": []},
            headers=auth_headers,
        )
        assert r.status_code == 200

    def test_the_escape_hatch_still_works(
        self, client, auth_headers, app, monkeypatch
    ):
        self._app_with_proxy(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post(
            "/api/v1/proxy/local-capture",
            json={"processes": ["MobileSafari"], "skip_cert_check": True},
            headers=auth_headers,
        )
        assert r.status_code == 200


class TestBootDoesNotInstallWithoutConsent:
    """Booting a simulator used to install a MITM root CA unconditionally.

    `boot_device` installed the CA whenever the CA file existed, with no
    reference to `auto_install_cert` -- which defaults to False and whose
    docstring says a typo should read as "ask me", never as consent.
    CONTRIBUTING is blunter: a silent, persistent CA-install policy is worse
    than the failure it prevents.

    Measured before the fix on a real simulator: `auto_install_cert: false`,
    TrustStore empty before the call, holding our CA after it, and the response
    saying `cert_auto_installed: true`.
    """

    def _controller(self, app, *, android=False):
        """The real controller's `_is_android` returns a bool.

        A bare MagicMock returns a truthy mock, which silently satisfies the
        Android exemption and lets the install fire -- a green test for a gate
        that never ran, which is the failure this file has hit twice.
        """
        app.state.device_controller.boot = AsyncMock(return_value="AAAA")
        app.state.device_controller._is_android = lambda _u: android
        app.state.device_controller.is_cert_installed = AsyncMock(return_value=False)
        return app.state.device_controller

    def _installs(self, monkeypatch, tmp_path):
        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("present")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        installed = []

        async def fake_install(_c, udid, *a, **kw):
            installed.append(udid)
            return True

        monkeypatch.setattr("server.proxy.cert_manager.install_cert", fake_install)
        return installed

    def test_no_install_when_the_user_has_not_opted_in(
        self, client, auth_headers, app, monkeypatch, tmp_path
    ):
        self._controller(app)
        installed = self._installs(monkeypatch, tmp_path)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post("/api/v1/device/boot", json={"udid": "AAAA"}, headers=auth_headers)
        assert r.status_code == 200
        assert installed == [], "a root CA was installed without consent"
        assert r.json()["cert_auto_installed"] is None

    def test_it_still_installs_when_they_have(
        self, client, auth_headers, app, monkeypatch, tmp_path
    ):
        # The converse, so the fix cannot be "never install on boot".
        self._controller(app)
        installed = self._installs(monkeypatch, tmp_path)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: True)

        r = client.post("/api/v1/device/boot", json={"udid": "AAAA"}, headers=auth_headers)
        assert r.status_code == 200
        assert installed == ["AAAA"]
        assert r.json()["cert_auto_installed"] is True

    def test_android_is_exempt_because_nothing_would_refuse_for_it(
        self, client, auth_headers, app, monkeypatch, tmp_path
    ):
        """Gating Android would remove the install with nothing in its place.

        `simulators_without_cert` skips every non-simulator, so the capture gate
        neither installs nor refuses for an emulator (D9). And `install_cert`'s
        Android path is the only caller of `adb.set_http_proxy` in the tree, so
        gating it would drop the emulator's proxy configuration too and fail
        every HTTPS request with nothing pointing at the cause.
        """
        self._controller(app, android=True)
        installed = self._installs(monkeypatch, tmp_path)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post("/api/v1/device/boot", json={"udid": "AAAA"}, headers=auth_headers)
        assert r.status_code == 200
        assert installed == ["AAAA"], "the Android exemption stopped working"


class TestErasingWithdrawsTheTrustClaim:
    """The erase is what invalidated the record, and this is the one path where
    quern knows it happened.

    Leaving `cert_installed: true` behind means every reader that cannot query
    a shut-down simulator reports the CA as installed -- which is the field
    report's state, created by quern's own endpoint.
    """

    def test_the_record_no_longer_claims_the_ca_is_installed(
        self, client, auth_headers, app
    ):

        from server.models import DeviceCertState
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        before = "2020-01-01T00:00:00+00:00"
        update_cert_state("ERASEME", DeviceCertState(
            name="iPhone 16 Pro", cert_installed=True, fingerprint="a" * 64,
            installed_at="2026-09-01T00:00:00+00:00",
            verified_at=before,
            # Populated on purpose: with this left at None, a mutation that
            # dropped it was indistinguishable from one that kept it, so the
            # docstring's "not a delete" claim had nothing pinning it.
            wifi_proxy_configs={"MonaLisaOverdrive": {
                "proxy_host": "192.168.1.189", "proxy_port": 9101,
                "client_ip": "192.168.1.50", "set_at": before,
            }},
        ).model_dump())
        app.state.device_controller.erase = AsyncMock(return_value=None)

        r = client.post(
            "/api/v1/device/erase", json={"udid": "ERASEME"}, headers=auth_headers
        )
        assert r.status_code == 200

        after = read_cert_state_for_device("ERASEME")
        assert after["cert_installed"] is False, "the erase left a stale trust claim"
        assert after["fingerprint"] is None
        # The erase is the basis and it happened now.
        assert after["verified_at"] != before
        # Not a delete: these are still true of the device, and installed_at is
        # what tells a later reader it *had* the CA before the erase.
        assert after["installed_at"] == "2026-09-01T00:00:00+00:00"
        assert after["name"] == "iPhone 16 Pro"
        assert list(after["wifi_proxy_configs"]) == ["MonaLisaOverdrive"]

    def test_the_claim_is_withdrawn_only_after_the_erase_succeeds(
        self, client, auth_headers, app
    ):
        """Order matters, and nothing pinned it.

        With the invalidation moved above `controller.erase`, a *failed* erase
        still wipes the trust claim — writing `cert_installed: false` for a
        device that was never erased and still trusts the CA. Every other test
        here passed with that mutation applied.
        """
        from datetime import UTC, datetime

        from server.models import DeviceCertState, DeviceError
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("FAILME", DeviceCertState(
            name="iPhone 16 Pro", cert_installed=True, fingerprint="a" * 64,
            verified_at=datetime.now(UTC).isoformat(),
        ).model_dump())
        app.state.device_controller.erase = AsyncMock(
            side_effect=DeviceError("simulator is booted")
        )

        r = client.post(
            "/api/v1/device/erase", json={"udid": "FAILME"}, headers=auth_headers
        )
        assert r.status_code != 200, "a failed erase reported success"

        after = read_cert_state_for_device("FAILME")
        assert after["cert_installed"] is True, (
            "a failed erase withdrew the trust claim anyway"
        )
        assert after["fingerprint"] == "a" * 64

    def test_a_device_we_never_recorded_is_left_alone(
        self, client, auth_headers, app, monkeypatch, caplog
    ):
        import logging

        from server.proxy.cert_state import read_cert_state_for_device

        app.state.device_controller.erase = AsyncMock(return_value=None)
        # Watch the write, not just the result. Without this the test passed
        # even with the `if not existing` guard deleted, because `None.update`
        # raised into the `except` and produced the same empty outcome -- green
        # by way of the error handler rather than the guard.
        wrote = MagicMock()
        monkeypatch.setattr("server.proxy.cert_state.update_cert_state", wrote)

        r = client.post(
            "/api/v1/device/erase", json={"udid": "NEVERSEEN"}, headers=auth_headers
        )
        assert r.status_code == 200
        assert not wrote.called, "an entry was written for a device we never recorded"
        assert read_cert_state_for_device("NEVERSEEN") is None
        # Deleting the guard produces the identical observable outcome, because
        # `None.update` raises into the `except` and no write happens either
        # way. The only difference is the noise, so that is what pins it:
        # a routine erase must not log a warning about clearing cert state.
        assert not [
            rec for rec in caplog.records
            if rec.levelno >= logging.WARNING and "cert state" in rec.getMessage()
        ], "erasing an unrecorded device logged a spurious warning"

    def test_a_bookkeeping_failure_does_not_fail_the_erase(
        self, client, auth_headers, app, monkeypatch
    ):
        # The device is already erased by then; reporting an error would be a
        # lie about the thing the caller actually asked for.
        app.state.device_controller.erase = AsyncMock(return_value=None)
        reader = MagicMock(side_effect=OSError("disk"))
        monkeypatch.setattr(
            "server.proxy.cert_state.read_cert_state_for_device", reader
        )
        r = client.post(
            "/api/v1/device/erase", json={"udid": "AAAA"}, headers=auth_headers
        )
        assert r.status_code == 200
        # Without this the test passes when the invalidation is never called at
        # all, or reads through some other function -- proving nothing about
        # the error handling it names.
        assert reader.called, "the failing seam was never reached"


class TestBootAutoStartAsksTheTrustStore:
    """Gating the install removed this path's only ground-truth refresh.

    `install_cert` calls `is_cert_installed(verify=True)` and writes what it
    learns, so before the consent gate the boot path always refreshed the
    record before deciding whether to auto-start the proxy. With the install
    gated off, reading the record instead is wrong in both directions.
    """

    def _adapter(self, app):
        adapter = MagicMock()
        adapter.is_running = False
        adapter.start = AsyncMock()
        app.state.proxy_adapter = adapter
        return adapter

    def _boot(self, app, monkeypatch, tmp_path, *, truststore):
        app.state.device_controller.boot = AsyncMock(return_value="AAAA")
        app.state.device_controller._is_android = lambda _u: False
        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("present")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        monkeypatch.setattr(
            "server.proxy.cert_manager.get_cert_fingerprint", lambda _p: "a" * 64,
        )
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        monkeypatch.setattr(
            "server.proxy.cert_manager.verify_cert_in_truststore",
            MagicMock(return_value=truststore),
        )

    def test_it_starts_for_a_device_the_truststore_says_is_trusted(
        self, client, auth_headers, app, monkeypatch, tmp_path
    ):
        """A CA installed by hand, or a record quern never wrote.

        Reading the record would say no and the proxy would not start.
        """
        from server.models import DeviceCertState
        from server.proxy.cert_state import update_cert_state

        adapter = self._adapter(app)
        self._boot(app, monkeypatch, tmp_path, truststore=True)
        update_cert_state("AAAA", DeviceCertState(
            name="iPhone 16 Pro", cert_installed=False,
        ).model_dump())

        r = client.post("/api/v1/device/boot", json={"udid": "AAAA"}, headers=auth_headers)
        assert r.status_code == 200
        assert adapter.start.called, "the proxy did not start for a trusting device"

    def test_it_does_not_start_on_a_record_the_truststore_contradicts(
        self, client, auth_headers, app, monkeypatch, tmp_path
    ):
        """A simulator erased outside quern, record still saying installed."""
        from server.models import DeviceCertState
        from server.proxy.cert_state import update_cert_state

        adapter = self._adapter(app)
        self._boot(app, monkeypatch, tmp_path, truststore=False)
        update_cert_state("AAAA", DeviceCertState(
            name="iPhone 16 Pro", cert_installed=True, fingerprint="a" * 64,
        ).model_dump())

        r = client.post("/api/v1/device/boot", json={"udid": "AAAA"}, headers=auth_headers)
        assert r.status_code == 200
        assert not adapter.start.called, (
            "the proxy auto-started on a record the TrustStore contradicts"
        )


class TestSettingCaptureSaysWhatItDropped:
    """`set` replaces, silently, and the response echoes only the new list.

    So removing a process looks identical to adding one. An agent told
    "capture MobileSafari" sends `["MobileSafari"]` and deletes whatever else
    was being watched. The defaults are the usual casualty -- MobileSafari and
    com.apple.WebKit.Networking are applied only when nothing is specified, so
    naming one process drops them and web-view traffic stops being captured.

    Reported from a real session: two of three processes were passed and the
    third vanished with nothing said.
    """

    def _app_with_proxy(self, app, current):
        adapter = MagicMock()
        adapter.is_running = False
        adapter.listen_host = "0.0.0.0"
        adapter.listen_port = 9101
        adapter.started_at = None
        adapter._intercept_pattern = None
        adapter._active_filter = None
        adapter._mock_rules = []
        adapter._held_flows = {}
        adapter._error = None
        adapter._tls_rejections = []
        adapter.get_bypass_patterns = MagicMock(return_value=[])
        adapter.reconfigure = MagicMock()
        adapter.stop = AsyncMock()
        adapter.start = AsyncMock()
        app.state.proxy_adapter = adapter
        app.state.local_capture_processes = list(current)

    def test_a_dropped_process_is_named(
        self, client, auth_headers, app, monkeypatch, caplog
    ):
        import logging

        self._app_with_proxy(
            app, ["Metatext", "MobileSafari", "com.apple.WebKit.Networking"]
        )
        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert",
            AsyncMock(return_value=[]),
        )

        with caplog.at_level(logging.WARNING):
            r = client.post(
                "/api/v1/proxy/local-capture",
                json={"processes": ["MobileSafari", "com.apple.WebKit.Networking"]},
                headers=auth_headers,
            )
        assert r.status_code == 200
        # Assert on the record, not a substring. Coupling to wording let a
        # mutation that reworded the message *and* warned on every change pass.
        warnings = [
            rec for rec in caplog.records
            if rec.levelno >= logging.WARNING and "local_capture" in rec.getMessage()
        ]
        assert len(warnings) == 1, "a process was dropped with nothing said"
        msg = warnings[0].getMessage()
        assert "Metatext" in msg
        assert "MobileSafari" not in msg.split("replaced by")[0], (
            "the warning named a process that is still being captured"
        )

    def test_adding_one_says_nothing(
        self, client, auth_headers, app, monkeypatch, caplog
    ):
        # The converse: a purely additive change must not warn, or the warning
        # becomes noise and stops being read.
        import logging

        self._app_with_proxy(app, ["MobileSafari"])
        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert",
            AsyncMock(return_value=[]),
        )

        with caplog.at_level(logging.WARNING):
            client.post(
                "/api/v1/proxy/local-capture",
                json={"processes": ["MobileSafari", "Metatext"]},
                headers=auth_headers,
            )
        # No warning at all, rather than "no warning containing this phrase" --
        # which a reworded message satisfies while warning on every change.
        assert not [
            rec for rec in caplog.records
            if rec.levelno >= logging.WARNING and "local_capture" in rec.getMessage()
        ], "a pure addition warned; that noise is why warnings stop being read"

    def test_disabling_capture_names_everything_it_stops(
        self, client, auth_headers, app, monkeypatch, caplog
    ):
        import logging

        self._app_with_proxy(app, ["Metatext", "MobileSafari"])
        with caplog.at_level(logging.WARNING):
            client.post(
                "/api/v1/proxy/local-capture",
                json={"processes": []},
                headers=auth_headers,
            )
        warnings = [
            rec for rec in caplog.records
            if rec.levelno >= logging.WARNING and "local_capture" in rec.getMessage()
        ]
        assert len(warnings) == 1
        dropped = warnings[0].getMessage().split("replaced by")[0]
        assert "Metatext" in dropped and "MobileSafari" in dropped


class TestStartingTheSystemProxyIsGatedToo:
    """`POST /proxy/start {system_proxy: true}` reached the gated action by
    another door.

    It calls the same `detect_and_configure` that `configure_system` calls one
    line after its preflight, and had no preflight of its own. So an agent
    refused by `configure_system_proxy` could stop the proxy and start it again
    with the flag, and land in exactly the state the refusal exists to prevent
    -- no 428, nobody asked. The gate's own docstring said it was called from
    "both paths that begin routing a device's traffic through the proxy"; there
    were three.

    Starting the listener alone is still never refused. Binding a port routes
    nothing, and refusing it would stop people setting up the very thing that
    fixes the refusal.
    """

    def _no_trust(self, monkeypatch):
        async def missing(_controller):
            return [{"udid": "AAAA", "name": "iPhone 16 Pro"}]

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", missing
        )

    def _adapter(self, app):
        adapter = MagicMock()
        adapter.is_running = False
        adapter.listen_host = "0.0.0.0"
        adapter.listen_port = 9101
        adapter.started_at = None
        adapter._intercept_pattern = None
        adapter._active_filter = None
        adapter._mock_rules = []
        adapter._held_flows = {}
        adapter._error = None
        adapter.get_bypass_patterns = MagicMock(return_value=[])
        adapter.reconfigure = MagicMock()
        adapter.stop = AsyncMock()
        adapter.start = AsyncMock()
        app.state.proxy_adapter = adapter
        app.state.local_capture_processes = []
        return adapter

    def test_it_is_refused_when_the_ca_is_not_trusted(
        self, client, auth_headers, app, monkeypatch
    ):
        self._adapter(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post(
            "/api/v1/proxy/start", json={"system_proxy": True}, headers=auth_headers,
        )
        assert r.status_code == 428, (
            "the system proxy was configured for a device that cannot use it"
        )

    def test_the_refusal_leaves_the_proxy_stopped(
        self, client, auth_headers, app, monkeypatch
    ):
        """The gate runs before anything starts.

        Gating after `adapter.start()` would leave the listener running behind a
        refused request -- a side effect on the path that declined to act, and
        the next call would then get 409 rather than the 428 that explains it.
        """
        adapter = self._adapter(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        client.post(
            "/api/v1/proxy/start", json={"system_proxy": True}, headers=auth_headers,
        )
        adapter.start.assert_not_called()
        adapter.reconfigure.assert_not_called()

    def test_starting_the_listener_alone_is_never_refused(
        self, client, auth_headers, app, monkeypatch
    ):
        """Binding a port routes nothing, so an untrusted device is irrelevant
        -- and this is the call someone makes on the way to fixing it."""
        adapter = self._adapter(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post("/api/v1/proxy/start", json={}, headers=auth_headers)
        assert r.status_code == 200
        adapter.start.assert_awaited_once()

    def test_the_string_false_does_not_switch_the_gate_off(
        self, client, auth_headers, app, monkeypatch
    ):
        """`bool("false")` is `True`, and this body was an untyped dict."""
        self._adapter(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)

        r = client.post(
            "/api/v1/proxy/start",
            json={"system_proxy": True, "skip_cert_check": "false"},
            headers=auth_headers,
        )
        assert r.status_code == 428, "the string 'false' disabled the cert gate"

    def test_an_explicit_skip_is_honoured(
        self, client, auth_headers, app, monkeypatch
    ):
        adapter = self._adapter(app)
        self._no_trust(monkeypatch)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        monkeypatch.setattr(
            "server.proxy.system_proxy.detect_and_configure", lambda *a, **k: None
        )

        r = client.post(
            "/api/v1/proxy/start",
            json={"system_proxy": True, "skip_cert_check": True},
            headers=auth_headers,
        )
        assert r.status_code == 200
        adapter.start.assert_awaited_once()
