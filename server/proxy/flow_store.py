"""In-memory store for captured HTTP flow records.

Uses an OrderedDict for FIFO eviction when the store reaches capacity.
All public methods are async with a lock to match the RingBuffer pattern.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime

from server.models import FlowQueryParams, FlowRecord


class FlowStore:
    """Thread-safe in-memory store for HTTP flow records."""

    def __init__(self, max_size: int = 5_000) -> None:
        self._flows: OrderedDict[str, FlowRecord] = OrderedDict()
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[FlowRecord]] = []

    @property
    def size(self) -> int:
        return len(self._flows)

    @property
    def max_size(self) -> int:
        return self._max_size

    async def add(self, flow: FlowRecord) -> None:
        """Insert or update a flow record, evicting oldest if at capacity."""
        async with self._lock:
            if flow.id in self._flows:
                # Update existing — move to end
                del self._flows[flow.id]
            elif len(self._flows) >= self._max_size:
                # Evict oldest
                self._flows.popitem(last=False)
            self._flows[flow.id] = flow

        # Notify subscribers (outside lock to avoid deadlock)
        dead_subs: list[asyncio.Queue[FlowRecord]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(flow)
            except asyncio.QueueFull:
                dead_subs.append(queue)
        for dead in dead_subs:
            self._subscribers.remove(dead)

    async def get(self, flow_id: str) -> FlowRecord | None:
        """Look up a flow by ID."""
        async with self._lock:
            return self._flows.get(flow_id)

    async def query(self, params: FlowQueryParams) -> tuple[list[FlowRecord], int]:
        """Filter and paginate flows. Returns (page, total_matching)."""
        async with self._lock:
            results = self._filter(params)
            total = len(results)
            page = results[params.offset : params.offset + params.limit]
            return page, total

    async def clear(self) -> None:
        """Remove all flows."""
        async with self._lock:
            self._flows.clear()

    async def get_since(self, since: datetime) -> list[FlowRecord]:
        """Return all flows with timestamp > since."""
        async with self._lock:
            return [f for f in self._flows.values() if f.timestamp > since]

    async def get_all(self) -> list[FlowRecord]:
        """Return all flows (snapshot under lock)."""
        async with self._lock:
            return list(self._flows.values())

    def subscribe(self) -> asyncio.Queue[FlowRecord]:
        """Create a subscription queue for real-time SSE streaming.

        Returns a queue that will receive new flows as they arrive.
        Caller must call unsubscribe() when done.
        """
        queue: asyncio.Queue[FlowRecord] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[FlowRecord]) -> None:
        """Remove a subscription queue."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def _filter(self, params: FlowQueryParams) -> list[FlowRecord]:
        """Apply query filters. Returns newest-first. Must be called under lock."""
        results: list[FlowRecord] = []
        hosts_set = set(params.hosts) if params.hosts else None
        exclude_set = set(params.exclude_hosts) if params.exclude_hosts else None

        for flow in self._flows.values():
            if params.device_id and flow.device_id != params.device_id:
                continue
            if hosts_set and flow.request.host not in hosts_set:
                continue
            elif params.host and flow.request.host != params.host:
                continue
            if exclude_set and flow.request.host in exclude_set:
                continue
            if params.path_contains and params.path_contains not in flow.request.path:
                continue
            if params.method and flow.request.method.upper() != params.method.upper():
                continue
            if params.status_min is not None:
                if flow.response is None or flow.response.status_code < params.status_min:
                    continue
            if params.status_max is not None:
                if flow.response is None or flow.response.status_code > params.status_max:
                    continue
            if params.has_error is True and flow.error is None:
                continue
            if params.has_error is False and flow.error is not None:
                continue
            if params.simulator_udid and flow.simulator_udid != params.simulator_udid:
                continue
            if params.client_ip and flow.client_ip != params.client_ip:
                continue
            if params.since and flow.timestamp < params.since:
                continue
            if params.until and flow.timestamp > params.until:
                continue
            results.append(flow)

        # Newest first — most useful for debugging
        results.reverse()
        return results
