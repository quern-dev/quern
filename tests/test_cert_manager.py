"""Unit tests for server/proxy/cert_manager.py."""

import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.lifecycle.state import ServerState, write_state
from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType
from server.proxy import cert_manager


@pytest.fixture
def mock_cert_path(tmp_path):
    """Create a mock certificate file."""
    cert_path = tmp_path / "mitmproxy-ca-cert.pem"
    cert_path.write_text("""-----BEGIN CERTIFICATE-----
MIIDgTCCAmmgAwIBAgIUNqLmJa7xkFKZ3mQjKe6bFgPmY3kwDQYJKoZIhvcNAQEL
BQAwUDELMAkGA1UEBhMCVVMxEzARBgNVBAgMCkNhbGlmb3JuaWExEjAQBgNVBAcM
CVNhbiBEaWVnbzEYMBYGA1UEAwwPbWl0bXByb3h5LW1pdGlwMB4XDTIzMDMxNTA0
-----END CERTIFICATE-----
""")
    return cert_path


@pytest.fixture
def mock_truststore_db(tmp_path):
    """Create a mock TrustStore.sqlite3 with tsettings table."""
    db_path = tmp_path / "TrustStore.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tsettings(
            sha256 BLOB NOT NULL DEFAULT '',
            subj BLOB NOT NULL DEFAULT '',
            tset BLOB,
            data BLOB,
            uuid BLOB NOT NULL DEFAULT '',
            UNIQUE(sha256,uuid)
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_controller():
    """Create a mock DeviceController."""
    controller = MagicMock()
    controller.simctl = MagicMock()
    controller.simctl._run_simctl = AsyncMock(return_value=("", ""))
    controller.list_devices = AsyncMock(return_value=[
        DeviceInfo(
            udid="test-udid-1234",
            name="iPhone 16 Pro",
            state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR,
        )
    ])
    return controller


@pytest.fixture
def clean_state(tmp_path):
    """Ensure clean state.json for each test."""
    # Mock CONFIG_DIR to use tmp_path
    with patch("server.lifecycle.state.STATE_FILE", tmp_path / "state.json"):
        yield tmp_path / "state.json"


class TestGetCertPath:
    def test_get_cert_path(self):
        """Test get_cert_path returns expected path."""
        path = cert_manager.get_cert_path()
        assert path == Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


class TestGetCertFingerprint:
    def test_get_cert_fingerprint_success(self, mock_cert_path):
        """Test get_cert_fingerprint with valid cert."""
        # Mock openssl subprocess
        mock_output = "SHA256 Fingerprint=9B:6F:C9:AF:52:D1:0A:49:23:FA:93:23:71:41:76:15:5A:9E:AC:38:8A:8E:E2:14:FC:67:1B:A1:5A:EA:72:C3\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=mock_output,
                stderr="",
                returncode=0,
            )
            fingerprint = cert_manager.get_cert_fingerprint(mock_cert_path)

        assert fingerprint == "9b6fc9af52d10a4923fa9323714176155a9eac388a8ee214fc671ba15aea72c3"
        mock_run.assert_called_once()

    def test_get_cert_fingerprint_command_fails(self, mock_cert_path):
        """Test get_cert_fingerprint when openssl fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "openssl", stderr="Error reading certificate"
            )

            with pytest.raises(RuntimeError, match="Failed to get cert fingerprint"):
                cert_manager.get_cert_fingerprint(mock_cert_path)

    def test_get_cert_fingerprint_parse_error(self, mock_cert_path):
        """Test get_cert_fingerprint with malformed output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Invalid output format\n",
                stderr="",
                returncode=0,
            )

            with pytest.raises(RuntimeError, match="Failed to parse openssl output"):
                cert_manager.get_cert_fingerprint(mock_cert_path)


class TestGetTruststorePath:
    def test_get_truststore_path(self):
        """Test get_truststore_path returns expected path."""
        udid = "A1B2C3D4-E5F6-4321-9876-543210FEDCBA"
        path = cert_manager.get_truststore_path(udid)

        expected = (
            Path.home()
            / "Library/Developer/CoreSimulator/Devices"
            / udid
            / "data/private/var/protected/trustd/private/TrustStore.sqlite3"
        )
        assert path == expected


