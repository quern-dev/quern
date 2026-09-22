#!/usr/bin/env python3
"""Example UI automation script that works with run-parallel.py

The coordinator passes device info via environment variables:
- DEVICE_UDID: The device UDID to target
- DEVICE_NAME: The device name
- DEVICE_INDEX: The parallel execution index (0, 1, 2, 3...)
"""

import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import httpx


def _discover_server_url() -> str:
    """The running server's base URL, from the state file it publishes.

    `~/.quern/state.json` is the contract every consumer shares -- the MCP
    wrapper reads the same field. Loopback rather than the recorded bind
    host, which is 0.0.0.0 by default.
    """
    state_path = Path.home() / ".quern" / "state.json"
    try:
        state = json.loads(state_path.read_text())
        port = state["server_port"]
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(
            f"No running server found via {state_path} ({exc}).\n"
            "Start it with `quern start`, or set QUERN_SERVER_URL."
        ) from exc
    # The file can hold anything, including the remains of a server that is
    # gone. `isinstance(port, int)` would not do: True passes it, and so do
    # 0 and 70000.
    if type(port) is not int or not (1 <= port <= 65535):
        raise SystemExit(
            f"{state_path} records an unusable port ({port!r}).\n"
            "Start the server with `quern start`, or set QUERN_SERVER_URL."
        )
    # A recorded port is not a running server: a crash or a SIGKILL leaves
    # the file behind. Without this the requests below go to whatever is on
    # that port, or to nothing, and fail somewhere far from the cause.
    if not _server_is_up(port):
        raise SystemExit(
            f"{state_path} says port {port}, but nothing is answering there.\n"
            "Start the server with `quern start`, or set QUERN_SERVER_URL."
        )
    # Nor is answering proof of being Quern, and the next thing this script
    # does is send it an API key. Quern uses whatever port was free, so if it
    # dies, another local process can take the freed one and answer 200. The
    # recorded pid is what an impostor does not control.
    if not _listener_is(state, port):
        raise SystemExit(
            f"Something other than the Quern recorded in {state_path} is "
            f"listening on port {port}; refusing to send it the API key."
        )
    return f"http://127.0.0.1:{port}"


def _listener_is(state: dict, port: int) -> bool:
    """Whether the process listening on `port` is the one `state` records."""
    recorded = state.get("pid")
    if not isinstance(recorded, int):
        return True          # nothing to compare against; health check stands
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return True          # could not ask, so do not invent a refusal
    return not out or str(recorded) in out


def _server_is_up(port: int, timeout: float = 2.0) -> bool:
    """Whether something answers /health on the loopback port."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


async def run_test(device_udid: str, device_name: str, device_index: int):
    """Your UI automation test logic goes here."""

    # Where the server is. Read, never assumed: the port is whatever the
    # server settled on, which is not 9100 when something else already had
    # it. This example used to default to 9100 and would have quietly talked
    # to the wrong thing -- or nothing -- the first time that happened.
    #
    # From the shell, `eval "$(quern env)"` sets both of these for you.
    api_key = os.getenv("QUERN_API_KEY") or (
        Path.home() / ".quern" / "api-key"
    ).read_text().strip()
    server_url = os.getenv("QUERN_SERVER_URL") or _discover_server_url()

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
