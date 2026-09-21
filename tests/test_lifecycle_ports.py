"""Tests for server.lifecycle.ports — port availability and scanning."""

from __future__ import annotations

import socket

import pytest

from server.lifecycle.ports import find_available_port, is_port_available


def test_port_available_when_free():
    """A port that nobody is using should be available."""
    # Use a high ephemeral port unlikely to be in use
    assert is_port_available(59123) is True


def test_port_taken_when_bound():
    """A port that's already bound should not be available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 59124))
        s.listen(1)
        assert is_port_available(59124) is False


def test_find_available_returns_preferred_when_free():
    """find_available_port should return the preferred port if it's free."""
    port = find_available_port(59125)
    assert port == 59125


def test_find_available_skips_taken():
    """find_available_port should skip ports that are in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 59126))
        s.listen(1)
        port = find_available_port(59126)
        assert port == 59127


def test_find_available_respects_exclude():
    """find_available_port should skip ports in the exclude set."""
    port = find_available_port(59128, exclude={59128, 59129})
    assert port == 59130


def test_find_available_raises_when_exhausted():
    """find_available_port should raise RuntimeError when all ports are taken."""
    # Use max_attempts=1 with the port excluded
    with pytest.raises(RuntimeError, match="No available port found"):
        find_available_port(59131, max_attempts=1, exclude={59131})


class TestWhatCountsAsAQuernProcess:
    """`reclaim_port` SIGTERMs and then SIGKILLs whatever this says yes to,
    so a loose answer here is a loose answer to "may I kill that process".

    It used to say yes to any argv containing `uvicorn`. That is the most
    widely used ASGI server in Python, so an unrelated app holding the port
    was identified as a stale quern and killed by `quern start` — on 9100,
    which is also Prometheus node_exporter's default and the JetDirect
    printing port, so sharing it is ordinary rather than unlucky. Quern's own
    daemon never matched that pattern anyway: its argv is `<python> -m
    server`.
    """

    @staticmethod
    def _argv(argv: str) -> bool:
        from types import SimpleNamespace
        from unittest.mock import patch

        from server.lifecycle.ports import _is_quern_process

        with patch(
            "server.lifecycle.ports.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=argv),
        ):
            return _is_quern_process(1234)

    @pytest.mark.parametrize("argv", [
        "/opt/homebrew/bin/python3.12 -m server",
        "/Users/x/.local/share/quern/.venv/bin/python -m server --port 9100",
        "/usr/bin/python3 -m server.main",
    ])
    def test_ours_is_recognised(self, argv):
        assert self._argv(argv) is True, "a stale quern would never be reclaimed"

    @pytest.mark.parametrize("argv", [
        # The regression. Someone else's ASGI app, which quern would have
        # killed to take the port.
        "/usr/local/bin/uvicorn myapp:api --host 0.0.0.0 --port 9100",
        "/usr/bin/python3 -m uvicorn myapp:app",
        # A module whose name merely starts the same way.
        "/usr/bin/python3 -m serverfoo --port 9100",
        # node_exporter, which owns 9100 by convention.
        "/usr/local/bin/node_exporter --web.listen-address=:9100",
        "",
    ])
    def test_somebody_else_is_not_killed(self, argv):
        assert self._argv(argv) is False, f"quern would have killed: {argv}"

    def test_the_mitmdump_addon_has_to_be_ours(self):
        """An orphaned mitmdump running *our* addon is ours to clean up. A
        file that merely shares the path tail is not."""
        assert self._argv(
            "mitmdump -s /Users/x/.local/share/quern/server/proxy/addon.py"
        ) is True
        assert self._argv("mitmdump -s /Users/x/vendor/proxy/addon.py") is False
