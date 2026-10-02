"""One timeline: what quern did, what the app sent, what it logged.

Reconstructing that by hand is what #84 cost a session doing. The pieces have
all been queryable separately for a while; this is the join, and
`server/trace.py` is where the attribution rules live and are explained.

The endpoint is deliberately thin. Everything interesting -- which flow
belongs to which action, when that cannot be decided, and how much to trust it
per regime -- is a pure function that can be tested without a server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from server import recording as recording_mod
from server.device.devicectl import canonical_device_id
from server.models import LogQueryParams, LogSource, TraceResponse, UtcDatetime
from server.trace import (
    APP_LOG_SOURCES,
    Attribution,
    Ownership,
    build_trace,
    device_of,
    identified_by,
    ip_to_udid,
    log_identified_by,
    owns,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["trace"])

#: How far back a trace reaches when the caller does not say. Long enough to
#: cover the thing that just went wrong, short enough not to return a session.
DEFAULT_WINDOW = timedelta(minutes=5)

#: `LogQueryParams.limit` refuses anything larger, and asking for more raises
#: inside the handler rather than returning a 4xx.
_MAX_QUERY_LIMIT = 1000


def _serialise(attribution: Attribution, ip_map: dict[str, tuple[str, bool]]) -> dict:
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
                # On every flow, not only the doubtful ones. A reader should
                # never have to infer the good case from silence.
                "identified_by": identified_by(flow, ip_map).value,
            }
            for flow in attribution.flows
        ],
        "logs": [
            {
                "timestamp": entry.timestamp.isoformat(),
                "level": entry.level.value,
                "process": entry.process,
                "message": entry.message,
                "identified_by": log_identified_by(entry).value,
            }
            for entry in attribution.logs
        ],
        # Both always present, even when empty. A reader should not have to
        # know whether the absence of a caveat means "firmly attributed" or
        # "this version does not report caveats".
        "overlaps": attribution.overlaps,
        "caveats": attribution.caveats,
    }


@router.get("/trace", response_model=TraceResponse)
async def get_trace(
    request: Request,
    since: UtcDatetime | None = None,
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
    # Annotated, so a direct call -- as the tests make -- gets None rather
    # than a truthy `Query` object that would send every call down the
    # recording path.
    recording: Annotated[str | None, Query(
        description=(
            "Build the trace from a recording instead of the live buffers: its id, or "
            "the directory it was written to. The same attribution, over a window the "
            "buffers no longer hold (#364)."
        ),
    )] = None,
    until: Annotated[UtcDatetime | None, Query(
        description="End of the window; with `recording`, default its end.",
    )] = None,
) -> dict:
    """Actions in a window, each with the flows and log lines it caused."""
    if recording:
        return await _trace_from_recording(request, recording, since, until, udid, limit)
    if until is not None and since is not None and until < since:
        raise HTTPException(status_code=400, detail="until is before since")
    # Canonicalised, because the caller may name a physical device by either
    # of its two identifiers while the action log stores only one. Comparing
    # the raw parameter returned an empty trace for a device that had just
    # been driven -- and an empty trace is indistinguishable from a quiet one,
    # which is the failure this whole endpoint exists to avoid. Measured:
    # `?udid=00008030-...` returned 0 actions for a phone whose two
    # screenshots were both in the buffer under `B34C4EE9-...`. See #270.
    if udid:
        udid = canonical_device_id(udid)

    # `since` is a `UtcDatetime`, so a naive value has already been read as
    # UTC by the time it gets here -- the coercion this endpoint used to do
    # inline now lives on the type, which is what stopped the other six
    # endpoints from returning 500 on the same well-formed request (#267).
    window_start = since or datetime.now(UTC) - DEFAULT_WINDOW

    server_buffer = request.app.state.server_buffer
    ring_buffer = request.app.state.ring_buffer
    # Crash reports have their own buffer so the firehose cannot evict them
    # (#255), which means a trace that read only `ring_buffer` would silently
    # lose the one entry it most exists to show.
    crash_buffer = request.app.state.crash_buffer
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
        and (until is None or e.timestamp <= until)
    ]
    # `limit` bounds what the caller gets back.
    #
    # An earlier version of this comment said the buffer query had bounded
    # "the wrong thing" because the udid filter runs afterwards. That was
    # wrong: `filter_entries` ignores `params.limit` entirely, so the query
    # never bounded anything at all -- as this file says correctly a few
    # lines below. The slice is the only bound there has ever been.
    #
    # The newest are kept, since a trace is read backwards from whatever just
    # went wrong. The response is still ordered oldest-first.
    actions_over_limit = len(actions) > limit
    if actions_over_limit:
        actions = actions[-limit:]

    # Actions can also age out, and the probe below only ever watched the
    # device-log buffer. Actions come from `server_buffer`, a separate and
    # much smaller RingBuffer that receives *every* server log record, not
    # only action entries -- so on a busy server the start of a window can be
    # gone while `log_window_truncated` says nothing, because it was never
    # about that buffer. "Quern did nothing for two minutes" and "the entries
    # aged out" then look identical.
    #
    # Answered by the buffer's own eviction record rather than a probe. The
    # probe here asked "is it full, and is the oldest survivor newer than the
    # window?", which was wrong in both directions: a buffer holding exactly
    # `max_size` entries with nothing ever evicted read as truncated, and one
    # purged back below full after evicting read as whole (#255).
    actions_truncated = not server_buffer.is_complete_since(window_start)

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
    # Discard what the trace will not use *before* bounding, or the bound is
    # spent on data that is then thrown away. `build_trace` keeps only
    # APP_LOG_SOURCES, so slicing first let newer build, proxy and server
    # entries push out older app logs and crash reports -- and the trace then
    # looked empty for a reason that had nothing to do with the app.
    device_logs = [e for e in device_logs if e.source in APP_LOG_SOURCES
                   and (until is None or e.timestamp <= until)]
    # And the caller's device, for the same reason. With `udid` set, bounding
    # first spends the budget on other devices' lines -- which attribution
    # then discards -- so a busy neighbour could empty this caller's trace.
    if udid:
        device_logs = [e for e in device_logs if not e.device_id or e.device_id == udid]
    logs_over_limit = len(device_logs) > log_limit
    if logs_over_limit:
        device_logs = device_logs[-log_limit:]

    # Crashes join after the bound, not before it. They come a handful per
    # session from a buffer of their own, and letting a burst of newer app
    # lines slice them off would undo the reason that buffer exists.
    crashes = await crash_buffer.filter_entries(LogQueryParams(since=window_start, until=until))
    if udid:
        crashes = [e for e in crashes if not e.device_id or e.device_id == udid]
    device_logs = sorted(device_logs + crashes, key=lambda e: e.timestamp)

    # Did the window outlive the buffers?
    #
    # Answered from what each buffer evicted rather than probed from what
    # survived. The probe that stood here asked whether the buffer was full
    # and its oldest survivor newer than the window, and it was wrong both
    # ways: entries are not in timestamp order (a crash is stamped when it
    # happened, a physical device by its own clock), so an old-stamped
    # survivor hid newer evictions; a buffer purged below full after evicting
    # read as whole; and one holding exactly `max_size` with nothing evicted
    # read as truncated. Narrowed to the sources the trace uses, so build and
    # proxy lines being shed does not flag an app trace (#255).
    app_sources = APP_LOG_SOURCES - {LogSource.CRASH}
    truncated = not (
        ring_buffer.is_complete_since(window_start, app_sources)
        and crash_buffer.is_complete_since(window_start)
    )
    # Flows get the same treatment the logs already had, for the same two
    # reasons -- and they matter more here, because attribution compares every
    # flow against every action. Measured on this branch: 1,000 actions
    # against a full 5,000-flow store is ~0.95s of synchronous work inside an
    # async handler with no await, which stalls the event loop for every other
    # caller on the server.
    # Read before the flow filter, not after: the filter needs it to resolve a
    # physical device's flows at all.
    ip_map = await asyncio.to_thread(_ip_map)

    flows: list = []
    flows_over_limit = False
    flow_window_truncated = False
    if flow_store is not None:
        flows = await flow_store.get_since(window_start)
        if until is not None:
            flows = [f for f in flows if f.timestamp <= until]
        # Sorted, because the store is not. It is an OrderedDict in insertion
        # order, and a flow is inserted when it *completes* while its
        # `timestamp` is when the request *started* -- so any overlapping
        # requests, which is the normal case for an app, come back out of
        # order. Three things depended on the order being chronological and
        # none of them said so: the slice below kept the most recently
        # completed rather than the newest, the truncation probe read the
        # first-inserted as though it were the oldest and claimed truncation
        # that had not happened, and an endpoint whose premise is "one
        # timeline" returned its flows out of sequence.
        flows.sort(key=lambda f: f.timestamp)
        if udid:
            # Resolved the way attribution resolves it, via `device_of`, not
            # by reading `simulator_udid` directly. Only simulators have that
            # field; a physical device is identified by the `client_ip` its
            # recorded proxy config names. Matching on `simulator_udid` alone
            # made every physical-device flow on the server look unidentified,
            # so all of them survived a filter whose whole job is to spend the
            # bound on this caller -- and a second device on Wi-Fi could push
            # this one's flows out of its own trace.
            flows = [
                f for f in flows
                if owns(udid, device_of(f, ip_map)[0]) is not Ownership.FOREIGN
            ]
        flows_over_limit = len(flows) > log_limit
        if flows_over_limit:
            flows = flows[-log_limit:]
        # From the store's own eviction record, like the log buffers. The
        # probe that stood here -- full, and oldest survivor newer than the
        # window -- is the one #255 discredited: the store evicts in
        # completion order and a flow is stamped when it started, so a long
        # request from before the window survived and hid newer evictions.
        # Reproduced in review: a store of 3 holding t=10, 11, 12, then a flow
        # at t=-600, evicted t=10 and reported nothing lost.
        flow_window_truncated = not flow_store.is_complete_since(window_start)

    # In a thread. Attribution is pure CPU over plain data with nothing to
    # await, and it compares every flow and every log against every action --
    # measured at 0.25s for a full window even after bounding the inputs, and
    # ~0.95s before. That is the whole server's event loop, shared by every
    # other agent and device, stalled on one caller reading a trace.
    attributions = await asyncio.to_thread(
        build_trace, actions, flows, device_logs, ip_map=ip_map,
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
        "actions": [_serialise(a, ip_map) for a in attributions],
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
        # The flow path had none of these. A trace missing the requests that
        # explain a failure, with nothing saying they were dropped, is the
        # shape this whole file guards against -- and it was only guarded on
        # one of the two inputs.
        "actions_over_limit": actions_over_limit,
        "action_window_truncated": actions_truncated,
        "flows_over_limit": flows_over_limit,
        "flow_window_truncated": flow_window_truncated,
        # The adapter's own view, not "a flow store exists". The store is
        # created at startup and outlives a stopped proxy, so the previous
        # check reported True with capture off -- which is exactly the
        # "empty and broken look alike" failure this field exists to prevent.
        "proxy_running": _proxy_is_running(request),
    }


async def _trace_from_recording(request: Request, ref: str, since: datetime | None,
                                until: datetime | None, udid: str | None, limit: int) -> dict:
    """The trace over a recording: `build_trace`, unchanged, on what it wrote.

    The truncation fields mean what they mean live, answered from the file:
    a `dropped` or gap span overlapping the window sets the flag for what it
    lost, and `recording.holes` says where. Nothing in the file is inferred
    to be complete.
    """
    from server.api.recordings import recording_dir

    directory = recording_dir(request, ref)
    manager = getattr(request.app.state, "recordings", None)
    live = bool(manager and manager.is_live(directory))
    try:
        loaded = await asyncio.to_thread(recording_mod.load, directory, live=live)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"{directory} could not be read: {e}") from e
    udid = canonical_device_id(udid) if udid else loaded.udid

    def inside(at: datetime) -> bool:
        return (since is None or at >= since) and (until is None or at <= until)

    actions = sorted((a for a in loaded.actions
                      if a.outcome != "started" and inside(a.timestamp)
                      and (not udid or a.udid == udid)), key=lambda a: a.timestamp)
    actions_over_limit = len(actions) > limit
    if actions_over_limit:
        actions = actions[-limit:]
    flows = sorted((f for f in loaded.flows if inside(f.timestamp)), key=lambda f: f.timestamp)
    logs = sorted((e for e in loaded.logs if inside(e.timestamp)), key=lambda e: e.timestamp)
    ip_map = await asyncio.to_thread(_ip_map)
    attributions = await asyncio.to_thread(build_trace, actions, flows, logs, ip_map=ip_map)

    def holes(*kinds: str) -> list[str]:
        return recording_mod.holes_in(loaded, set(kinds), since, until)

    return {
        "since": (since or (min((a.timestamp for a in actions), default=None))
                  or datetime.now(UTC)).isoformat(),
        "udid": udid,
        # The recording's own anchor, not this server's now: a recording read
        # after a reboot, or on another machine, is on another monotonic base.
        # Each stretch's anchor is in `recording.clock_anchors`.
        "clock_anchor": ({k: v for k, v in loaded.clock_anchors[0].items() if k != "segment"}
                         if loaded.clock_anchors else
                         {"wall": datetime.now(UTC).isoformat(), "monotonic": time.monotonic()}),
        "actions": [_serialise(a, ip_map) for a in attributions],
        "log_window_truncated": bool(holes("log", "crash")),
        "logs_over_limit": False,
        "actions_over_limit": actions_over_limit,
        "action_window_truncated": bool(holes("action")),
        "flows_over_limit": False,
        "flow_window_truncated": bool(holes("flow")),
        # Not recorded: whether capture was on is a fact about the run, and
        # this server's proxy today says nothing about it.
        "proxy_running": None,
        "recording": {
            "directory": str(directory),
            "stopped": loaded.stopped,
            "holes": holes("action", "flow", "log", "crash"),
            "unreadable_lines": loaded.unreadable_lines,
            "clock_anchors": loaded.clock_anchors,
            # A reboot during the run: `started_monotonic` values on either
            # side of it are on different bases, so join each stretch with
            # its own anchor.
            "monotonic_resets": loaded.monotonic_resets,
            "warnings": loaded.warnings,
        },
    }


def read_ip_map() -> dict[str, tuple[str, bool]]:
    """Physical devices by the address they came from. Raises when the cert
    state cannot be read: a recording takes it from here and says so in the
    file, since it cannot be asked again later."""
    from server.proxy.cert_state import read_cert_state

    return ip_to_udid(read_cert_state())


def _ip_map() -> dict[str, tuple[str, bool]]:
    """Physical devices are identified by the address they came from.

    Best effort: a cert state that cannot be read costs the trace its
    physical-device attribution, which is worth less than failing the call.
    """
    try:
        return read_ip_map()
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
