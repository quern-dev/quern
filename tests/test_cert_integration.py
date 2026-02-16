"""Integration tests for certificate management with real simulators.

These tests require:
- A booted iOS simulator
- mitmproxy CA certificate generated (~/.mitmproxy/mitmproxy-ca-cert.pem)
- simctl available in PATH

Mark with @pytest.mark.integration to skip in CI.
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from server.device.controller import DeviceController
from server.models import DeviceState
from server.proxy import cert_manager


def get_booted_simulator() -> str | None:
    """Get the UDID of a booted simulator, or None if none are booted."""
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "booted"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        # Parse output like: "    iPhone 17 Pro (43B500A9-B34B-4E50-AB65-F9F0F3281E07) (Booted)"
        for line in result.stdout.splitlines():
            if "(Booted)" in line and "(" in line:
                # Extract UDID from between first and second parentheses
                import re
                match = re.search(r'\(([A-F0-9-]{36})\)', line)
                if match:
                    return match.group(1)
        return None
    except Exception:
        return None


@pytest.fixture
def controller():
    """Create a real DeviceController."""
    ctrl = DeviceController()
    # Give it time to initialize
    asyncio.run(ctrl.check_tools())
    return ctrl


@pytest.fixture
def booted_simulator():
    """Get a booted simulator UDID, or skip test if none available."""
    udid = get_booted_simulator()
    if not udid:
        pytest.skip("No booted simulator found - boot one with: xcrun simctl boot <udid>")
    return udid


@pytest.fixture
def cert_path():
    """Verify mitmproxy cert exists."""
    path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if not path.exists():
        pytest.skip(
            "mitmproxy CA cert not found. Generate it by running: "
            "mitmproxy (quit immediately after it starts)"
        )
    return path


@pytest.mark.integration
class TestCertInstallationIntegration:
    """Integration tests for certificate installation with real simulators."""

    @pytest.mark.asyncio
    async def test_verify_cert_in_truststore(self, booted_simulator, cert_path):
        """Test SQLite TrustStore verification with real simulator."""
        # Get the expected fingerprint
        expected_fingerprint = cert_manager.get_cert_fingerprint(cert_path)

        # Query the TrustStore
        is_installed = cert_manager.verify_cert_in_truststore(
            booted_simulator, expected_fingerprint
        )

        # Result should be boolean (either installed or not)
        assert isinstance(is_installed, bool)

        # Verify TrustStore file exists for this simulator
        truststore_path = cert_manager.get_truststore_path(booted_simulator)
        # TrustStore might not exist if simulator was never fully booted
        # but if cert is installed, TrustStore must exist
        if is_installed:
            assert truststore_path.exists(), "Cert installed but TrustStore missing"

    @pytest.mark.asyncio
    async def test_full_install_verify_cycle(self, controller, booted_simulator, cert_path):
        """Test full cycle: check → install → verify → check again."""
        # Step 1: Check current state (don't verify via SQLite, use cache)
        initial_state = await cert_manager.is_cert_installed(
            controller, booted_simulator, verify=False
        )
        print(f"\nInitial cert state (cached): {initial_state}")

        # Step 2: Force SQLite verification
        verified_state = await cert_manager.is_cert_installed(
            controller, booted_simulator, verify=True
        )
        print(f"Verified cert state (SQLite): {verified_state}")

        # Step 3: Attempt installation (might already be installed)
        was_installed = await cert_manager.install_cert(
            controller, booted_simulator, force=False
        )
        print(f"Install result: {'newly installed' if was_installed else 'already installed'}")

        # Step 4: Verify it's now installed
        final_state = await cert_manager.is_cert_installed(
            controller, booted_simulator, verify=True
        )
        assert final_state is True, "Cert should be installed after install_cert()"

        # Step 5: Get full device cert state
        device_state = await cert_manager.get_device_cert_state(
            controller, booted_simulator, verify=True
        )
        assert device_state.cert_installed is True
        assert device_state.fingerprint is not None
        assert device_state.name != "Unknown Device"
        print(f"Final device state: {device_state.model_dump()}")

    @pytest.mark.asyncio
    async def test_install_idempotency(self, controller, booted_simulator):
        """Test that install_cert is idempotent."""
        # Install once
        first_install = await cert_manager.install_cert(
            controller, booted_simulator, force=False
        )

        # Install again (should be no-op)
        second_install = await cert_manager.install_cert(
            controller, booted_simulator, force=False
        )

        # Second install should return False (already installed)
        assert second_install is False, "Second install should return False (already installed)"

    @pytest.mark.asyncio
    async def test_force_reinstall(self, controller, booted_simulator):
        """Test force=True reinstalls even if already present."""
        # Ensure cert is installed first
        await cert_manager.install_cert(controller, booted_simulator, force=False)

        # Force reinstall
        was_reinstalled = await cert_manager.install_cert(
            controller, booted_simulator, force=True
        )

        # force=True should always install (return True)
        assert was_reinstalled is True, "force=True should always install"

        # Verify it's still installed after force reinstall
        is_installed = await cert_manager.is_cert_installed(
            controller, booted_simulator, verify=True
        )
        assert is_installed is True

    @pytest.mark.asyncio
    async def test_cache_ttl_behavior(self, controller, booted_simulator):
        """Test that cache is used within TTL window."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch

        # Force a verification to populate the cache
        await cert_manager.is_cert_installed(controller, booted_simulator, verify=True)

        # Now check without verification (should hit cache)
        # Mock the verify_cert_in_truststore to raise if called
        with patch("server.proxy.cert_manager.verify_cert_in_truststore") as mock_verify:
            mock_verify.side_effect = AssertionError("Should not call SQLite!")

            # This should NOT call SQLite (cache hit)
            result = await cert_manager.is_cert_installed(
                controller, booted_simulator, verify=False
            )

            # Should return a result without calling SQLite
            assert isinstance(result, bool)
            mock_verify.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_devices(self, controller):
        """Test handling multiple booted simulators."""
        devices = await controller.list_devices()
        booted_devices = [d for d in devices if d.state == DeviceState.BOOTED]

        if len(booted_devices) < 2:
            pytest.skip("Test requires at least 2 booted simulators")

        # Verify cert on all booted devices
        results = []
        for device in booted_devices[:2]:  # Test with first 2
            state = await cert_manager.get_device_cert_state(
                controller, device.udid, verify=True
            )
            results.append((device.name, state.cert_installed))

        print(f"\nMulti-device cert status: {results}")
        # All results should be boolean
        assert all(isinstance(installed, bool) for _, installed in results)


@pytest.mark.integration
class TestCertManagerErrorHandling:
    """Test error handling in cert_manager with real environment."""

    @pytest.mark.asyncio
    async def test_nonexistent_simulator(self, controller):
        """Test handling of non-existent simulator UDID."""
        fake_udid = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

        # Should return False (TrustStore doesn't exist)
        result = cert_manager.verify_cert_in_truststore(fake_udid, "fake_fingerprint")
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_cert_file(self, controller, booted_simulator, tmp_path):
        """Test handling of missing cert file."""
        nonexistent_cert = tmp_path / "nonexistent.pem"

        with patch("server.proxy.cert_manager.get_cert_path", return_value=nonexistent_cert):
            result = await cert_manager.is_cert_installed(
                controller, booted_simulator, verify=False
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_invalid_fingerprint(self, booted_simulator):
        """Test verification with wrong fingerprint."""
        wrong_fingerprint = "0000000000000000000000000000000000000000000000000000000000000000"

        # Should return False (fingerprint doesn't match)
        result = cert_manager.verify_cert_in_truststore(booted_simulator, wrong_fingerprint)
        assert result is False


if __name__ == "__main__":
    # Allow running integration tests directly
    pytest.main([__file__, "-v", "-s", "-m", "integration"])