class TestVerifyCertInTruststore:
    def test_verify_cert_found(self, mock_truststore_db):
        """Test verify_cert_in_truststore when cert exists."""
        # Insert test SHA256
        test_sha256 = "9b6fc9af52d10a4923fa9323714176155a9eac388a8ee214fc671ba15aea72c3"
        conn = sqlite3.connect(str(mock_truststore_db))
        conn.execute(
            "INSERT INTO tsettings (sha256, uuid) VALUES (?, ?)",
            (bytes.fromhex(test_sha256), b"test-uuid"),
        )
        conn.commit()
        conn.close()

        with patch("server.proxy.cert_manager.get_truststore_path", return_value=mock_truststore_db):
            result = cert_manager.verify_cert_in_truststore("test-udid", test_sha256)

        assert result is True

    def test_verify_cert_not_found(self, mock_truststore_db):
        """Test verify_cert_in_truststore when cert does not exist."""
        with patch("server.proxy.cert_manager.get_truststore_path", return_value=mock_truststore_db):
            result = cert_manager.verify_cert_in_truststore(
                "test-udid",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )

        assert result is False

    def test_verify_cert_truststore_missing(self, tmp_path):
        """Test verify_cert_in_truststore when TrustStore doesn't exist."""
        nonexistent_path = tmp_path / "nonexistent" / "TrustStore.sqlite3"

        with patch("server.proxy.cert_manager.get_truststore_path", return_value=nonexistent_path):
            result = cert_manager.verify_cert_in_truststore("test-udid", "abc123")

        assert result is False

    def test_verify_cert_sqlite_error(self, tmp_path):
        """Test verify_cert_in_truststore handles SQLite errors gracefully."""
        # Create invalid SQLite file
        bad_db = tmp_path / "bad.db"
        bad_db.write_text("not a sqlite database")

        with patch("server.proxy.cert_manager.get_truststore_path", return_value=bad_db):
            result = cert_manager.verify_cert_in_truststore("test-udid", "abc123")

        assert result is False


