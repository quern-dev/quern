"""Daemon mode for Quern Debug Server.

Spawns the server as a detached background process and waits for it to
become healthy before the parent exits.

Uses subprocess.Popen (posix_spawn) instead of os.fork() — on macOS, the
ObjC runtime aborts forked children of multi-threaded processes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from server.config import CONFIG_DIR
from server.lifecycle.state import is_server_healthy, read_state, remove_state

LOG_FILE = CONFIG_DIR / "server.log"


def daemonize(server_port: int) -> None:
    """Spawn the server as a detached background process.

    Re-invokes the current command with ``--foreground`` so the child
    runs the normal foreground code path with its own fresh Python
    interpreter (no fork).  The parent waits for the server to become
    healthy, prints status, and calls ``sys.exit()``.

    This function never returns — it always calls ``sys.exit()`` in the
    parent.  The caller must check ``args.foreground`` and skip this
    call when already in foreground mode.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull_fd = os.open(os.devnull, os.O_RDONLY)

    # Build the child command: same Python, same module, force foreground.
    # Strip any existing -f/--foreground from argv (shouldn't be there, but
    # be safe), then pass through all remaining args after the subcommand.
    passthrough = [
        a for a in sys.argv[2:]
        if a not in ("-f", "--foreground")
    ]
    child_cmd = [sys.executable, "-m", "server", "start", "--foreground"] + passthrough

    proc = subprocess.Popen(
        child_cmd,
        stdin=devnull_fd,
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,  # equivalent to os.setsid()
    )

    os.close(log_fd)
    os.close(devnull_fd)

    _parent_wait_and_exit(proc.pid, server_port)


def _parent_wait_and_exit(child_pid: int, server_port: int) -> None:
    """Parent process: poll for server health, print status, and exit.

    Uses a two-phase approach:
    - Phase 1: Quick 0.5s HTTP timeout (port not yet bound)
    - Phase 2: Once the port accepts connections but hasn't responded yet,
      switch to a single long timeout for the remaining deadline (uvicorn is
      bound but the lifespan startup hasn't finished)
    """
    timeout = 30.0
    interval = 0.1
    start = time.monotonic()
    deadline = start + timeout
    debug = os.environ.get("QUERN_DEBUG_STARTUP")

    while time.monotonic() < deadline:
        time.sleep(interval)

        # Check if child is still alive
        try:
            os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            print("Error: Server process exited unexpectedly", file=sys.stderr)
            sys.exit(1)

        # Use remaining time as timeout — once uvicorn binds the port,
        # the connection succeeds but the request blocks until startup
        # completes. A short timeout wastes the entire budget on retries.
        remaining = deadline - time.monotonic()
        check_timeout = min(remaining, 2.0) if remaining > 0 else 0.5
        healthy = is_server_healthy(server_port, timeout=check_timeout)
        if debug:
            elapsed = time.monotonic() - start
            print(f"  [{elapsed:.1f}s] health: {'OK' if healthy else 'waiting'}",
                  file=sys.stderr)
        if healthy:
            state = read_state()
            if state:
                _print_status(state)
                sys.exit(0)

    elapsed_total = time.monotonic() - start
    print(f"Warning: Server started (pid {child_pid}) but health check timed out "
          f"after {elapsed_total:.1f}s", file=sys.stderr)
    print(f"Check logs: {LOG_FILE}", file=sys.stderr)
    sys.exit(0)


def _print_status(state: dict) -> None:
    """Print formatted server status."""
    pid = state.get("pid", "?")
    local_ip = state.get("local_ip") or state.get("server_host", "0.0.0.0")
    port = state.get("server_port", "?")
    api_key = state.get("api_key", "")
    proxy_port = state.get("proxy_port", "?")
    proxy_enabled = state.get("proxy_enabled", False)

    key_display = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else api_key

    print(f"Quern Debug Server running")
    print(f"  PID:        {pid}")
    print(f"  Server:     http://{local_ip}:{port}")
    if proxy_enabled:
        proxy_status = state.get("proxy_status", "unknown")
        print(f"  Proxy:      port {proxy_port} ({proxy_status})")
        local_capture = state.get("local_capture", [])
        if local_capture:
            names = ", ".join(local_capture) if isinstance(local_capture, list) else "enabled"
            print(f"  Local capture: {names}")
        else:
            print(f"  Local capture: disabled (run: quern enable-local-capture)")
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
