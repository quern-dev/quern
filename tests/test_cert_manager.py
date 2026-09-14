"""Unit tests for server/proxy/cert_manager.py."""

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    controller._is_android = MagicMock(return_value=False)
    controller.simctl = MagicMock()
    controller.simctl._run_simctl = AsyncMock(return_value=("", ""))
    controller.list_devices = AsyncMock(
        return_value=[
            DeviceInfo(
                udid="test-udid-1234",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
            )
        ]
    )
    return controller


@pytest.fixture
def clean_cert_state(tmp_path):
    """Ensure clean cert-state.json for each test."""
    cert_state_file = tmp_path / "cert-state.json"
    with (
        patch("server.proxy.cert_state.CERT_STATE_FILE", cert_state_file),
        patch("server.proxy.cert_state.CONFIG_DIR", tmp_path),
    ):
        yield cert_state_file


class TestGetCertPath:
    def test_get_cert_path(self):
        """Test get_cert_path returns expected path."""
        path = cert_manager.get_cert_path()
        assert path == Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


class TestGetCertFingerprint:
    def test_get_cert_fingerprint_success(self, mock_cert_path):
        """Test get_cert_fingerprint with valid cert."""
        # Mock openssl subprocess
        mock_output = "SHA256 Fingerprint=9B:6F:C9:AF:52:D1:0A:49:23:FA:93:23:71:41:76:15:5A:9E:AC:38:8A:8E:E2:14:FC:67:1B:A1:5A:EA:72:C3\n"  # noqa: E501

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

        with patch(
            "server.proxy.cert_manager.get_truststore_path", return_value=mock_truststore_db
        ):
            result = cert_manager.verify_cert_in_truststore("test-udid", test_sha256)

        assert result is True

    def test_verify_cert_not_found(self, mock_truststore_db):
        """Test verify_cert_in_truststore when cert does not exist."""
        with patch(
            "server.proxy.cert_manager.get_truststore_path", return_value=mock_truststore_db
        ):
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


class TestCheckTruststoreStatus:
    def test_installed(self, mock_truststore_db, tmp_path):
        """Test check_truststore_status returns 'installed' when cert exists."""
        test_sha256 = "9b6fc9af52d10a4923fa9323714176155a9eac388a8ee214fc671ba15aea72c3"
        conn = sqlite3.connect(str(mock_truststore_db))
        conn.execute(
            "INSERT INTO tsettings (sha256, uuid) VALUES (?, ?)",
            (bytes.fromhex(test_sha256), b"test-uuid"),
        )
        conn.commit()
        conn.close()

        # trustd dir exists (parent of parent of TrustStore)
        trustd_dir = mock_truststore_db.parent.parent

        with patch("server.proxy.cert_manager.get_trustd_dir", return_value=trustd_dir):
            with patch(
                "server.proxy.cert_manager.get_truststore_path", return_value=mock_truststore_db
            ):
                result = cert_manager.check_truststore_status("test-udid", test_sha256)

        assert result == "installed"

    def test_not_installed(self, mock_truststore_db, tmp_path):
        """Test check_truststore_status returns 'not_installed' when trustd exists but no cert."""
        trustd_dir = mock_truststore_db.parent.parent

        with patch("server.proxy.cert_manager.get_trustd_dir", return_value=trustd_dir):
            with patch(
                "server.proxy.cert_manager.get_truststore_path", return_value=mock_truststore_db
            ):
                result = cert_manager.check_truststore_status("test-udid", "abc123")

        assert result == "not_installed"

    def test_never_booted(self, tmp_path):
        """Test check_truststore_status returns 'never_booted' when trustd dir missing."""
        nonexistent_dir = tmp_path / "nonexistent_trustd"

        with patch("server.proxy.cert_manager.get_trustd_dir", return_value=nonexistent_dir):
            result = cert_manager.check_truststore_status("test-udid", "abc123")

        assert result == "never_booted"


