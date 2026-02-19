"""API routes for proxy certificate management and setup guide."""

from __future__ import annotations

import logging
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from server.models import (
    CertInstallRequest,
    CertStatusResponse,
    CertVerifyRequest,
    CertVerifyResponse,
    DeviceCertInstallStatus,
    DeviceCertState,
)
from server.proxy import cert_manager
from server.proxy.cert_state import read_cert_state, read_cert_state_for_device
from server.proxy.system_proxy import (
    detect_active_interface,
    get_default_route_device,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])


# ---------------------------------------------------------------------------
# Certificate management
# ---------------------------------------------------------------------------


@router.get("/cert")
async def download_cert() -> FileResponse:
    """Download the mitmproxy CA certificate for device installation."""
    cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if not cert_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="CA certificate not found. Run mitmproxy once to generate it.",
        )
    return FileResponse(
        path=str(cert_path),
        media_type="application/x-pem-file",
        filename="mitmproxy-ca-cert.pem",
    )


@router.get("/cert/status", response_model=CertStatusResponse)
async def cert_status(request: Request) -> CertStatusResponse:
    """Get certificate installation status for all devices.

    Returns cert file existence, fingerprint, and cached installation state
    for all known devices. Does not perform SQLite verification - use
    POST /cert/verify for ground truth.
    """
    cert_path = cert_manager.get_cert_path()
    cert_exists = cert_path.exists()

    fingerprint = None
    if cert_exists:
        try:
            fingerprint = cert_manager.get_cert_fingerprint(cert_path)
        except Exception as e:
            _logger.warning(f"Failed to get cert fingerprint: {e}")

    # Get cached device cert states from persistent cert-state.json
    device_certs_dict = read_cert_state()

    # Convert to DeviceCertState models
    devices = {
        udid: DeviceCertState(**cert_data)
        for udid, cert_data in device_certs_dict.items()
    }

    return CertStatusResponse(
        cert_exists=cert_exists,
        cert_path=str(cert_path),
        fingerprint=fingerprint,
        devices=devices,
    )


@router.post("/cert/verify", response_model=CertVerifyResponse)
async def verify_cert(request: Request, body: CertVerifyRequest) -> CertVerifyResponse:
    """Verify certificate installation via SQLite TrustStore query.

    Always performs ground-truth verification by querying the simulator's
    TrustStore database. Updates persistent cert-state.json with results.

    Works for both booted and shutdown simulators (TrustStore is readable on disk).

    Args:
        body.udid: Specific device UDID. If None, verifies all simulators.

    Returns:
        Detailed installation status per device with timestamps.
    """
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")

    # Fetch all devices once to avoid repeated simctl calls
    try:
        all_devices = await controller.list_devices()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list devices: {e}")

    device_name_map = {d.udid: d.name for d in all_devices}

    # Determine which devices to verify
    if body.udid:
        udids = [body.udid]
    else:
        udids = [d.udid for d in all_devices]

    if not udids:
        raise HTTPException(status_code=400, detail="No simulators found")

    # Get cert fingerprint for truststore status checks
    cert_path = cert_manager.get_cert_path()
    fingerprint = None
    if cert_path.exists():
        try:
            fingerprint = cert_manager.get_cert_fingerprint(cert_path)
        except Exception:
            pass

    # Verify each device
    device_statuses = []
    erased_devices = []
    for udid in udids:
        try:
            # Check previous state for erase detection
            prev_state = read_cert_state_for_device(udid)
            was_installed = prev_state.get("cert_installed", False) if prev_state else False

            name = device_name_map.get(udid, "Unknown Device")
            cert_state = await cert_manager.get_device_cert_state(
                controller, udid, verify=True, device_name=name,
            )

            # Determine detailed status
            if fingerprint:
                status = cert_manager.check_truststore_status(udid, fingerprint)
            else:
                status = "not_installed"

            # Detect erase
            if was_installed and not cert_state.cert_installed:
                erased_devices.append(udid)

            device_statuses.append(
                DeviceCertInstallStatus(
                    udid=udid,
                    name=cert_state.name,
                    cert_installed=cert_state.cert_installed,
                    fingerprint=cert_state.fingerprint,
                    verified_at=cert_state.verified_at or datetime.now(timezone.utc).isoformat(),
                    status=status,
                )
            )
        except Exception as e:
            _logger.warning(f"Failed to verify cert for {udid}: {e}")
            # Continue with other devices rather than failing completely
            device_statuses.append(
                DeviceCertInstallStatus(
                    udid=udid,
                    name=device_name_map.get(udid, "Unknown Device"),
                    cert_installed=False,
                    fingerprint=None,
                    verified_at=datetime.now(timezone.utc).isoformat(),
                    status="error",
                )
            )

    # All verified successfully if all devices have cert installed
    all_verified = all(status.cert_installed for status in device_statuses)

    return CertVerifyResponse(
        verified=all_verified,
        devices=device_statuses,
        erased_devices=erased_devices,
    )


