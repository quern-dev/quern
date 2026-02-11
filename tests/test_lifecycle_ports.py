"""Tests for server.lifecycle.ports — port availability and scanning."""

from __future__ import annotations

import socket

import pytest

from server.lifecycle.ports import is_port_available, find_available_port


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
