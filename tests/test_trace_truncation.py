"""A trace that lost the start of its window must say so."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from server.api.trace import get_trace
from server.models import LogEntry, LogLevel, LogSource
from server.storage.ring_buffer import RingBuffer

BASE = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _entry(at_s):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=at_s),
        device_id="SIM-A", process="MyApp", level=LogLevel.INFO,
        message="x", source=LogSource.SIMULATOR,
    )


async def _call(ring, since):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        server_buffer=RingBuffer(max_size=10), ring_buffer=ring, flow_store=None,
    )))
    return await get_trace(request=request, since=since, udid=None, limit=100)


class TestSilentEvictionIsReported:
    """The buffer is a deque with a maxlen and eviction counts nothing, so an
    incomplete trace is indistinguishable from a quiet one. The reader
    concludes the app logged nothing, when the entries were dropped."""

    async def test_a_full_buffer_that_starts_late_is_truncated(self):
        ring = RingBuffer(max_size=3)
        for at in (10, 11, 12):          # window asks from t=0
            await ring.append(_entry(at))

        result = await _call(ring, BASE)

        assert result["log_window_truncated"] is True

    async def test_a_full_buffer_reaching_back_far_enough_is_not(self):
        """Full is not the same as truncated. If the oldest surviving entry
        predates the window, nothing inside it was lost."""
        ring = RingBuffer(max_size=3)
        for at in (10, 11, 12):
            await ring.append(_entry(at))

        result = await _call(ring, BASE + timedelta(seconds=11))

        assert result["log_window_truncated"] is False

    async def test_a_buffer_with_room_left_is_never_truncated(self):
        ring = RingBuffer(max_size=100)
        await ring.append(_entry(10))

        result = await _call(ring, BASE)

        assert result["log_window_truncated"] is False
