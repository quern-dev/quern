"""Certificate installation verification for iOS simulators.

Hybrid verification approach:
- Fast path: Check persistent cert-state.json cache (recent verification < 1 hour)
- Slow path: Query simulator's TrustStore.sqlite3 via SQLite
- Always update cache after SQLite verification
- Detects device erasure (cert was installed, now missing)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from server.proxy.cert_state import read_cert_state, read_cert_state_for_device, update_cert_state
from server.models import DeviceCertState

logger = logging.getLogger(__name__)

# Cache TTL: 1 hour (3600 seconds)
CACHE_TTL_SECONDS = 3600


def get_cert_path() -> Path:
    """Get path to mitmproxy CA certificate.

    Returns:
        Path to mitmproxy-ca-cert.pem
    """
    return Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


def get_cert_fingerprint(cert_path: Path) -> str:
    """Get SHA256 fingerprint of certificate.

    Args:
        cert_path: Path to PEM certificate file

    Returns:
        SHA256 fingerprint as lowercase hex string (no colons)

    Raises:
        RuntimeError: If openssl command fails
    """
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Output: "SHA256 Fingerprint=9B:6F:C9:AF:..."
        fingerprint = proc.stdout.split("=")[1].strip()
        # Remove colons and convert to lowercase
        return fingerprint.replace(":", "").lower()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get cert fingerprint: {e.stderr}") from e
    except (IndexError, AttributeError) as e:
        raise RuntimeError(f"Failed to parse openssl output: {proc.stdout}") from e


def get_truststore_path(udid: str) -> Path:
    """Get path to simulator's TrustStore.sqlite3.

    Args:
        udid: Device UDID

    Returns:
        Path to TrustStore.sqlite3
    """
    return (
        Path.home()
        / "Library/Developer/CoreSimulator/Devices"
        / udid
        / "data/private/var/protected/trustd/private/TrustStore.sqlite3"
    )


def get_trustd_dir(udid: str) -> Path:
    """Get path to simulator's trustd directory.

    This directory is created when the simulator first boots and trustd runs.
    If it doesn't exist, the device has never been booted.
    """
    return (
        Path.home()
        / "Library/Developer/CoreSimulator/Devices"
        / udid
        / "data/private/var/protected/trustd"
    )


def verify_cert_in_truststore(udid: str, expected_sha256: str) -> bool:
    """Check if cert with given SHA256 exists in TrustStore (SQLite query).

    Args:
        udid: Device UDID
        expected_sha256: Expected SHA256 fingerprint (lowercase hex, no colons)

    Returns:
        True if certificate is installed, False otherwise
    """
    truststore = get_truststore_path(udid)
    if not truststore.exists():
        logger.info(f"TrustStore does not exist for {udid}: {truststore}")
        return False

    try:
        conn = sqlite3.connect(str(truststore))
        cursor = conn.execute(
            "SELECT COUNT(*) FROM tsettings WHERE hex(sha256) = upper(?)",
            (expected_sha256,),
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception as e:
        logger.warning(f"Failed to query TrustStore for {udid}: {e}")
        return False


def check_truststore_status(
    udid: str, expected_sha256: str
) -> Literal["installed", "not_installed", "never_booted"]:
    """Check TrustStore status, distinguishing "no cert" from "never booted".

    Args:
        udid: Device UDID
        expected_sha256: Expected SHA256 fingerprint (lowercase hex, no colons)

    Returns:
        "installed" if cert is in TrustStore,
        "never_booted" if trustd directory doesn't exist,
        "not_installed" if trustd exists but cert is not present
    """
    trustd_dir = get_trustd_dir(udid)
    if not trustd_dir.exists():
        return "never_booted"

    if verify_cert_in_truststore(udid, expected_sha256):
        return "installed"

    return "not_installed"


async def is_cert_installed(controller, udid: str, verify: bool = False) -> bool:
    """Check if mitmproxy CA cert is installed on simulator.

    Hybrid approach:
    - If verify=False: Check cert-state.json first, only query SQLite if cache is stale
    - If verify=True: Always query SQLite (ground truth)

    Detects device erasure: if cert was previously installed but is now missing,
    logs a warning about probable erase.

    Args:
        controller: DeviceController instance
        udid: Device UDID
        verify: If True, always check SQLite. If False, trust cache.

    Returns:
        True if certificate is installed, False otherwise
    """
    cert_path = get_cert_path()
    if not cert_path.exists():
        logger.error(f"Cert file does not exist: {cert_path}")
        return False

    expected_fingerprint = get_cert_fingerprint(cert_path)

    # Fast path: Check cache
    cached = read_cert_state_for_device(udid)

    # Check cache age
    if not verify and cached and cached.get("verified_at"):
        try:
            verified_at = datetime.fromisoformat(cached["verified_at"])
            age = datetime.now(timezone.utc) - verified_at
            if age.total_seconds() < CACHE_TTL_SECONDS:
                logger.debug(f"Cache hit for {udid} (age: {age.total_seconds():.0f}s)")
                return cached.get("cert_installed", False)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid verified_at timestamp for {udid}: {e}")

    # Slow path: Query TrustStore SQLite database
    logger.debug(f"Verifying cert for {udid} via SQLite")
    is_installed = verify_cert_in_truststore(udid, expected_fingerprint)

    # Detect erase: was installed before, now it's gone
    was_installed = cached.get("cert_installed", False) if cached else False
    if was_installed and not is_installed:
        logger.warning(
            f"Certificate was previously installed on {udid} but is now missing. "
            "Device may have been erased."
        )

    # Update persistent cache
    device_name = await _get_device_name(controller, udid)
    cert_state = DeviceCertState(
        name=device_name,
        cert_installed=is_installed,
        fingerprint=expected_fingerprint if is_installed else None,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )

    update_cert_state(udid, cert_state.model_dump())

    return is_installed


async def install_cert(controller, udid: str, force: bool = False) -> bool:
    """Install mitmproxy CA cert if not already present.

    Args:
        controller: DeviceController instance
        udid: Device UDID
        force: If True, install even if already present

    Returns:
        True if cert was newly installed, False if already installed

    Raises:
        RuntimeError: If installation fails
    """
    # Check if already installed (unless force=True)
    if not force and await is_cert_installed(controller, udid, verify=True):
        logger.info(f"Cert already installed on {udid}")
        return False  # Already installed

    cert_path = get_cert_path()
    if not cert_path.exists():
        raise RuntimeError(f"Cert file does not exist: {cert_path}")

    # Install via simctl
    try:
        await controller.simctl._run_simctl("keychain", udid, "add-root-cert", str(cert_path))
    except Exception as e:
        raise RuntimeError(f"Failed to install cert on {udid}: {e}") from e

    # Update state (no need to verify, we just installed it)
    fingerprint = get_cert_fingerprint(cert_path)
    device_name = await _get_device_name(controller, udid)
    now = datetime.now(timezone.utc).isoformat()

    cert_state = DeviceCertState(
        name=device_name,
        cert_installed=True,
        fingerprint=fingerprint,
        installed_at=now,
        verified_at=now,
    )

    update_cert_state(udid, cert_state.model_dump())

    logger.info(f"Installed mitmproxy CA cert on {udid}")
    return True  # Newly installed


async def get_device_cert_state(controller, udid: str, verify: bool = False) -> DeviceCertState:
    """Get certificate installation state for a device.

    Args:
        controller: DeviceController instance
        udid: Device UDID
        verify: If True, force SQLite verification

    Returns:
        DeviceCertState with current installation status
    """
    cert_path = get_cert_path()
    device_name = await _get_device_name(controller, udid)

    if not cert_path.exists():
        # Cert file doesn't exist
        return DeviceCertState(
            name=device_name,
            cert_installed=False,
            fingerprint=None,
            verified_at=datetime.now(timezone.utc).isoformat(),
        )

    is_installed = await is_cert_installed(controller, udid, verify=verify)
    fingerprint = get_cert_fingerprint(cert_path) if is_installed else None

    # Get timestamps from persistent state
    cached = read_cert_state_for_device(udid) or {}

    return DeviceCertState(
        name=device_name,
        cert_installed=is_installed,
        fingerprint=fingerprint,
        installed_at=cached.get("installed_at"),
        verified_at=datetime.now(timezone.utc).isoformat(),
    )


async def _get_device_name(controller, udid: str) -> str:
    """Get device name from DeviceController.

    Args:
        controller: DeviceController instance
        udid: Device UDID

    Returns:
        Device name or "Unknown Device" if not found
    """
    try:
        devices = await controller.list_devices()
        for device in devices:
            if device.udid == udid:
                return device.name
        return "Unknown Device"
    except Exception as e:
        logger.warning(f"Failed to get device name for {udid}: {e}")
        return "Unknown Device"
