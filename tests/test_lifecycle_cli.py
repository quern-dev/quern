"""Integration tests for CLI lifecycle commands (start/stop/status).

These tests spawn real server processes via subprocess. Each test that starts
a daemon has a finally block to kill leftover processes.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Use the same Python interpreter that runs the tests
PYTHON = sys.executable
STATE_FILE = Path.home() / ".quern" / "state.json"


def _read_state() -> dict | None:
    """Read state.json, return None if missing or invalid."""
    try:
        if not STATE_FILE.exists():
            return None
        content = STATE_FILE.read_text().strip()
        if not content:
            return None
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return None


def _kill_server(state: dict | None = None) -> None:
    """Safety net: kill any server process from state.json."""
    if state is None:
        state = _read_state()
    if state and "pid" in state:
        pid = state["pid"]
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # Also clean up state file
    try:
        STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _wait_for_health(port: int, timeout: float = 5.0) -> bool:
    """Wait for the server to respond on the given port."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def _bind_port(port: int) -> socket.socket:
    """Bind a port to simulate it being in use. Returns the socket (caller must close)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


class TestStartDaemon:
    """Tests for `quern-debug-server start` (daemon mode)."""

    def setup_method(self):
        """Clean up any leftover state before each test."""
        _kill_server()

    def teardown_method(self):
        """Kill any leftover server process."""
        _kill_server()

    def test_start_creates_state_and_process(self):
        """start should create state.json and a running daemon."""
        result = subprocess.run(
            [PYTHON, "-m", "server.main", "start", "--no-proxy"],
            capture_output=True, text=True, timeout=15,
        )
        try:
            assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
            state = _read_state()
            assert state is not None, "state.json should exist"
            assert "pid" in state
            assert "server_port" in state
            assert "api_key" in state

            # Server should be healthy
            assert _wait_for_health(state["server_port"]), "Server should be responding"
        finally:
            _kill_server()

    def test_start_is_idempotent(self):
        """Starting twice should exit 0 with 'already running' message."""
        result1 = subprocess.run(
            [PYTHON, "-m", "server.main", "start", "--no-proxy"],
            capture_output=True, text=True, timeout=15,
        )
        try:
            assert result1.returncode == 0
            state = _read_state()
            assert state is not None

            # Second start
            result2 = subprocess.run(
                [PYTHON, "-m", "server.main", "start", "--no-proxy"],
                capture_output=True, text=True, timeout=10,
            )
            assert result2.returncode == 0
            assert "already running" in result2.stdout.lower()

            # PID should be unchanged
            state2 = _read_state()
            assert state2["pid"] == state["pid"]
        finally:
            _kill_server()

    def test_port_conflict_scans_next(self):
        """If preferred port is taken, start should scan upward."""
        # Use a high port to avoid TIME_WAIT from previous tests
        test_port = 59200
        sock = _bind_port(test_port)
        try:
            result = subprocess.run(
                [PYTHON, "-m", "server.main", "start", "--no-proxy", "--port", str(test_port)],
                capture_output=True, text=True, timeout=15,
            )
            try:
                assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
                state = _read_state()
                assert state is not None
                assert state["server_port"] != test_port, "Should have found a different port"
                assert _wait_for_health(state["server_port"])
            finally:
                _kill_server()
        finally:
            sock.close()


class TestStop:
    """Tests for `quern-debug-server stop`."""

    def setup_method(self):
        _kill_server()

    def teardown_method(self):
        _kill_server()

    def test_stop_kills_process_and_removes_state(self):
        """stop should terminate the daemon and remove state.json."""
        # Start first
        subprocess.run(
            [PYTHON, "-m", "server.main", "start", "--no-proxy"],
            capture_output=True, text=True, timeout=15,
        )
        state = _read_state()
        assert state is not None
        pid = state["pid"]

        # Stop
        result = subprocess.run(
            [PYTHON, "-m", "server.main", "stop"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "stopped" in result.stdout.lower() or "killed" in result.stdout.lower()

        # State file should be gone
        assert not STATE_FILE.exists() or _read_state() is None

        # Process should be gone
        try:
            os.kill(pid, 0)
            # If we get here, process is still alive — give it a moment
            time.sleep(1)
            os.kill(pid, 0)
            pytest.fail(f"Process {pid} should be terminated")
        except ProcessLookupError:
            pass  # Expected

    def test_stop_when_not_running(self):
        """stop with no server running should exit cleanly."""
        # Ensure no state file
        STATE_FILE.unlink(missing_ok=True)

        result = subprocess.run(
            [PYTHON, "-m", "server.main", "stop"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "no server running" in result.stdout.lower()


class TestStatus:
    """Tests for `quern-debug-server status`."""

    def setup_method(self):
        _kill_server()

    def teardown_method(self):
        _kill_server()

    def test_status_shows_running_server(self):
        """status should show info when server is running."""
        subprocess.run(
            [PYTHON, "-m", "server.main", "start", "--no-proxy"],
            capture_output=True, text=True, timeout=15,
        )
        state = _read_state()
        assert state is not None

        try:
            result = subprocess.run(
                [PYTHON, "-m", "server.main", "status"],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0
            assert str(state["server_port"]) in result.stdout
        finally:
            _kill_server()

    def test_status_when_not_running(self):
        """status with no server should exit 1."""
        STATE_FILE.unlink(missing_ok=True)

        result = subprocess.run(
            [PYTHON, "-m", "server.main", "status"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        assert "no server running" in result.stdout.lower()


class TestBackwardCompat:
    """Tests for backward compatibility (no subcommand)."""

    def test_no_args_is_foreground(self):
        """Running without subcommand should start in foreground mode."""
        # Start the server in foreground and kill it quickly
        proc = subprocess.Popen(
            [PYTHON, "-m", "server.main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Wait briefly for it to start writing state
            time.sleep(3)
            state = _read_state()
            # In foreground mode, it should still write state.json
            assert state is not None, "Foreground mode should write state.json"
            assert _wait_for_health(state["server_port"], timeout=5)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _kill_server()
