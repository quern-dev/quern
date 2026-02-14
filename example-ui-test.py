#!/usr/bin/env python3
"""Example UI automation script that works with run-parallel.py

The coordinator passes device info via environment variables:
- DEVICE_UDID: The device UDID to target
- DEVICE_NAME: The device name
- DEVICE_INDEX: The parallel execution index (0, 1, 2, 3...)
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx


async def run_test(device_udid: str, device_name: str, device_index: int):
    """Your UI automation test logic goes here."""

    # Read server config
    api_key = (Path.home() / ".quern" / "api-key").read_text().strip()
    server_url = os.getenv("QUERN_SERVER_URL", "http://127.0.0.1:9100")

    print(f"[{device_index}] Running test on {device_name} ({device_udid[:8]}...)")

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {api_key}"}

        # Example: Get screen summary
        resp = await client.get(
            f"{server_url}/api/v1/device/screen-summary",
            headers=headers,
            params={"udid": device_udid},
        )
        resp.raise_for_status()
        summary = resp.json()
        print(f"[{device_index}] Screen: {summary.get('summary', 'N/A')}")

        # Example: Tap an element
        resp = await client.post(
            f"{server_url}/api/v1/device/ui/tap-element",
            headers=headers,
            json={"label": "Settings", "udid": device_udid},
        )

        if resp.status_code == 200:
            print(f"[{device_index}] ✓ Tapped Settings button")
        elif resp.status_code == 404:
            print(f"[{device_index}] ⚠ Settings button not found (app not running?)")
        else:
            resp.raise_for_status()

        # Add more test steps here...
        await asyncio.sleep(1)  # Simulate test work

    print(f"[{device_index}] ✓ Test complete on {device_name}")


async def main():
    # Get device info from environment (set by coordinator)
    device_udid = os.getenv("DEVICE_UDID")
    device_name = os.getenv("DEVICE_NAME", "Unknown")
    device_index = int(os.getenv("DEVICE_INDEX", "0"))

    if not device_udid:
        print("Error: DEVICE_UDID not set. This script must be run via run-parallel.py")
        sys.exit(1)

    try:
        await run_test(device_udid, device_name, device_index)
    except Exception as e:
        print(f"[{device_index}] ✗ Test failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
