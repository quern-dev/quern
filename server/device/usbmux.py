"""UsbmuxBackend — discovers pre-iOS 17 physical devices via pymobiledevice3 usbmux."""

from __future__ import annotations

import asyncio
import json
import logging

from server.models import DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.usbmux")


class UsbmuxBackend:
    """Discovers USB-connected iOS devices via pymobiledevice3 usbmux list.

    Only returns devices running iOS < 17, since iOS 17+ devices are already
    discovered by DevicectlBackend. This avoids the UDID identity problem
    (devicectl uses CoreDevice UUIDs, usbmux uses 40-char hex UDIDs).
    """

    def __init__(self) -> None:
        self._binary: str | None = None

    async def is_available(self) -> bool:
        """Check if pymobiledevice3 is installed."""
        return self._find_binary() is not None

    def _find_binary(self) -> str | None:
        """Find pymobiledevice3 binary, caching the result."""
        if self._binary is not None:
            return self._binary

        from server.device.tunneld import find_pymobiledevice3_binary

        path = find_pymobiledevice3_binary()
        if path:
            self._binary = str(path)
        return self._binary

    async def list_devices(self) -> list[DeviceInfo]:
        """List USB-connected devices with iOS < 17.

        Runs ``pymobiledevice3 --no-color usbmux list --usb`` and parses JSON stdout.
        Returns an empty list on any error (graceful degradation).
        """
        binary = self._find_binary()
        if not binary:
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "--no-color", "usbmux", "list", "--usb",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await proc.communicate()

            if proc.returncode != 0:
                return []
        except Exception:
            logger.debug("Failed to run pymobiledevice3 usbmux list", exc_info=True)
            return []

        try:
            raw = json.loads(stdout_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Failed to parse usbmux list JSON output")
            return []

        return self._parse_devices(raw)

    @staticmethod
    def _parse_devices(raw: list[dict]) -> list[DeviceInfo]:
        """Parse usbmux device list, filtering to iOS < 17 only."""
        devices: list[DeviceInfo] = []

        for entry in raw:
            product_version = entry.get("ProductVersion", "")
            if not product_version:
                continue

            # Filter: only include devices with major version < 17
            try:
                major = int(product_version.split(".")[0])
            except (ValueError, IndexError):
                continue

            if major >= 17:
                continue

            udid = entry.get("UniqueDeviceID", "")
            if not udid:
                continue

            name = entry.get("DeviceName", "Unknown")
            device_class = entry.get("DeviceClass", "")
            connection_type = entry.get("ConnectionType", "")

            devices.append(DeviceInfo(
                udid=udid,
                name=name,
                state=DeviceState.BOOTED,
                device_type=DeviceType.DEVICE,
                os_version=f"iOS {product_version}",
                connection_type=connection_type.lower() if connection_type else "usb",
                device_family=device_class,
                is_connected=True,
            ))

        return devices
