"""Port availability checking and scanning."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time

DEFAULT_SERVER_PORT = 9100
DEFAULT_PROXY_PORT = 9101
MAX_PORT_SCAN = 20


def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is available for binding."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def _get_pid_on_port(port: int) -> int | None:
    """Find the PID of the process listening on a port, or None."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        # lsof may return multiple PIDs (one per line); take the first
        return int(result.stdout.strip().splitlines()[0])
    except Exception:
        return None


def _is_quern_process(pid: int) -> bool:
    """Check if a PID belongs to a quern-debug-server process."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        cmd = result.stdout.strip()
        return (
            "quern-debug-server" in cmd
            or "quern" in cmd
            or "server.main" in cmd
            or "uvicorn" in cmd
            or ("-m server" in cmd and "ython" in cmd)
        )
    except Exception:
        return False


def reclaim_port(port: int, host: str = "127.0.0.1") -> bool:
    """Try to reclaim a port occupied by a stale quern process.

    If the port is occupied by a quern process (zombie from a previous run),
    kills it and waits for the port to become available.

    Returns True if the port is available (either was free, or we reclaimed it).
    Returns False if the port is occupied by a non-quern process.
    """
    if is_port_available(port, host):
        return True

    pid = _get_pid_on_port(port)
    if pid is None:
        # Port is busy but we can't identify the holder — wait briefly
        # (might be TIME_WAIT or a race)
        time.sleep(0.5)
        return is_port_available(port, host)

    if not _is_quern_process(pid):
        return False

    # It's a stale quern process — kill it
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return is_port_available(port, host)

    # Wait for it to die
    for _ in range(30):  # 3 seconds
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        # Still alive — force kill
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except (ProcessLookupError, PermissionError):
            pass

    return is_port_available(port, host)


def find_available_port(
    preferred: int,
    host: str = "127.0.0.1",
    max_attempts: int = MAX_PORT_SCAN,
    exclude: set[int] | None = None,
) -> int:
    """Find an available port, starting from preferred and scanning upward.

    Args:
        preferred: The preferred port to try first.
        host: The host to bind to.
        max_attempts: Maximum number of ports to try.
        exclude: Set of ports to skip.

    Returns:
        An available port number.

    Raises:
        RuntimeError: If no available port is found within max_attempts.
    """
    exclude = exclude or set()

    for offset in range(max_attempts):
        port = preferred + offset
        if port in exclude:
            continue
        if is_port_available(port, host):
            return port

    raise RuntimeError(
        f"No available port found in range {preferred}-{preferred + max_attempts - 1}"
    )
