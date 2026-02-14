#!/usr/bin/env python3
"""Parallel test coordinator - runs your UI automation script on multiple devices.

Usage:
    python run-parallel.py your_script.py --devices 4
    python run-parallel.py your_script.py --device-filter "iPhone 16"
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx


class DevicePoolCoordinator:
    """Manages claiming devices and running tests in parallel."""

    def __init__(self, server_url: str, api_key: str):
        self.server_url = server_url
        self.api_key = api_key
        self.session_id = f"parallel-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    async def claim_devices(self, count: int, device_filter: str = None) -> list[dict]:
        """Claim N devices from the pool."""
        async with httpx.AsyncClient() as client:
            # Get available devices
            params = {"state": "booted", "claimed": "available"}
            resp = await client.get(
                f"{self.server_url}/api/v1/devices/pool",
                headers={"Authorization": f"Bearer {self.api_key}"},
                params=params,
            )
            resp.raise_for_status()
            available = resp.json()["devices"]

            # Filter by name if specified
            if device_filter:
                available = [d for d in available if device_filter.lower() in d["name"].lower()]

            if len(available) < count:
                raise RuntimeError(
                    f"Not enough devices available. Requested {count}, found {len(available)}"
                )

            # Claim devices
            claimed = []
            for i in range(count):
                device = available[i]
                claim_resp = await client.post(
                    f"{self.server_url}/api/v1/devices/claim",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "session_id": f"{self.session_id}-{i}",
                        "udid": device["udid"],
                    },
                )
                claim_resp.raise_for_status()
                claimed.append(claim_resp.json()["device"])
                print(f"✓ Claimed: {device['name']} ({device['udid'][:8]}...)")

            return claimed

    async def release_devices(self, devices: list[dict]) -> None:
        """Release all claimed devices."""
        async with httpx.AsyncClient() as client:
            for i, device in enumerate(devices):
                try:
                    await client.post(
                        f"{self.server_url}/api/v1/devices/release",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={
                            "udid": device["udid"],
                            "session_id": f"{self.session_id}-{i}",
                        },
                    )
                    print(f"✓ Released: {device['name']}")
                except Exception as e:
                    print(f"✗ Failed to release {device['name']}: {e}")

    async def run_script_on_device(
        self, script_path: Path, device: dict, index: int
    ) -> tuple[int, str, str]:
        """Run the user's script with device UDID as environment variable."""
        import os

        env = os.environ.copy()
        env["DEVICE_UDID"] = device["udid"]
        env["DEVICE_NAME"] = device["name"]
        env["DEVICE_INDEX"] = str(index)

        print(f"[Device {index}] Starting: {device['name']}")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            print(f"[Device {index}] ✓ Success: {device['name']}")
        else:
            print(f"[Device {index}] ✗ Failed: {device['name']} (exit {proc.returncode})")

        return proc.returncode, stdout.decode(), stderr.decode()

    async def run_parallel(
        self, script_path: Path, device_count: int, device_filter: str = None
    ) -> dict:
        """Main coordinator: claim devices, run tests in parallel, release devices."""
        print(f"\n=== Parallel Test Coordinator ===")
        print(f"Script: {script_path}")
        print(f"Devices: {device_count}")
        if device_filter:
            print(f"Filter: {device_filter}")
        print()

        devices = None
        try:
            # Claim devices
            print("Claiming devices...")
            devices = await self.claim_devices(device_count, device_filter)
            print()

            # Run tests in parallel
            print("Running tests in parallel...")
            tasks = [
                self.run_script_on_device(script_path, device, i)
                for i, device in enumerate(devices)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect results
            successes = sum(1 for r in results if isinstance(r, tuple) and r[0] == 0)
            failures = sum(1 for r in results if isinstance(r, tuple) and r[0] != 0)
            errors = sum(1 for r in results if isinstance(r, Exception))

            print()
            print(f"=== Results ===")
            print(f"✓ Success: {successes}")
            print(f"✗ Failed: {failures}")
            print(f"✗ Errors: {errors}")

            return {
                "total": len(results),
                "successes": successes,
                "failures": failures,
                "errors": errors,
                "results": results,
            }

        finally:
            # Always release devices
            if devices:
                print()
                print("Releasing devices...")
                await self.release_devices(devices)


async def main():
    parser = argparse.ArgumentParser(
        description="Run UI automation script on multiple devices in parallel"
    )
    parser.add_argument("script", type=Path, help="Python script to run")
    parser.add_argument(
        "--devices", "-n", type=int, default=4, help="Number of devices (default: 4)"
    )
    parser.add_argument(
        "--device-filter", "-f", help="Filter devices by name (e.g., 'iPhone 16')"
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:9100", help="Quern server URL"
    )
    parser.add_argument("--api-key", help="API key (default: read from ~/.quern/api-key)")

    args = parser.parse_args()

    # Validate script exists
    if not args.script.exists():
        print(f"Error: Script not found: {args.script}")
        sys.exit(1)

    # Load API key
    if args.api_key:
        api_key = args.api_key
    else:
        api_key_file = Path.home() / ".quern" / "api-key"
        if not api_key_file.exists():
            print("Error: API key not found. Pass --api-key or ensure ~/.quern/api-key exists")
            sys.exit(1)
        api_key = api_key_file.read_text().strip()

    # Run coordinator
    coordinator = DevicePoolCoordinator(args.server, api_key)
    results = await coordinator.run_parallel(args.script, args.devices, args.device_filter)

    # Exit with failure if any tests failed
    if results["failures"] > 0 or results["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
