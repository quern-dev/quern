"""Certificate installation and verification for iOS simulators and Android devices.

Verification always asks the device:
- iOS simulators: query the TrustStore.sqlite3 via SQLite
- Android: check the system cert store via adb
- The result is written to cert-state.json, and the previous record is read only
  to notice a device that *had* the cert and no longer does (a probable erase)

There was an hour-long cache in front of this, with a `verify` flag to skip it.
Both are gone -- see ADR 1 in docs/proposals/cert-trust-model.md. It saved a
0.11 ms SQLite query out of a ~9 ms call, Android returned above it and so could
never use it, and the hour it held an answer for was an hour in which an erase
went unnoticed.

Android cert installation:
- Rootable emulators (Google APIs / dev-keys): Automated system cert injection
  - API < 34: adb remount + push to /system/etc/security/cacerts/
  - API >= 34: nsenter APEX mount injection
- Non-rootable (Google Play images, physical devices): Not supported (user must
  install manually as user cert + add networkSecurityConfig to debug builds)
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from server.models import DeviceCertState, DeviceType
from server.proxy.cert_state import read_cert_state_for_device, update_cert_state

logger = logging.getLogger(__name__)



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


async def is_cert_installed(
    controller, udid: str, *, device_name: str | None = None,
) -> bool:
    """Whether this device trusts the mitmproxy CA, asked of the device.

    Always ground truth: the TrustStore for a simulator, `adb` for Android.
    There used to be an hour-long cache in front of this, and a `verify` flag
    to skip it. Both are gone -- see ADR 1 in
    docs/proposals/cert-trust-model.md. In short: Android returned above the
    cache and so could never use it, `get_cert_fingerprint` shells out to
    openssl *before* the cache was consulted and costs 9 ms, and the query the
    cache skipped costs 0.11 ms. It saved about 1% of the call, and the hour it
    held an answer for was an hour in which an erase went unnoticed.

    Detects device erasure: if the cert was previously installed and is now
    missing, logs a warning about a probable erase.

    Args:
        controller: DeviceController instance
        udid: Device UDID
        device_name: Pre-resolved device name to avoid redundant list_devices calls.

    Returns:
        True if the certificate is installed, False otherwise
    """
    cert_path = get_cert_path()
    if not cert_path.exists():
        logger.error(f"Cert file does not exist: {cert_path}")
        return False

    # Android: check system cert store via adb
    if controller._is_android(udid):
        return await _is_cert_installed_android(
            controller, udid, cert_path, device_name=device_name,
        )

    expected_fingerprint = get_cert_fingerprint(cert_path)
    cached = read_cert_state_for_device(udid)

    # Read, never believed: only to notice that a device which *had* the cert
    # no longer does, which is what makes the erase warning below possible.
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
    if device_name is None:
        device_name = await _get_device_name(controller, udid)
    # Exactly what this call learned, and nothing else. A full `model_dump()`
    # names every field -- including `installed_at` and `wifi_proxy_configs`,
    # which verification has no opinion about -- and naming them clears them.
    update_cert_state(udid, {
        "name": device_name,
        "cert_installed": is_installed,
        "fingerprint": expected_fingerprint if is_installed else None,
        "verified_at": datetime.now(UTC).isoformat(),
    })

    return is_installed


async def _is_cert_installed_android(
    controller, udid: str, cert_path: Path, *, device_name: str | None = None,
) -> bool:
    """Check if the mitmproxy cert is installed as a system cert on Android."""
    adb = controller.adb

    try:
        cert_hash = await adb._get_cert_hash(cert_path)
        cert_filename = f"{cert_hash}.0"
        is_installed = await adb.is_system_cert_installed(udid, cert_filename)
    except Exception as e:
        logger.warning(f"Failed to check cert on Android device {udid}: {e}")
        is_installed = False

    # Update cache
    if device_name is None:
        device_name = await _get_device_name(controller, udid)
    fingerprint = get_cert_fingerprint(cert_path) if is_installed else None

    update_cert_state(udid, {
        "name": device_name,
        "cert_installed": is_installed,
        "fingerprint": fingerprint,
        "verified_at": datetime.now(UTC).isoformat(),
    })

    return is_installed


async def install_cert(
    controller, udid: str, force: bool = False, *, device_name: str | None = None,
) -> bool:
    """Install mitmproxy CA cert if not already present.

    Dispatches to the appropriate backend:
    - iOS simulators: simctl keychain add-root-cert
    - Android (rootable): adb system cert injection (remount or nsenter)
    - Android (non-rootable): raises RuntimeError with guidance

    Args:
        controller: DeviceController instance
        udid: Device UDID
        force: If True, install even if already present
        device_name: Pre-resolved device name to avoid redundant list_devices calls.

    Returns:
        True if cert was newly installed, False if already installed

    Raises:
        RuntimeError: If installation fails
    """
    cert_path = get_cert_path()
    if not cert_path.exists():
        raise RuntimeError(f"Cert file does not exist: {cert_path}")

    if controller._is_android(udid):
        return await _install_cert_android(
            controller, udid, cert_path, force,
            device_name=device_name,
        )

    # Check if already installed (unless force=True)
    if not force and await is_cert_installed(
        controller, udid, device_name=device_name,
    ):
        logger.info(f"Cert already installed on {udid}")
        return False  # Already installed

    # Install via simctl
    try:
        await controller.simctl._run_simctl("keychain", udid, "add-root-cert", str(cert_path))
    except Exception as e:
        raise RuntimeError(f"Failed to install cert on {udid}: {e}") from e

    # Update state (no need to verify, we just installed it)
    fingerprint = get_cert_fingerprint(cert_path)
    if device_name is None:
        device_name = await _get_device_name(controller, udid)
    now = datetime.now(UTC).isoformat()

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


async def _install_cert_android(
    controller, udid: str, cert_path: Path, force: bool,
    *, device_name: str | None = None,
) -> bool:
    """Install mitmproxy CA cert on an Android device.

    Rootable devices (dev-keys): Automated system cert injection.
    Non-rootable devices: Raises with guidance for manual user cert install.
    """
    adb = controller.adb

    if not await adb.is_rootable(udid):
        raise RuntimeError(
            f"Cannot auto-install cert on {udid}: device is not rootable "
            "(Google Play image or physical device). "
            "Options:\n"
            "1. Create a rootable emulator (Google APIs image, not Google Play) — "
            "identical for app development since apps install via adb:\n"
            '   sdkmanager "system-images;android-34;google_apis;arm64-v8a"\n'
            "   avdmanager create avd -n <name> "
            '-k "system-images;android-34;google_apis;arm64-v8a" '
            "-d pixel_6\n"
            "2. Or install the cert manually as a user certificate and add "
            "networkSecurityConfig with <certificates src=\"user\" /> to your "
            "app's debug build."
        )

    cert_hash = await adb._get_cert_hash(cert_path)
    cert_filename = f"{cert_hash}.0"

    # Check if already installed (unless force=True)
    if not force and await adb.is_system_cert_installed(udid, cert_filename):
        logger.info(f"Cert already installed on Android device {udid}")
        # Update state cache
        if device_name is None:
            device_name = await _get_device_name(controller, udid)
        fingerprint = get_cert_fingerprint(cert_path)
        cert_state = DeviceCertState(
            name=device_name,
            cert_installed=True,
            fingerprint=fingerprint,
            verified_at=datetime.now(UTC).isoformat(),
        )
        update_cert_state(udid, cert_state.model_dump())
        return False

    # Install system cert
    try:
        await adb.install_system_cert(udid, cert_path)
    except Exception as e:
        raise RuntimeError(f"Failed to install cert on Android device {udid}: {e}") from e

    # Update state
    fingerprint = get_cert_fingerprint(cert_path)
    if device_name is None:
        device_name = await _get_device_name(controller, udid)
    now = datetime.now(UTC).isoformat()

    cert_state = DeviceCertState(
        name=device_name,
        cert_installed=True,
        fingerprint=fingerprint,
        installed_at=now,
        verified_at=now,
    )
    update_cert_state(udid, cert_state.model_dump())

    # Also set HTTP proxy for emulators (10.0.2.2 = host loopback)
    if controller._device_type(udid) == DeviceType.ANDROID_EMULATOR:
        try:
            await adb.set_http_proxy(udid, "10.0.2.2", 9101)
            logger.info(f"Configured HTTP proxy on Android emulator {udid}")
        except Exception as e:
            logger.warning(f"Failed to set HTTP proxy on {udid}: {e}")

    logger.info(f"Installed mitmproxy CA cert on Android device {udid}")
    return True


async def get_device_cert_state(
    controller, udid: str, *, device_name: str | None = None,
) -> DeviceCertState:
    """Get certificate installation state for a device.

    Args:
        controller: DeviceController instance
        udid: Device UDID
        device_name: Pre-resolved device name to avoid redundant list_devices calls.

    Returns:
        DeviceCertState with current installation status
    """
    cert_path = get_cert_path()
    if device_name is None:
        device_name = await _get_device_name(controller, udid)

    if not cert_path.exists():
        # Cert file doesn't exist
        return DeviceCertState(
            name=device_name,
            cert_installed=False,
            fingerprint=None,
            verified_at=datetime.now(UTC).isoformat(),
        )

    is_installed = await is_cert_installed(
        controller, udid, device_name=device_name,
    )
    fingerprint = get_cert_fingerprint(cert_path) if is_installed else None

    # Get timestamps from persistent state
    cached = read_cert_state_for_device(udid) or {}

    return DeviceCertState(
        name=device_name,
        cert_installed=is_installed,
        fingerprint=fingerprint,
        installed_at=cached.get("installed_at"),
        verified_at=datetime.now(UTC).isoformat(),
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
