"""The server stops its log sources together at shutdown (server/main.py).

Each source may spend up to its drain bound finishing what its stream had
written, and `quern stop` kills the server five seconds after asking. Stopped
one after another, a few slow sources ran past that.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from server.main import _stop_all


class _Source:
    def __init__(self, name: str, delay: float = 0.0, fails: bool = False):
        self.adapter_id = name
        self.delay = delay
        self.fails = fails
        self.stopped = False

    async def stop(self):
        await asyncio.sleep(self.delay)
        if self.fails:
            raise RuntimeError(f"{self.adapter_id} broke")
        self.stopped = True


@pytest.mark.asyncio
async def test_sources_stop_together_not_one_after_another():
    sources = [_Source(f"s{i}", delay=0.3) for i in range(4)]
    started = time.monotonic()
    await _stop_all(sources)
    elapsed = time.monotonic() - started

    assert all(s.stopped for s in sources)
    assert elapsed < 0.9, f"{elapsed:.2f}s: stopped in series, not together"


@pytest.mark.asyncio
async def test_one_failing_stop_does_not_skip_the_others(caplog):
    """In series, a stop() that raised ended the loop: every source after it
    was left running."""
    broken = _Source("broken", fails=True)
    after = _Source("after")
    with caplog.at_level(logging.ERROR, logger="server.main"):
        await _stop_all([broken, after])

    assert after.stopped
    assert any("broken" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_source_registered_twice_is_stopped_once():
    """A capture sits both among the sources and in its per-device registry."""
    calls = []

    class Counted(_Source):
        async def stop(self):
            calls.append(self.adapter_id)

    capture = Counted("simlog-A")
    await _stop_all([capture, _Source("other"), capture])
    assert calls == ["simlog-A"]