class TestIsCertInstalled:
    @pytest.mark.asyncio
    async def test_a_fresh_record_does_not_prevent_the_query(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """The cache is gone, and this is what replaces three tests of it.

        `is_cert_installed` used to return a record written within the hour
        without asking the device. Those three tests pinned that behaviour --
        cache hit, cache stale, and a `verify=True` flag to force past it --
        and all three described machinery that no longer exists.

        What matters now is the opposite claim: a record saying "installed",
        written a second ago, must not stop the TrustStore being asked. See
        ADR 1 in docs/proposals/cert-trust-model.md.
        """
        import json

        clean_cert_state.write_text(json.dumps({
            "test-udid": {
                "name": "iPhone 16 Pro",
                "cert_installed": True,
                "fingerprint": "abc123",
                "verified_at": datetime.now(UTC).isoformat(),
            }
        }))

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=False,
                ) as mock_verify:
                    result = await cert_manager.is_cert_installed(
                        mock_controller, "test-udid"
                    )

        assert mock_verify.called, "a record was believed instead of the device"
        assert result is False, (
            "a device erased since the record was written reported as trusting"
        )

    @pytest.mark.asyncio
    async def test_the_truststore_is_asked_about_the_current_ca(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """Which CA is being asked about is now the only identity mechanism left.

        ADR 1 closes #151 by deleting the cache rather than teaching it to
        compare fingerprints -- so the comparison inside
        `verify_cert_in_truststore` is what stops a device that trusts an *old*
        CA reporting as trusting the current one. Nothing pinned it: passing a
        constant instead of the real fingerprint left the whole suite green.
        """
        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch(
                "server.proxy.cert_manager.get_cert_fingerprint",
                return_value="current-ca-fingerprint",
            ):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=True,
                ) as mock_verify:
                    await cert_manager.is_cert_installed(mock_controller, "test-udid")

        mock_verify.assert_called_once_with("test-udid", "current-ca-fingerprint")

    @pytest.mark.asyncio
    async def test_the_device_is_what_decides(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        # The converse, so the test above cannot be satisfied by always
        # returning False.
        import json

        clean_cert_state.write_text(json.dumps({
            "test-udid": {"name": "iPhone 16 Pro", "cert_installed": False,
                          "verified_at": datetime.now(UTC).isoformat()}
        }))

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=True,
                ) as mock_verify:
                    assert await cert_manager.is_cert_installed(
                        mock_controller, "test-udid"
                    ) is True

        # Not just the answer. Without this, an implementation that shortcuts a
        # persisted `False` straight to `True` -- never asking the device --
        # satisfies the assertion above.
        assert mock_verify.called, "the device was never asked"

    @pytest.mark.asyncio
    async def test_is_cert_installed_cert_missing(
        self, mock_controller, tmp_path, clean_cert_state
    ):
        """Test is_cert_installed when cert file doesn't exist."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        with patch("server.proxy.cert_manager.get_cert_path", return_value=nonexistent_cert):
            result = await cert_manager.is_cert_installed(
                mock_controller, "test-udid"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_is_cert_installed_erase_detection(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """Test is_cert_installed detects device erase (was installed, now gone)."""
        import json

        old_time = datetime.now(UTC) - timedelta(hours=2)
        cert_state_data = {
            "test-udid": {
                "name": "iPhone 16 Pro",
                "cert_installed": True,
                "fingerprint": "abc123",
                "verified_at": old_time.isoformat(),
            }
        }
        clean_cert_state.write_text(json.dumps(cert_state_data))

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore", return_value=False
                ):
                    with patch("server.proxy.cert_manager.logger") as mock_logger:
                        result = await cert_manager.is_cert_installed(
                            mock_controller, "test-udid"
                        )

        assert result is False
        # Should have logged a warning about probable erase
        mock_logger.warning.assert_any_call(
            "Certificate was previously installed on test-udid but is now missing. "
            "Device may have been erased."
        )


class TestInstallCert:
    @pytest.mark.asyncio
    async def test_install_cert_success(self, mock_controller, mock_cert_path, clean_cert_state):
        """Test install_cert installs successfully."""
        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch("server.proxy.cert_manager.is_cert_installed", return_value=False):
                    result = await cert_manager.install_cert(mock_controller, "test-udid")

        assert result is True  # Newly installed
        mock_controller.simctl._run_simctl.assert_called_once()

    @pytest.mark.asyncio
    async def test_install_cert_already_installed(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """Test install_cert skips when already installed."""
        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch("server.proxy.cert_manager.is_cert_installed", return_value=True):
                    result = await cert_manager.install_cert(mock_controller, "test-udid")

        assert result is False  # Already installed
        mock_controller.simctl._run_simctl.assert_not_called()

    @pytest.mark.asyncio
    async def test_install_cert_force(self, mock_controller, mock_cert_path, clean_cert_state):
        """Test install_cert with force=True installs even if already present."""
        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                result = await cert_manager.install_cert(mock_controller, "test-udid", force=True)

        assert result is True
        mock_controller.simctl._run_simctl.assert_called_once()

    @pytest.mark.asyncio
    async def test_install_cert_file_missing(self, mock_controller, tmp_path, clean_cert_state):
        """Test install_cert fails when cert file doesn't exist."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        with patch("server.proxy.cert_manager.get_cert_path", return_value=nonexistent_cert):
            with pytest.raises(RuntimeError, match="Cert file does not exist"):
                await cert_manager.install_cert(mock_controller, "test-udid", force=True)

    @pytest.mark.asyncio
    async def test_install_cert_simctl_fails(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """Test install_cert handles simctl failure."""
        mock_controller.simctl._run_simctl.side_effect = Exception("simctl failed")

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with pytest.raises(RuntimeError, match="Failed to install cert"):
                    await cert_manager.install_cert(mock_controller, "test-udid", force=True)


class TestGetDeviceCertState:
    @pytest.mark.asyncio
    async def test_get_device_cert_state_installed(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """Test get_device_cert_state when cert is installed."""
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
    async def test_get_device_cert_state_not_installed(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """Test get_device_cert_state when cert is not installed."""
        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch("server.proxy.cert_manager.is_cert_installed", return_value=False):
                    state = await cert_manager.get_device_cert_state(
                        mock_controller, "test-udid-1234"
                    )

        assert state.cert_installed is False
        assert state.fingerprint is None


class TestVerificationDoesNotClobberTheRecord:
    """A read-shaped call was erasing fields it had no opinion about.

    `is_cert_installed` rebuilt the entry from the four things it had just
    learned and wrote that over the whole record, and `update_cert_state`
    replaced rather than merged. So every verification erased `installed_at`
    and `wifi_proxy_configs`.

    Found by running the real thing: after an auto-install and an erase on a
    live simulator, `installed_at` read `None` — the field the erase path's own
    docstring claims is "what tells a later reader it *had* the CA".
    """

    @pytest.mark.asyncio
    async def test_installed_at_survives_a_verification(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("test-udid", {
            "name": "iPhone 16 Pro", "cert_installed": True,
            "fingerprint": "abc123",
            "installed_at": "2026-09-14T09:00:00+00:00",
        })

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=True,
                ):
                    await cert_manager.is_cert_installed(mock_controller, "test-udid")

        after = read_cert_state_for_device("test-udid")
        assert after["installed_at"] == "2026-09-14T09:00:00+00:00", (
            "verification erased when the CA was installed"
        )
        # Absence of damage is not the whole claim. Without this, a
        # verification that writes *nothing at all* passes -- and it did:
        # `if is_installed: return True` above the write left the full suite
        # green.
        assert after["verified_at"] != "2026-09-14T09:00:00+00:00"
        assert after["verified_at"].startswith("20"), after["verified_at"]
        assert after["cert_installed"] is True

    @pytest.mark.asyncio
    async def test_a_phones_proxy_config_survives_a_verification(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """`wifi_proxy_configs` holds the recorded proxy host and `client_ip`.

        It is what `_verify_physical_device` reads to find that device's
        traffic at all, so losing it makes a phone unverifiable.
        """
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("test-udid", {
            "name": "iPhone 11", "cert_installed": True,
            "wifi_proxy_configs": {"MonaLisaOverdrive": {
                "proxy_host": "192.168.1.189", "proxy_port": 9101,
                "client_ip": "192.168.1.50",
                "set_at": "2026-09-14T09:00:00+00:00",
            }},
        })

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=True,
                ):
                    await cert_manager.is_cert_installed(mock_controller, "test-udid")

        after = read_cert_state_for_device("test-udid")
        assert list(after.get("wifi_proxy_configs") or {}) == ["MonaLisaOverdrive"]
        assert after["wifi_proxy_configs"]["MonaLisaOverdrive"]["client_ip"] == (
            "192.168.1.50"
        )
        assert after.get("verified_at"), "the verification recorded nothing"

    @pytest.mark.asyncio
    async def test_verification_still_updates_what_it_learned(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        # The converse: preserving must not become "never writes anything".
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("test-udid", {
            "name": "iPhone 16 Pro", "cert_installed": True,
            "fingerprint": "abc123", "installed_at": "2026-09-14T09:00:00+00:00",
        })

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=False,
                ):
                    await cert_manager.is_cert_installed(mock_controller, "test-udid")

        after = read_cert_state_for_device("test-udid")
        assert after["cert_installed"] is False, "the erase was not recorded"
        assert after["fingerprint"] is None, "a named field must still be cleared"
        assert after["installed_at"] == "2026-09-14T09:00:00+00:00"

    def test_naming_a_field_none_clears_it(self, clean_cert_state):
        """Omission preserves; naming overwrites, including with None.

        Without that, the erase path could not withdraw a trust claim.
        """
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("test-udid", {
            "name": "x", "cert_installed": True, "fingerprint": "abc123",
            "installed_at": "2026-09-14T09:00:00+00:00",
        })
        update_cert_state("test-udid", {"cert_installed": False, "fingerprint": None})

        after = read_cert_state_for_device("test-udid")
        assert after["cert_installed"] is False
        assert after["fingerprint"] is None
        assert after["installed_at"] == "2026-09-14T09:00:00+00:00"

    def test_other_devices_are_still_untouched(self, clean_cert_state):
        """The merge is per device as well as per field.

        An earlier version of this test wrote `aaa` first, so a merge that
        took *any* device's record as its base still produced the right answer
        for `aaa`. Now the device being updated is the second one written, and
        the first carries a field it must not inherit.
        """
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("bbb", {
            "name": "B", "cert_installed": True,
            "installed_at": "2020-01-01T00:00:00+00:00",
            "wifi_proxy_configs": {"B-only": {
                "proxy_host": "10.0.0.1", "proxy_port": 9101,
                "client_ip": "10.0.0.2", "set_at": "2020-01-01T00:00:00+00:00",
            }},
        })
        update_cert_state("aaa", {"name": "A", "cert_installed": True})
        update_cert_state("aaa", {"cert_installed": False})

        a = read_cert_state_for_device("aaa")
        assert a["cert_installed"] is False
        assert a.get("installed_at") is None, "aaa inherited bbb's installed_at"
        assert not a.get("wifi_proxy_configs"), "aaa inherited bbb's proxy config"

        b = read_cert_state_for_device("bbb")
        assert b["cert_installed"] is True, "updating aaa changed bbb"
        assert list(b["wifi_proxy_configs"]) == ["B-only"]

    @pytest.mark.asyncio
    async def test_the_android_path_preserves_the_record_too(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """The sibling site. It was changed by the same commit and had no test,
        so reverting it to a full `model_dump()` left the suite green."""
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("droid", {
            "name": "Pixel 6", "cert_installed": True,
            "installed_at": "2026-09-14T09:00:00+00:00",
        })
        mock_controller._is_android = lambda _u: True
        mock_controller.adb = MagicMock()
        mock_controller.adb._get_cert_hash = AsyncMock(return_value="abc")
        mock_controller.adb.is_system_cert_installed = AsyncMock(return_value=True)

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="f"):
                await cert_manager.is_cert_installed(
                    mock_controller, "droid", device_name="Pixel 6",
                )

        assert read_cert_state_for_device("droid")["installed_at"] == (
            "2026-09-14T09:00:00+00:00"
        )

    @pytest.mark.asyncio
    async def test_a_failed_name_lookup_does_not_rename_the_device(
        self, mock_controller, mock_cert_path, clean_cert_state
    ):
        """`_get_device_name` returned "Unknown Device" both when the device was
        absent and when `list_devices` raised, and the write named `name`
        unconditionally -- so adb being down renamed a device on disk.

        The PR's own tests demonstrated this and did not notice: they seed
        "iPhone 16 Pro" and the record they assert on says "Unknown Device".
        """
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("test-udid", {"name": "iPhone 16 Pro", "cert_installed": True})
        mock_controller.list_devices = AsyncMock(side_effect=OSError("adb is down"))

        with patch("server.proxy.cert_manager.get_cert_path", return_value=mock_cert_path):
            with patch("server.proxy.cert_manager.get_cert_fingerprint", return_value="abc123"):
                with patch(
                    "server.proxy.cert_manager.verify_cert_in_truststore",
                    return_value=True,
                ):
                    await cert_manager.is_cert_installed(mock_controller, "test-udid")

        assert read_cert_state_for_device("test-udid")["name"] == "iPhone 16 Pro", (
            "a failed lookup renamed the device"
        )

    def test_legacy_and_computed_fields_do_not_survive_a_write(
        self, clean_cert_state
    ):
        """The merge preserved them forever, where the old replace healed them.

        Flat `proxy_host`/`proxy_port` are the bad case: `DeviceCertState`
        ignores extras, so nothing raises, `strip_noncanonical_fields` never
        fires, and they persist permanently.
        """
        import json

        from server.proxy.cert_state import (
            _CANONICAL_FIELDS,
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("legacy", {"name": "A", "cert_installed": True})
        raw = json.loads(clean_cert_state.read_text())
        raw["legacy"].update({
            "wifi_proxy_stale": True, "active_wifi_network": "Home",
            "proxy_host": "10.0.0.1", "proxy_port": 9101,
        })
        clean_cert_state.write_text(json.dumps(raw))

        update_cert_state("legacy", {"cert_installed": False})

        after = read_cert_state_for_device("legacy")
        assert [k for k in after if k not in _CANONICAL_FIELDS] == [], (
            f"non-canonical fields survived: {sorted(set(after) - _CANONICAL_FIELDS)}"
        )

    def test_non_canonical_fields_are_not_accepted_on_the_way_in(
        self, clean_cert_state
    ):
        """Both sides are filtered, and each needs its own test.

        Stripping only what is on disk still passes when the incoming filter is
        deleted, because the caller in that test passes a clean dict. These are
        the computed fields `proxy_status` derives at read time and must never
        store -- writing one makes the stored value shadow the computed one.
        """
        from server.proxy.cert_state import (
            _CANONICAL_FIELDS,
            read_cert_state_for_device,
            update_cert_state,
        )

        update_cert_state("incoming", {
            "name": "A", "cert_installed": True,
            "wifi_proxy_stale": True, "active_wifi_network": "Home",
            "cert_trust_stale": True,
        })

        after = read_cert_state_for_device("incoming")
        assert after["name"] == "A", "the canonical fields were dropped too"
        assert [k for k in after if k not in _CANONICAL_FIELDS] == [], (
            f"a computed field was stored: {sorted(set(after) - _CANONICAL_FIELDS)}"
        )
