"""Daemon mode for iOS Debug Server.

Forks the server into a background process, redirects stdio to a log file,
and waits for the server to become healthy before the parent exits.

IMPORTANT: daemonize() must be called BEFORE any asyncio event loop is
created — Python asyncio is not fork-safe.
"""

from __future__ import annotations

import os
import signal
import sys
import time

from server.config import CONFIG_DIR
from server.lifecycle.state import is_server_healthy, read_state, remove_state

LOG_FILE = CONFIG_DIR / "server.log"


def daemonize(server_port: int) -> None:
    """Fork the current process into a daemon.

    After this call:
    - The parent process waits for the server to become healthy, prints
      status, and calls sys.exit().
    - The child process returns normally (caller should proceed to start
      the server).

    Must be called before creating an asyncio event loop.
    """
    pid = os.fork()

    if pid > 0:
        # Parent process — wait for health check, then exit
        _parent_wait_and_exit(pid, server_port)
        # _parent_wait_and_exit always calls sys.exit()

    # Child process — become session leader, redirect stdio
    os.setsid()
    _redirect_stdio()
    # Child returns to caller


def _redirect_stdio() -> None:
    """Redirect stdin/stdout/stderr to /dev/null and log file.

    Redirects at the file descriptor level so subprocess output
    (e.g., mitmdump) is also captured.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Open targets
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    # Redirect: stdin <- /dev/null, stdout/stderr -> log file
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)

    # Close originals (they're now duped)
    os.close(devnull_fd)
    os.close(log_fd)


def _parent_wait_and_exit(child_pid: int, server_port: int) -> None:
    """Parent process: poll for server health, print status, and exit."""
    timeout = 5.0
    interval = 0.1
    elapsed = 0.0

    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval

        # Check if child is still alive
        try:
            os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            print(f"Error: Server process exited unexpectedly", file=sys.stderr)
            sys.exit(1)

        if is_server_healthy(server_port):
            state = read_state()
            if state:
                _print_status(state)
                sys.exit(0)

    print(f"Warning: Server started (pid {child_pid}) but health check timed out", file=sys.stderr)
    print(f"Check logs: {LOG_FILE}", file=sys.stderr)
    sys.exit(0)


def _print_status(state: dict) -> None:
    """Print formatted server status."""
    pid = state.get("pid", "?")
    port = state.get("server_port", "?")
    api_key = state.get("api_key", "")
    proxy_port = state.get("proxy_port", "?")
    proxy_enabled = state.get("proxy_enabled", False)

    key_display = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else api_key

    print(f"iOS Debug Server running")
    print(f"  PID:        {pid}")
    print(f"  Server:     http://127.0.0.1:{port}")
    if proxy_enabled:
        proxy_status = state.get("proxy_status", "unknown")
        print(f"  Proxy:      port {proxy_port} ({proxy_status})")
    else:
        print(f"  Proxy:      disabled")
    print(f"  API key:    {key_display}")
    print(f"  Log file:   {LOG_FILE}")


def install_signal_handlers(cleanup_fn) -> None:
    """Install SIGTERM/SIGINT handlers that run cleanup before exit.

    Args:
        cleanup_fn: A callable that performs cleanup (stop proxy, remove state).
                    Will be called from the signal handler context.
    """
    def handler(signum, frame):
        cleanup_fn()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