class TestIsCertInstalled:
    @pytest.mark.asyncio
    async def test_is_cert_installed_cache_hit(self, mock_controller, mock_cert_path, clean_state):
        """Test is_cert_installed uses cache when fresh."""
        # Set up state with recent verification
        now = datetime.now(timezone.utc)
        state: ServerState = {
            "device_certs": {
                "test-udid": {
                    "name": "iPhone 16 Pro",
                    "cert_installed": True,
                    "fingerprint": "abc123",
                    "verified_at": now.isoformat(),
                }
            }
        }

        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            write_state(state)

            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.verify_cert_in_truststore") as mock_verify:
                        mock_verify.side_effect = AssertionError("Should not call SQLite!")

                        result = await cert_manager.is_cert_installed(
                            mock_controller, "test-udid", verify=False
                        )

        assert result is True
        mock_verify.assert_not_called()  # Cache hit, no SQLite query

    @pytest.mark.asyncio
    async def test_is_cert_installed_cache_stale(self, mock_controller, mock_cert_path, clean_state):
        """Test is_cert_installed queries SQLite when cache is stale."""
        # Set up state with old verification (2 hours ago)
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        state: ServerState = {
            "device_certs": {
                "test-udid": {
                    "name": "iPhone 16 Pro",
                    "cert_installed": True,
                    "fingerprint": "abc123",
                    "verified_at": old_time.isoformat(),
                }
            }
        }

        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            write_state(state)

            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.verify_cert_in_truststore", return_value=True) as mock_verify:
                        result = await cert_manager.is_cert_installed(
                            mock_controller, "test-udid", verify=False
                        )

        assert result is True
        mock_verify.assert_called_once()  # Cache stale, should verify

    @pytest.mark.asyncio
    async def test_is_cert_installed_force_verify(self, mock_controller, mock_cert_path, clean_state):
        """Test is_cert_installed always queries SQLite when verify=True."""
        # Set up state with recent verification
        now = datetime.now(timezone.utc)
        state: ServerState = {
            "device_certs": {
                "test-udid": {
                    "name": "iPhone 16 Pro",
                    "cert_installed": True,
                    "fingerprint": "abc123",
                    "verified_at": now.isoformat(),
                }
            }
        }

        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            write_state(state)

            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.verify_cert_in_truststore", return_value=True) as mock_verify:
                        result = await cert_manager.is_cert_installed(
                            mock_controller, "test-udid", verify=True
                        )

        assert result is True
        mock_verify.assert_called_once()  # Force verify, always check

    @pytest.mark.asyncio
    async def test_is_cert_installed_cert_missing(self, mock_controller, tmp_path, clean_state):
        """Test is_cert_installed when cert file doesn't exist."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=nonexistent_cert):
                result = await cert_manager.is_cert_installed(
                    mock_controller, "test-udid", verify=False
                )

        assert result is False


class TestInstallCert:
    @pytest.mark.asyncio
    async def test_install_cert_success(self, mock_controller, mock_cert_path, clean_state):
        """Test install_cert installs successfully."""
        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.is_cert_installed", return_value=False):
                        result = await cert_manager.install_cert(mock_controller, "test-udid")

        assert result is True  # Newly installed
        mock_controller.simctl._run_simctl.assert_called_once()

    @pytest.mark.asyncio
    async def test_install_cert_already_installed(self, mock_controller, mock_cert_path, clean_state):
        """Test install_cert skips when already installed."""
        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.is_cert_installed", return_value=True):
                        result = await cert_manager.install_cert(mock_controller, "test-udid")

        assert result is False  # Already installed
        mock_controller.simctl._run_simctl.assert_not_called()

    @pytest.mark.asyncio
    async def test_install_cert_force(self, mock_controller, mock_cert_path, clean_state):
        """Test install_cert with force=True installs even if already present."""
        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    result = await cert_manager.install_cert(
                        mock_controller, "test-udid", force=True
                    )

        assert result is True
        mock_controller.simctl._run_simctl.assert_called_once()

    @pytest.mark.asyncio
    async def test_install_cert_file_missing(self, mock_controller, tmp_path, clean_state):
        """Test install_cert fails when cert file doesn't exist."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=nonexistent_cert):
                with pytest.raises(RuntimeError, match="Cert file does not exist"):
                    await cert_manager.install_cert(mock_controller, "test-udid", force=True)

    @pytest.mark.asyncio
    async def test_install_cert_simctl_fails(self, mock_controller, mock_cert_path, clean_state):
        """Test install_cert handles simctl failure."""
        mock_controller.simctl._run_simctl.side_effect = Exception("simctl failed")

        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with pytest.raises(RuntimeError, match="Failed to install cert"):
                        await cert_manager.install_cert(mock_controller, "test-udid", force=True)


class TestGetDeviceCertState:
    @pytest.mark.asyncio
    async def test_get_device_cert_state_installed(self, mock_controller, mock_cert_path, clean_state):
        """Test get_device_cert_state when cert is installed."""
        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.is_cert_installed", return_value=True):
                        state = await cert_manager.get_device_cert_state(
                            mock_controller, "test-udid-1234"
                        )

        assert isinstance(state, DeviceCertState)
        assert state.name == "iPhone 16 Pro"
        assert state.cert_installed is True
        assert state.fingerprint == "abc123"

    @pytest.mark.asyncio
    async def test_get_device_cert_state_not_installed(self, mock_controller, mock_cert_path, clean_state):
        """Test get_device_cert_state when cert is not installed."""
        with patch("server.lifecycle.state.STATE_FILE", clean_state):
            with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
                with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                    with patch("server.proxy.cert_manager.is_cert_installed", return_value=False):
                        state = await cert_manager.get_device_cert_state(
                            mock_controller, "test-udid-1234"
                        )

        assert state.cert_installed is False
        assert state.fingerprint is None