@router.post("/cert/install")
async def install_cert(request: Request, body: CertInstallRequest) -> dict:
    """Install mitmproxy CA certificate on simulator(s).

    Idempotent - skips devices that already have the cert installed
    unless force=True.

    Args:
        body.udid: Specific device UDID. If None, installs on all booted devices.
        body.force: Force reinstall even if already present.

    Returns:
        Installation results per device.
    """
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")

    # Determine which devices to install on
    if body.udid:
        udids = [body.udid]
    else:
        # Install on all booted devices
        try:
            all_devices = await controller.list_devices()
            from server.models import DeviceState

            udids = [d.udid for d in all_devices if d.state == DeviceState.BOOTED]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list devices: {e}")

    if not udids:
        raise HTTPException(status_code=400, detail="No booted devices found to install on")

    # Install on each device
    results = []
    for udid in udids:
        try:
            was_installed = await cert_manager.install_cert(
                controller, udid, force=body.force
            )
            results.append({
                "udid": udid,
                "status": "installed" if was_installed else "already_installed",
                "success": True,
            })
        except Exception as e:
            _logger.error(f"Failed to install cert on {udid}: {e}")
            results.append({
                "udid": udid,
                "status": "failed",
                "success": False,
                "error": str(e),
            })

    success_count = sum(1 for r in results if r["success"])
    return {
        "total": len(results),
        "succeeded": success_count,
        "failed": len(results) - success_count,
        "devices": results,
    }


# ---------------------------------------------------------------------------
# Setup guide
# ---------------------------------------------------------------------------


