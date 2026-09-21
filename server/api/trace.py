"""One timeline: what quern did, what the app sent, what it logged.

Reconstructing that by hand is what #84 cost a session doing. The pieces have
all been queryable separately for a while; this is the join, and
`server/trace.py` is where the attribution rules live and are explained.

The endpoint is deliberately thin. Everything interesting -- which flow
belongs to which action, when that cannot be decided, and how much to trust it
per regime -- is a pure function that can be tested without a server.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request

from server.models import LogQueryParams, LogSource
from server.trace import Attribution, build_trace, ip_to_udid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["trace"])

#: How far back a trace reaches when the caller does not say. Long enough to
#: cover the thing that just went wrong, short enough not to return a session.
DEFAULT_WINDOW = timedelta(minutes=5)

#: `LogQueryParams.limit` refuses anything larger, and asking for more raises
#: inside the handler rather than returning a 4xx.
_MAX_QUERY_LIMIT = 1000


def _serialise(attribution: Attribution) -> dict:
    action = attribution.action
    return {
        "action": action.action,
        "udid": action.udid,
        "outcome": action.outcome,
        "duration_ms": action.duration_ms,
        "category": action.category,
        # The whole interval, as a property of the record. `finished_at` alone
        # is a trap: entries are written when an action *ends*, so a consumer
        # placing a marker at it is late by the action's own duration -- and
        # that is not a constant to subtract out (measured 2369ms cold and
        # 129ms warm for the same tap on one simulator).
        "started_at": (
            action.timestamp - timedelta(milliseconds=action.duration_ms or 0)
        ).isoformat(),
        "finished_at": action.timestamp.isoformat(),
        # On time.monotonic(), the same base as mach absolute time, which is
        # what video capture stamps frames with. Published so a consumer
        # aligning against a recording needs no wall-clock conversion and
        # inherits none of its drift. End is this plus duration_ms.
        "started_monotonic": action.started_monotonic,
        "detail": action.message,
        "flows": [
            {
                "id": flow.id,
                "timestamp": flow.timestamp.isoformat(),
                "method": flow.request.method,
                "url": flow.request.url,
                "status": flow.response.status_code if flow.response else None,
                "source_process": flow.source_process,
            }
            for flow in attribution.flows
        ],
        "logs": [
            {
                "timestamp": entry.timestamp.isoformat(),
                "level": entry.level.value,
                "process": entry.process,
                "message": entry.message,
            }
            for entry in attribution.logs
        ],
        # Both always present, even when empty. A reader should not have to
        # know whether the absence of a caveat means "firmly attributed" or
        # "this version does not report caveats".
        "overlaps": attribution.overlaps,
        "caveats": attribution.caveats,
    }


@router.get("/trace")
async def get_trace(
    request: Request,
    since: datetime | None = None,
    udid: str | None = Query(
        default=None,
        description=(
            "Only actions against this device. With several agents sharing "
            "one server this is how a caller gets its own trace: quern has no "
            "notion of who is asking, so the device is the only separator "
            "(see #254)."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Actions in a window, each with the flows and log lines it caused."""
    window_start = since or datetime.now(UTC) - DEFAULT_WINDOW

    server_buffer = request.app.state.server_buffer
    ring_buffer = request.app.state.ring_buffer
    flow_store = getattr(request.app.state, "flow_store", None)

    entries = await server_buffer.filter_entries(
        LogQueryParams(since=window_start, source=LogSource.SERVER, limit=limit),
    )
    # An action entry is one with an action name. `started` markers are the
    # DEBUG begin entries and are deliberately excluded: a trace wants one row
    # per thing that happened, and the pair only helps when something hung.
    actions = [
        e for e in entries
        if e.action and e.outcome != "started"
        and (not udid or e.udid == udid)
    ]
    # `limit` bounds what the caller gets back. Applying it only to the buffer
    # query bounded the wrong thing: the udid filter runs afterwards, so a
    # busy server could return far fewer actions than asked for, or -- with no
    # udid filter -- more work than the caller sized for. Newest first, since
    # a trace is read backwards from the thing that just went wrong.
    if len(actions) > limit:
        actions = actions[-limit:]

    # Clamped, not multiplied blindly: LogQueryParams caps `limit` at 1000, so
    # `limit * 10` raised a ValidationError inside the handler -- an uncaught
    # HTTP 500 for any caller passing limit > 100.
    #
    # The multiplier exists because one action can produce many log lines, so
    # asking for only `limit` entries would starve the attribution. Hitting the
    # ceiling is itself worth reporting rather than silently returning less.
    log_limit = min(limit * 10, _MAX_QUERY_LIMIT)
    device_logs = await ring_buffer.filter_entries(
        LogQueryParams(since=window_start, limit=log_limit),
    )
    # `filter_entries` returns everything that matches -- its docstring says
    # "ALL matching entries (no pagination)" -- so the limit above only stops
    # the query being rejected. Bounding the result is this slice.
    #
    # It is not only tidiness: attribution compares every log against every
    # action, so 1,000 actions against a full 10,000-entry buffer is ten
    # million comparisons on one request.
    logs_over_limit = len(device_logs) > log_limit
    if logs_over_limit:
        device_logs = device_logs[-log_limit:]

    # Did the window outlive the buffer?
    #
    # The ring buffer is a deque with a maxlen, shared by syslog, oslog,
    # crash, build and proxy. Eviction is silent -- nothing counts drops -- so
    # a trace asking for the last five minutes gets whatever survived and
    # looks identical whether or not anything was lost. A busy device can turn
    # over 10,000 entries in well under that.
    #
    # It is detectable without new bookkeeping: if the buffer is full and its
    # oldest surviving entry starts after the window did, the beginning of the
    # window has been evicted. Cheap, and a false negative at worst -- it
    # cannot claim truncation that did not happen.
    truncated = False
    if ring_buffer.size >= ring_buffer.max_size:
        oldest = await ring_buffer.get_recent(count=ring_buffer.size)
        if oldest and oldest[0].timestamp > window_start:
            truncated = True
    flows = await flow_store.get_since(window_start) if flow_store else []

    attributions = build_trace(
        actions, flows, device_logs, ip_map=_ip_map(),
    )

    return {
        "since": window_start.isoformat(),
        "udid": udid,
        # Read now, per export, rather than once at server start.
        #
        # Be careful what this claims. Wall and monotonic sit about 4.3s apart
        # on this machine and that gap was *stable* across every reading taken
        # -- no drift was observed, and an earlier note here asserting some
        # was wrong. It came from comparing two measurements that parsed
        # `kern.boottime` differently, one dropping its usec field: the
        # 0.218s "movement" was exactly that fraction. A sleep/wake
        # explanation was offered for it and is also unsupported.
        #
        # So this is not here because divergence was measured. It is here
        # because a recorded anchor is wrong the moment the wall clock is
        # stepped -- NTP correction, a manual change -- and a long-running
        # server has no way to notice. That failure is unbounded and silent,
        # and avoiding it costs two syscalls per export. A stable offset is
        # served correctly by reading it per export as well.
        "clock_anchor": {
            "wall": datetime.now(UTC).isoformat(),
            "monotonic": time.monotonic(),
        },
        "actions": [_serialise(a) for a in attributions],
        # Said rather than left to be inferred. An incomplete trace that looks
        # complete is worse than one that admits it: the reader concludes the
        # app logged nothing, when the entries were evicted.
        "log_window_truncated": truncated,
        # Deliberately separate from the above. That one means log lines were
        # evicted from the buffer before this trace asked for them; this one
        # means more matched than were returned. Same visible symptom --
        # fewer logs than really existed -- and different causes, so folding
        # them together would send a reader to the wrong fix.
        "logs_over_limit": logs_over_limit,
        # The adapter's own view, not "a flow store exists". The store is
        # created at startup and outlives a stopped proxy, so the previous
        # check reported True with capture off -- which is exactly the
        # "empty and broken look alike" failure this field exists to prevent.
        "proxy_running": _proxy_is_running(request),
    }


def _ip_map() -> dict[str, tuple[str, bool]]:
    """Physical devices are identified by the address they came from.

    Best effort: a cert state that cannot be read costs the trace its
    physical-device attribution, which is worth less than failing the call.
    """
    try:
        from server.proxy.cert_state import read_cert_state

        return ip_to_udid(read_cert_state())
    except Exception:
        logger.debug("Could not read cert state for ip attribution", exc_info=True)
        return {}


def _proxy_is_running(request: Request) -> bool:
    """Is capture actually on, rather than merely configured?

    The flow store is created at startup and survives the proxy stopping, so
    its existence says nothing. Asking the adapter is the difference between
    "no flows because nothing was requested" and "no flows because nothing was
    listening".
    """
    adapter = getattr(request.app.state, "proxy_adapter", None)
    if adapter is None:
        return False
    running = getattr(adapter, "is_running", None)
    if callable(running):
        return bool(running())
    if running is not None:
        return bool(running)
    return getattr(adapter, "_running", False) is True
