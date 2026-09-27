"""In-memory store for captured HTTP flow records.

Uses an OrderedDict for FIFO eviction when the store reaches capacity.
All public methods are async with a lock to match the RingBuffer pattern.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime

from server.models import FlowQueryParams, FlowRecord
from server.storage.arrival import ArrivalClock
from server.storage.fanout import Fanout, Missed

#: How many devices' eviction marks are kept individually. Past this the
#: least recently evicted fold into a floor that applies to every device.
MAX_DEVICE_KEYS = 256


def _device_keys(simulator_udid: str | None, client_ip: str | None) -> list[str]:
    """The keys a flow's device is recorded under, one per field it carries."""
    keys = []
    if simulator_udid:
        keys.append(f"sim:{simulator_udid}")
    if client_ip:
        keys.append(f"ip:{client_ip}")
    return keys


class FlowStore:
    """Thread-safe in-memory store for HTTP flow records."""

    def __init__(self, max_size: int = 5_000, clock: ArrivalClock | None = None) -> None:
        self._flows: OrderedDict[str, FlowRecord] = OrderedDict()
        # Arrival numbers per flow id, for the summary cursor (#317). A flow
        # is stamped when its request started but stored when it finished, so
        # a timestamp cursor skipped every request still running when a
        # summary was taken. An update is a new arrival: it moves the flow to
        # the end here and gets a new number, so a delta returns it.
        self.clock = clock or ArrivalClock()
        self._seq: dict[str, int] = {}
        self._last_evicted_seq = 0
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._fanout: Fanout[FlowRecord] = Fanout(maxsize=1000)
        # What eviction has lost, the way RingBuffer records it (#255): a
        # count, and the newest *timestamp* among evicted flows. The store
        # evicts in completion order while a flow's timestamp is when its
        # request started, so the oldest survivor says nothing about what
        # went before it -- a long request that started early and finished
        # late survives and hides newer evictions behind it. The maximum is
        # exact regardless: nothing stamped after it was ever evicted.
        self._evicted = 0
        self._evicted_through: datetime | None = None
        # The same mark per device, under the fields a query filters on, so a
        # query for one simulator is not flagged because another device's
        # traffic was evicted (#318). A flow is recorded under each field it
        # carries -- a query on either will then see it.
        self._evicted_through_by_device: dict[str, datetime] = {}
        # When the map is trimmed, the marks it drops fold into this floor,
        # which every narrowed lookup takes the max with. Dropping a device's
        # key outright would make its queries read complete -- a false
        # all-clear bought to save a few bytes -- so trimming may only ever
        # make the answer more cautious.
        self._evicted_through_floor: datetime | None = None
        # New flows taken in, as opposed to updates of ones already held.
        # `size` is what survived; this is what arrived.
        self._added = 0

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
            else:
                self._added += 1
                if len(self._flows) >= self._max_size:
                    # Evict oldest, and remember it
                    gone_id, gone = self._flows.popitem(last=False)
                    self._record_eviction(gone)
                    self._last_evicted_seq = self._seq.pop(gone_id, self._last_evicted_seq)
            self._flows[flow.id] = flow
            self._seq[flow.id] = self.clock.tick()

        # Notify subscribers (outside lock to avoid deadlock). A slow one
        # loses this flow and the loss is counted, rather than the subscriber
        # being dropped without a word -- see server/storage/fanout.py.
        self._fanout.publish(flow)

    @property
    def evicted(self) -> int:
        return self._evicted

    def _record_eviction(self, gone: FlowRecord) -> None:
        """Note a flow leaving the store. Must be called under lock."""
        self._evicted += 1
        at = gone.timestamp
        if self._evicted_through is None or at > self._evicted_through:
            self._evicted_through = at
        for key in _device_keys(gone.simulator_udid, gone.client_ip):
            previous = self._evicted_through_by_device.pop(key, None)
            # Re-inserted, so dict order is least recently evicted first.
            self._evicted_through_by_device[key] = (
                at if previous is None or at > previous else previous
            )
        # One key per udid or client_ip ever evicted, for the life of the
        # server -- DHCP churn and simulator erase cycles only add. Bounded.
        while len(self._evicted_through_by_device) > MAX_DEVICE_KEYS:
            oldest = next(iter(self._evicted_through_by_device))
            dropped = self._evicted_through_by_device.pop(oldest)
            if self._evicted_through_floor is None or dropped > self._evicted_through_floor:
                self._evicted_through_floor = dropped

    def evicted_through(
        self, *, simulator_udid: str | None = None, client_ip: str | None = None,
    ) -> datetime | None:
        """The newest timestamp of any evicted flow, or None if none were.

        With `simulator_udid` or `client_ip`, only evictions of flows carrying
        that value count -- the way the same filter narrows a query.
        """
        keys = _device_keys(simulator_udid, client_ip)
        if not keys:
            return self._evicted_through
        stamps = [
            self._evicted_through_by_device[k] for k in keys
            if k in self._evicted_through_by_device
        ]
        if self._evicted_through_floor is not None:
            stamps.append(self._evicted_through_floor)
        return max(stamps) if stamps else None

    def is_complete_since(
        self,
        since: datetime | None,
        *,
        simulator_udid: str | None = None,
        client_ip: str | None = None,
    ) -> bool:
        """Does the store still hold every flow stamped at or after `since`?

        True is a guarantee. False means a flow stamped inside the window was
        evicted, not necessarily one a given filter would have matched.
        """
        through = self.evicted_through(simulator_udid=simulator_udid, client_ip=client_ip)
        if through is None:
            return True
        return since is not None and since > through

    def stats(self) -> dict:
        """What the store holds, what it has taken in, and what it lost."""
        stamps = [f.timestamp for f in self._flows.values()]
        return {
            "capacity": self._max_size,
            "size": len(self._flows),
            "added": self._added,
            "evicted": self._evicted,
            "evicted_through": (
                self._evicted_through.isoformat() if self._evicted_through else None
            ),
            "oldest": min(stamps).isoformat() if stamps else None,
            "newest": max(stamps).isoformat() if stamps else None,
        }

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
            self._seq.clear()

    @property
    def last_evicted_seq(self) -> int:
        """Arrival number of the latest evicted flow; 0 if none ever were."""
        return self._last_evicted_seq

    async def flows_between(self, after: int, upto: int) -> list[FlowRecord]:
        """Flows whose latest arrival is after `after` and no later than `upto`."""
        async with self._lock:
            return [
                f for fid, f in self._flows.items()
                if after < self._seq.get(fid, 0) <= upto
            ]

    async def get_since(self, since: datetime) -> list[FlowRecord]:
        """Return all flows with timestamp > since."""
        async with self._lock:
            return [f for f in self._flows.values() if f.timestamp > since]

    async def get_all(self) -> list[FlowRecord]:
        """Return all flows (snapshot under lock)."""
        async with self._lock:
            return list(self._flows.values())

    def subscribe(
        self, accept: Callable[[FlowRecord], bool] | None = None,
    ) -> asyncio.Queue[FlowRecord]:
        """Create a subscription queue for real-time SSE streaming.

        Returns a queue that will receive new flows as they arrive.
        Caller must call unsubscribe() when done.
        """
        return self._fanout.subscribe(accept)

    def unsubscribe(self, queue: asyncio.Queue[FlowRecord]) -> None:
        """Remove a subscription queue."""
        self._fanout.unsubscribe(queue)

    def dropped(self, queue: asyncio.Queue[FlowRecord]) -> int:
        """Flows this subscriber missed because its queue was full."""
        return self._fanout.dropped(queue)

    def missed(self, queue: asyncio.Queue[FlowRecord]) -> Missed:
        """What this subscriber missed: the count and the span of timestamps."""
        return self._fanout.missed(queue)

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