@router.get("/setup-guide")
async def setup_guide(request: Request) -> dict:
    """Get device proxy setup instructions with auto-detected local IP and network interface."""
    adapter = request.app.state.proxy_adapter
    port = adapter.listen_port if adapter else 9101

    # Auto-detect local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    # Auto-detect active network interface name (macOS networksetup name)
    active_interface = _detect_active_interface()

    # Detect VPN and other potential issues
    warnings = _detect_proxy_warnings()

    # Get cert status for booted devices from persistent state
    cert_status_by_device = {}
    try:
        controller = request.app.state.device_controller
        if controller:
            all_devices = await controller.list_devices()
            from server.models import DeviceState

            booted_devices = [d for d in all_devices if d.state == DeviceState.BOOTED]

            # Load cached cert states from persistent cert-state.json
            device_certs = read_cert_state()

            for device in booted_devices:
                cached = device_certs.get(device.udid, {})
                cert_installed = cached.get("cert_installed", False)
                cert_status_by_device[device.udid] = {
                    "name": device.name,
                    "cert_installed": cert_installed,
                    "status_icon": "\u2713" if cert_installed else "\u2717",
                }
    except Exception as e:
        _logger.debug(f"Failed to get cert status: {e}")

    # Build simulator proxy commands with detected or placeholder interface
    iface_display = active_interface or "<interface>"
    set_proxy_cmd = (
        f'networksetup -setwebproxy "{iface_display}" 127.0.0.1 {port} && '
        f'networksetup -setsecurewebproxy "{iface_display}" 127.0.0.1 {port}'
    )
    clear_proxy_cmd = (
        f'networksetup -setwebproxystate "{iface_display}" off && '
        f'networksetup -setsecurewebproxystate "{iface_display}" off'
    )

    simulator_steps = []
    if not active_interface:
        simulator_steps.append(
            "0. Detect your active network interface: "
            "route -n get default | grep interface, then "
            "networksetup -listallhardwareports | grep -B1 <device>"
        )
    simulator_steps.extend([
        "1. If you have multiple network interfaces active (e.g. Wi-Fi + Ethernet), "
        "disable the one you're NOT using to avoid routing ambiguity.",
        f"2. Set proxy on the Mac's active network interface (NOT in simulator settings): "
        f"{set_proxy_cmd}",
        "3. Install the CA certificate into the simulator keychain: "
        "xcrun simctl keychain booted add-root-cert ~/.mitmproxy/mitmproxy-ca-cert.pem",
        "4. Reboot the simulator — it reads the host's proxy settings at boot time, "
        "so the proxy must be set BEFORE the simulator starts.",
        f"5. When done, disable the proxy: {clear_proxy_cmd}",
    ])

    # Add cert status to simulator steps if available
    if cert_status_by_device:
        cert_status_text = "\n".join([
            f"   {status['status_icon']} {status['name']}: "
            f"{'Cert installed' if status['cert_installed'] else 'Cert NOT installed'}"
            for status in cert_status_by_device.values()
        ])
        simulator_steps.insert(0, f"Certificate Status:\n{cert_status_text}")

    return {
        "proxy_host": local_ip,
        "proxy_port": port,
        "active_interface": active_interface,
        "warnings": warnings,
        "cert_install_url": "http://mitm.it",
        "cert_status": cert_status_by_device,
        "steps": [
            {
                "target": "simulator",
                "note": "The iOS Simulator inherits the Mac's network proxy settings. "
                        "You must configure the proxy on macOS, not inside the simulator.",
                "instructions": simulator_steps,
            },
            {
                "target": "physical_device",
                "note": "Physical devices are configured directly in iOS Settings.",
                "instructions": [
                    "1. Go to Settings > Wi-Fi > (your network) > Configure Proxy > Manual",
                    f"2. Set Server: {local_ip}, Port: {port}",
                    "3. Open Safari and go to http://mitm.it to download the CA certificate",
                    "4. Install the profile in Settings > General > VPN & Device Management",
                    "5. Trust the certificate in Settings > General > About > "
                    "Certificate Trust Settings",
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Helper functions (used by setup_guide)
# ---------------------------------------------------------------------------


def _detect_active_interface() -> str | None:
    """Delegate to system_proxy module (kept for local references)."""
    return detect_active_interface()


def _detect_proxy_warnings() -> list[str]:
    """Detect VPNs and network conditions that may interfere with mitmproxy.

    Checks:
    1. scutil --nc list — finds NetworkExtension/built-in VPNs with "Connected" status
    2. Default route interface — if it goes through a utun device, a VPN is routing traffic
    """
    warnings: list[str] = []

    # 1. Check scutil --nc list for connected VPNs
    connected_vpns = _get_connected_vpns()
    for vpn_name in connected_vpns:
        warnings.append(
            f"VPN detected: '{vpn_name}' is connected. "
            "This may route traffic around the proxy, causing flows to not appear."
        )

    # 2. Check if the default route goes through a utun interface
    default_device = get_default_route_device()
    if default_device and default_device.startswith("utun"):
        if not connected_vpns:
            # Only add this if we didn't already flag via scutil (avoid redundancy)
            warnings.append(
                f"Default route uses tunnel interface ({default_device}), "
                "which suggests a VPN is active. Traffic may bypass the proxy."
            )
        warnings.append(
            "Consider disconnecting the VPN before starting the proxy, "
            "or configure split tunneling to exclude the target traffic."
        )

    return warnings


def _get_connected_vpns() -> list[str]:
    """Parse `scutil --nc list` for VPN configurations with Connected status."""
    try:
        result = subprocess.run(
            ["scutil", "--nc", "list"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []

        vpns: list[str] = []
        for line in result.stdout.splitlines():
            # Lines look like:
            # * (Connected)      "My VPN" [com.apple.something]
            # * (Disconnected)   "Other VPN" [com.apple.something]
            if "(Connected)" not in line:
                continue
            # Extract the quoted name
            start = line.find('"')
            end = line.find('"', start + 1) if start != -1 else -1
            if start != -1 and end != -1:
                vpns.append(line[start + 1 : end])
        return vpns
    except Exception:
        return []


def _get_default_route_device() -> str | None:
    """Delegate to system_proxy module (kept for local references)."""
    return get_default_route_device()
