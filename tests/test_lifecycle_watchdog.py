"""Tests for server.lifecycle.watchdog — proxy health monitor."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.lifecycle.watchdog import proxy_watchdog


class FakeProcess:
    """Minimal subprocess mock."""

    def __init__(self, returncode=None):
        self.returncode = returncode


class FakeAdapter:
    """Minimal ProxyAdapter mock."""

    def __init__(self, running=True, process=None):
        self._running = running
        self._process = process
        self._error = None


@pytest.mark.asyncio
async def test_healthy_proxy_keeps_running():
    """Watchdog should keep looping when proxy is healthy."""
    proc = FakeProcess(returncode=None)  # Still running
    adapter = FakeAdapter(running=True, process=proc)

    # Run watchdog for a few cycles, then cancel
    task = asyncio.create_task(
        proxy_watchdog(lambda: adapter, check_interval=0.01)
    )
    await asyncio.sleep(0.05)  # Let it loop a few times
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Adapter should still be running
    assert adapter._running is True
    assert adapter._error is None


@pytest.mark.asyncio
async def test_detects_crash():
    """Watchdog should detect proxy crash and update state."""
    proc = FakeProcess(returncode=None)
    adapter = FakeAdapter(running=True, process=proc)

    with patch("server.lifecycle.watchdog.update_state") as mock_update:
        task = asyncio.create_task(
            proxy_watchdog(lambda: adapter, check_interval=0.01)
        )

        # Simulate crash after a short delay
        await asyncio.sleep(0.03)
        proc.returncode = 1

        # Wait for watchdog to notice and break
        await asyncio.wait_for(task, timeout=1.0)

        assert adapter._running is False
        assert "exited with code 1" in adapter._error
        mock_update.assert_called_once_with(proxy_status="crashed")


@pytest.mark.asyncio
async def test_skips_when_not_running():
    """Watchdog should skip checks when adapter is not running."""
    adapter = FakeAdapter(running=False, process=None)

    task = asyncio.create_task(
        proxy_watchdog(lambda: adapter, check_interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert adapter._error is None


@pytest.mark.asyncio
async def test_skips_when_adapter_is_none():
    """Watchdog should handle None adapter gracefully."""
    task = asyncio.create_task(
        proxy_watchdog(lambda: None, check_interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
