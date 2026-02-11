"""Port availability checking and scanning."""

from __future__ import annotations

import socket

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
