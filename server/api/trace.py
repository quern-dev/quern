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

from server.api.actions import logged_action
from server.models import LogQueryParams, LogSource
from server.trace import Attribution, build_trace, ip_to_udid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["trace"])

#: How far back a trace reaches when the caller does not say. Long enough to
#: cover the thing that just went wrong, short enough not to return a session.
DEFAULT_WINDOW = timedelta(minutes=5)


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
@logged_action("get_trace", category="logs")
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

    device_logs = await ring_buffer.filter_entries(
        LogQueryParams(since=window_start, limit=limit * 10),
    )
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
        # Said plainly rather than left to be inferred from empty lists: a
        # trace with no flows because the proxy was off looks identical to one
        # where nothing was requested.
        "proxy_running": flow_store is not None,
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
