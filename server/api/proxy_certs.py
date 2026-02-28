"""API routes for proxy certificate management and setup guide."""

from __future__ import annotations

import logging
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from pydantic import BaseModel

from server.models import (
    CertInstallRequest,
    CertStatusResponse,
    CertVerifyRequest,
    CertVerifyResponse,
    DeviceCertInstallStatus,
    DeviceCertState,
    DeviceType,
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
async def download_cert(format: str = "cer") -> FileResponse:
    """Download the mitmproxy CA certificate for device installation.

    Args:
        format: "cer" (default) — triggers iOS/Android Install Profile dialog.
                "pem" — raw PEM for scripts, adb, curl, etc.
    """
    cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if not cert_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="CA certificate not found. Run mitmproxy once to generate it.",
        )

    if format == "pem":
        return FileResponse(
            path=str(cert_path),
            media_type="application/x-pem-file",
            filename="mitmproxy-ca-cert.pem",
        )
    else:  # "cer" — default, works for iOS and Android
        return FileResponse(
            path=str(cert_path),
            media_type="application/x-x509-ca-cert",
            filename="mitmproxy-ca-cert.cer",
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
    """Verify certificate installation on simulators and physical devices.

    For simulators: queries the SQLite TrustStore database (ground truth).
    For physical devices: checks for successful HTTPS flows through the proxy
    from the device's recorded client IP. If mitmproxy successfully intercepted
    HTTPS traffic, the cert must be installed and trusted.

    Updates persistent cert-state.json with results.

    Args:
        body.udid: Specific device UDID. If None, verifies all matching devices.

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

    device_map = {d.udid: d for d in all_devices}

    # Filter devices by state and device_type before determining UDIDs
    filtered_devices = all_devices
    if body.state:
        from server.models import DeviceState as DS
        filtered_devices = [d for d in filtered_devices if d.state.value == body.state]
    if body.device_type:
        from server.models import DeviceType as DT
        try:
            dt = DT(body.device_type)
            filtered_devices = [d for d in filtered_devices if d.device_type == dt]
        except ValueError:
            pass

    # Determine which devices to verify
    if body.udid:
        udids = [body.udid]
    else:
        udids = [d.udid for d in filtered_devices]

    if not udids:
        raise HTTPException(status_code=400, detail="No devices found")

    # Get cert fingerprint for truststore status checks
    cert_path = cert_manager.get_cert_path()
    fingerprint = None
    if cert_path.exists():
        try:
            fingerprint = cert_manager.get_cert_fingerprint(cert_path)
        except Exception:
            pass

    # Get flow store for physical device verification
    flow_store = getattr(request.app.state, "flow_store", None)

    # Verify each device
    device_statuses = []
    erased_devices = []
    for udid in udids:
        try:
            device_info = device_map.get(udid)
            is_physical = (
                device_info and device_info.device_type == DeviceType.DEVICE
            )
            name = device_info.name if device_info else "Unknown Device"

            if is_physical:
                # Physical device: verify via proxy flow store
                status_entry = await _verify_physical_device(
                    udid, name, fingerprint, flow_store,
                )
            else:
                # Simulator: existing TrustStore SQLite check
                status_entry = await _verify_simulator(
                    controller, udid, name, fingerprint, erased_devices,
                )

            device_statuses.append(status_entry)
        except Exception as e:
            _logger.warning(f"Failed to verify cert for {udid}: {e}")
            dev = device_map.get(udid)
            device_statuses.append(
                DeviceCertInstallStatus(
                    udid=udid,
                    name=dev.name if dev else "Unknown Device",
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


async def _verify_simulator(
    controller,
    udid: str,
    name: str,
    fingerprint: str | None,
    erased_devices: list[str],
) -> DeviceCertInstallStatus:
    """Verify cert on a simulator via TrustStore SQLite query."""
    prev_state = read_cert_state_for_device(udid)
    was_installed = prev_state.get("cert_installed", False) if prev_state else False

    cert_state = await cert_manager.get_device_cert_state(
        controller, udid, verify=True, device_name=name,
    )

    if fingerprint:
        status = cert_manager.check_truststore_status(udid, fingerprint)
    else:
        status = "not_installed"

    if was_installed and not cert_state.cert_installed:
        erased_devices.append(udid)

    return DeviceCertInstallStatus(
        udid=udid,
        name=cert_state.name,
        cert_installed=cert_state.cert_installed,
        fingerprint=cert_state.fingerprint,
        verified_at=cert_state.verified_at or datetime.now(timezone.utc).isoformat(),
        status=status,
    )


async def _verify_physical_device(
    udid: str,
    name: str,
    fingerprint: str | None,
    flow_store,
) -> DeviceCertInstallStatus:
    """Verify cert on a physical device by checking for successful HTTPS flows.

    If the device has a recorded client_ip in cert-state.json, queries the
    flow store for successful HTTPS flows from that IP. A successful TLS
    interception proves the cert is installed and trusted.
    """
    from server.models import FlowQueryParams
    from server.proxy.cert_state import update_cert_state

    now = datetime.now(timezone.utc).isoformat()
    cert_state = read_cert_state_for_device(udid) or {}

    wifi_configs: dict = cert_state.get("wifi_proxy_configs") or {}
    has_proxy_config = bool(wifi_configs)
    device_client_ip = None
    for cfg in wifi_configs.values():
        if cfg.get("client_ip"):
            device_client_ip = cfg["client_ip"]
            break

    # No proxy configured at all
    if not has_proxy_config:
        return DeviceCertInstallStatus(
            udid=udid,
            name=name,
            cert_installed=False,
            fingerprint=None,
            verified_at=now,
            status="proxy_not_configured",
        )

    # Proxy configured but no client IP recorded
    if not device_client_ip:
        return DeviceCertInstallStatus(
            udid=udid,
            name=name,
            cert_installed=cert_state.get("cert_installed", False),
            fingerprint=cert_state.get("fingerprint"),
            verified_at=now,
            status="unverified_no_client_ip",
        )

    # No flow store available (proxy not running)
    if flow_store is None:
        return DeviceCertInstallStatus(
            udid=udid,
            name=name,
            cert_installed=cert_state.get("cert_installed", False),
            fingerprint=cert_state.get("fingerprint"),
            verified_at=now,
            status="unverified_proxy_not_running",
        )

    # Query flow store for successful flows from this device's IP
    params = FlowQueryParams(
        client_ip=device_client_ip,
        has_error=False,
        limit=20,
    )
    flows, _total = await flow_store.query(params)

    # Check for any successful HTTPS flow (TLS interception worked)
    has_https_flow = any(f.tls is not None for f in flows)

    if has_https_flow:
        # Cert verified — update persistent state
        cert_state.update({
            "cert_installed": True,
            "fingerprint": fingerprint,
            "verified_at": now,
        })
        update_cert_state(udid, cert_state)

        return DeviceCertInstallStatus(
            udid=udid,
            name=name,
            cert_installed=True,
            fingerprint=fingerprint,
            verified_at=now,
            status="verified_via_traffic",
        )

    # Proxy configured, client IP known, but no HTTPS flows yet
    return DeviceCertInstallStatus(
        udid=udid,
        name=name,
        cert_installed=cert_state.get("cert_installed", False),
        fingerprint=cert_state.get("fingerprint"),
        verified_at=now,
        status="unverified_no_traffic",
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

    # Fetch all devices once to avoid repeated simctl calls
    try:
        all_devices = await controller.list_devices()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list devices: {e}")

    device_name_map = {d.udid: d.name for d in all_devices}

    # Determine which devices to install on
    if body.udid:
        udids = [body.udid]
    else:
        from server.models import DeviceState

        udids = [d.udid for d in all_devices if d.state == DeviceState.BOOTED]

    if not udids:
        raise HTTPException(status_code=400, detail="No booted devices found to install on")

    # Install on each device
    results = []
    for udid in udids:
        try:
            name = device_name_map.get(udid, "Unknown Device")
            was_installed = await cert_manager.install_cert(
                controller, udid, force=body.force, device_name=name,
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
# Device proxy config tracking
# ---------------------------------------------------------------------------


class RecordDeviceProxyRequest(BaseModel):
    udid: str
    ssid: str  # Wi-Fi network name (visible at top of Settings > Wi-Fi)
    client_ip: str | None = None  # Device's LAN IP (Settings > Wi-Fi > network > IP Address)


@router.post("/device-proxy-config")
async def record_device_proxy_config_endpoint(
    body: RecordDeviceProxyRequest,
    request: Request,
) -> dict:
    """Record the Wi-Fi proxy address that was configured on a physical device.

    Derives proxy_host by finding the Mac interface on the same /24 subnet as
    the device's client_ip. Falls back to detect_local_ip if client_ip is not
    provided. Call this after completing Wi-Fi proxy setup in device Settings.
    """
    from server.lifecycle.state import detect_local_ip, detect_host_ip_for_subnet
    from server.proxy.cert_state import record_device_proxy_config

    proxy_host = None
    if body.client_ip:
        proxy_host = detect_host_ip_for_subnet(body.client_ip)
    if not proxy_host:
        proxy_host = detect_local_ip()
    if not proxy_host:
        raise HTTPException(status_code=503, detail="Cannot detect local IP address.")

    adapter = getattr(request.app.state, "proxy_adapter", None)
    port = adapter.listen_port if adapter else 9101

    record_device_proxy_config(body.udid, body.ssid, proxy_host, port, client_ip=body.client_ip)
    return {
        "udid": body.udid,
        "ssid": body.ssid,
        "wifi_proxy_host": proxy_host,
        "wifi_proxy_port": port,
        "client_ip": body.client_ip,
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
        "so the proxy must be set BEFORE the simulator starts. "
        "(Note: this reboot requirement only applies to the system proxy approach. "
        "With local capture mode, certs take effect immediately — no reboot needed.)",
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

    api_port = request.app.state.config.port
    cert_url = f"http://{local_ip}:{api_port}/api/v1/proxy/cert"

    return {
        "proxy_host": local_ip,
        "proxy_port": port,
        "active_interface": active_interface,
        "warnings": warnings,
        "cert_install_url": cert_url,
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
                "note": "Physical devices are configured directly in iOS Settings. "
                        "Install and trust the certificate BEFORE configuring the proxy.",
                "instructions": [
                    f"1. Open Safari on the device and go to {cert_url}",
                    "2. Install the profile — Settings > General > VPN & Device Management",
                    "3. Trust the certificate — Settings > General > About > "
                    "Certificate Trust Settings",
                    "4. Go to Settings > Wi-Fi > (your network) > Configure Proxy > Manual",
                    f"5. Set Server: {local_ip}, Port: {port}",
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
