"""Proxy subprocess health monitor.

Watches the mitmdump subprocess and updates state.json if it dies unexpectedly.
Does NOT auto-restart — the agent decides what to do.
"""

from __future__ import annotations

import asyncio
import logging

from server.lifecycle.state import update_state

logger = logging.getLogger(__name__)


async def proxy_watchdog(
    get_proxy_adapter,
    check_interval: float = 1.0,
) -> None:
    """Monitor the proxy subprocess and update state on unexpected exit.

    Args:
        get_proxy_adapter: Callable that returns the ProxyAdapter instance.
        check_interval: How often to check the subprocess, in seconds.
    """
    while True:
        await asyncio.sleep(check_interval)

        adapter = get_proxy_adapter()
        if adapter is None:
            continue

        if not adapter._running:
            continue

        proc = adapter._process
        if proc is None:
            continue

        if proc.returncode is not None:
            # Subprocess exited unexpectedly
            logger.error(
                "Proxy subprocess exited unexpectedly with code %d",
                proc.returncode,
            )
            adapter._running = False
            adapter._error = f"mitmdump exited with code {proc.returncode}"
            update_state(proxy_status="crashed")
            break
