"""Joining quern's actions to the traffic and logs they caused.

The action log says what quern did and when. The flow store says what the app
sent. The device log says what the app printed. A trace is those three on one
timeline, with each flow and log line attributed to the action that caused it.

**There is no correlation id, and there cannot be one.** The proxy is a
separate `mitmdump` process and the requests are the app's, so nothing quern
controls travels with them. What saves this is that the flows already identify
themselves -- see `server/proxy/addon.py`, which reads the client's pid off
the connection and walks its parents to a `launchd_sim` carrying a UDID.

So the join is on what is already there, and how well it works depends on how
the device reaches the proxy:

| regime | joins by | tells apps apart? |
|---|---|---|
| simulator + local capture | `simulator_udid`, resolved from the pid | yes, `source_process` |
| physical device + Wi-Fi proxy | `client_ip`, via recorded proxy config | no |
| simulator + Wi-Fi proxy | nothing; interval only | no |

The last row is why `set_local_capture` is worth recommending to anyone who
wants a trace.

**Ambiguity is marked, never guessed.** Two actions running against one device
with no app to tell them apart produce overlapping intervals, and a flow
inside both genuinely cannot be attributed. Picking one would make the trace
confidently wrong, which is the failure this codebase keeps finding. See
docs/proposals/logging-spec.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from server.models import FlowRecord, LogEntry, LogSource

#: How long after a recorded `client_ip` we still believe it identifies a
#: device. It is written once, at proxy setup, and DHCP reassigns: a stale
#: mapping does not fail, it attributes another device's traffic to this one,
#: which is worse than not attributing it at all.
IP_MAPPING_TRUSTED_FOR = timedelta(days=7)

#: How long after an action ends a flow may still be attributed to it.
#:
#: Without this the trace answers almost nothing. Most actions hand work to
#: the device and return: `open_url` was measured finishing in 143ms with the
#: HTTP request it caused arriving 174ms *later*. A tap that triggers a fetch
#: and a launch that makes startup requests behave the same way. Attributing
#: only what happens *during* an action therefore misses the traffic that
#: action caused, which is the question a trace exists to answer.
#:
#: Three seconds is a guess informed by one measurement, and it is a trade:
#: too short and causation is missed, too long and unrelated traffic is
#: swept in. Anything attributed this way is marked, so a reader can tell
#: inference from observation.
#:
#: Deliberately not a parameter. The media-engine work (PR #164) is the one
#: consumer that might have wanted a hard interval, and asked for the causal
#: default instead: a keyframe is a seek point rather than a claim about what
#: is visible, so a late flow needs no second anchor. What the grace forbids
#: is rendering `[started_at, finished_at]` as though the visible result were
#: inside it -- which is a rendering rule, not a reason to make the window
#: configurable. A knob nobody needs is one that eventually gets set wrong.
CAUSAL_GRACE = timedelta(seconds=3)

#: Sources whose timestamps come from a physical device's clock rather than
#: the host's. Simulator sources are deliberately absent: a simulator runs on
#: the host clock, so there is no skew to declare and saying otherwise would
#: make the caveat noise.
_DEVICE_CLOCK_SOURCES = frozenset({LogSource.DEVICE, LogSource.LOGCAT})

#: What counts as "the app said this while the action ran".
#:
#: The ring buffer is shared -- syslog, oslog, crash, build and proxy entries
#: all land in it. Taking everything in the interval would put build output
#: inside a tap, and proxy entries beside the same requests already listed
#: under `flows`, which reads as two things having happened.
#:
#: Crash reports are in deliberately. A crash during an action is the single
#: most useful thing a trace can show, and it is genuinely something the app
#: did rather than something quern did to it.
APP_LOG_SOURCES = frozenset({
    LogSource.SYSLOG,
    LogSource.OSLOG,
    LogSource.SIMULATOR,
    LogSource.DEVICE,
    LogSource.LOGCAT,
    LogSource.APP_DRAIN,
    LogSource.CRASH,
})


@dataclass
class Attribution:
    """One action and everything that happened inside it."""

    action: LogEntry
    flows: list[FlowRecord] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    #: Other actions whose interval overlaps this one on the same device.
    #: Non-empty means the flows below may belong to any of them.
    overlaps: list[str] = field(default_factory=list)
    #: Why attribution is weaker than it looks, in words a reader can act on.
    caveats: list[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return bool(self.overlaps)


def _interval(action: LogEntry) -> tuple[datetime, datetime]:
    """When an action ran.

    `timestamp` is when the entry was *written*, which is when the action
    finished -- so the interval runs backwards from it by the duration. Taking
    it as the start would attribute the next action's traffic to this one.
    """
    end = action.timestamp
    start = end - timedelta(milliseconds=action.duration_ms or 0)
    return start, end


def ip_to_udid(cert_state: dict, *, now: datetime | None = None) -> dict[str, tuple[str, bool]]:
    """`client_ip` -> (udid, still_trusted), from recorded Wi-Fi proxy config.

    Physical devices have no pid for the addon to walk, so their traffic is
    identified by the address it came from. `record_device_proxy_config` has
    been writing that per SSID all along; this is the reverse view.

    The flag is the honest part. The mapping is recorded once and never
    revisited, so an address reassigned by DHCP since then points at the wrong
    device. Callers surface it rather than dropping the attribution, because
    "probably this device, recorded three weeks ago" is more useful than
    silence -- as long as it says so.
    """
    now = now or datetime.now(tz=_tz_of(cert_state))
    mapping: dict[str, tuple[str, bool]] = {}
    recorded_at: dict[str, datetime | None] = {}
    for udid, record in (cert_state or {}).items():
        for config in (record.get("wifi_proxy_configs") or {}).values():
            ip = config.get("client_ip")
            if not ip:
                continue
            fresh = True
            set_at = config.get("set_at")
            if set_at:
                try:
                    recorded = datetime.fromisoformat(set_at)
                except ValueError:
                    recorded = None
                if recorded is not None:
                    fresh = (now - recorded) <= IP_MAPPING_TRUSTED_FOR
            # Two devices can hold the same address over time -- DHCP
            # reuses them. Keeping whichever the dict happened to reach last
            # would attribute a flow to an arbitrary one of them, so the most
            # recently recorded wins.
            previous = recorded_at.get(ip)
            if previous is not None and recorded is not None and recorded < previous:
                continue
            if previous is not None and recorded is None:
                continue
            recorded_at[ip] = recorded
            mapping[ip] = (udid, fresh)
    return mapping


def _tz_of(_: dict):
    from datetime import UTC

    return UTC


def device_of(flow: FlowRecord, ip_map: dict[str, tuple[str, bool]]) -> tuple[str | None, bool]:
    """Which device a flow came from, and whether that is firmly known.

    `simulator_udid` is resolved from the client's pid and is exact. Falling
    back to `client_ip` is how physical devices are identified at all, and it
    carries whatever staleness the recorded mapping has.
    """
    if flow.simulator_udid:
        return flow.simulator_udid, True
    if flow.client_ip and flow.client_ip in ip_map:
        udid, fresh = ip_map[flow.client_ip]
        return udid, fresh
    return None, False


def build_trace(
    actions: list[LogEntry],
    flows: list[FlowRecord],
    device_logs: list[LogEntry],
    *,
    ip_map: dict[str, tuple[str, bool]] | None = None,
    grace: timedelta = CAUSAL_GRACE,
) -> list[Attribution]:
    """Attribute each flow and log line to the action whose interval holds it.

    Attribution requires the device to match as well as the time. A flow from
    another simulator that happens to land mid-tap is not part of that tap,
    and time alone would say it was.
    """
    ip_map = ip_map or {}
    ordered = sorted(actions, key=lambda a: a.timestamp)
    result = [Attribution(action=a) for a in ordered]
    intervals = [_interval(a.action) for a in result]

    # Overlaps first: an attribution that is ambiguous should say so even if
    # nothing lands inside it.
    for i, attribution in enumerate(result):
        start, end = intervals[i]
        for j, other in enumerate(result):
            if i == j or other.action.udid != attribution.action.udid:
                continue
            o_start, o_end = intervals[j]
            if o_start < end and start < o_end:
                attribution.overlaps.append(other.action.action or "(unnamed)")
        if attribution.overlaps:
            attribution.caveats.append(
                "overlaps another action on this device; anything below may "
                "belong to either",
            )

    for flow in flows:
        udid, firm = device_of(flow, ip_map)

        # Which actions could own this flow: those it happened inside, and
        # those it arrived shortly after. Preferring the first means a flow
        # landing inside one action is not also blamed on the previous one
        # merely for being close to it.
        # Device first, then time. Choosing `during` over `after` before
        # checking the device dropped flows entirely: a flow landing inside
        # another device's action made `during` non-empty, so the grace
        # window was never consulted, and the device check then rejected the
        # only candidate. The flow belonged to an action on its own device
        # and was attributed to nothing.
        def _matches(i: int) -> bool:
            owner = result[i].action.udid
            return not (udid and owner and udid != owner)

        # Half-open, `(start, end]`, so an instant shared by two actions has
        # exactly one owner. With both ends inclusive, a flow landing where
        # one action ends and the next begins satisfied both -- and because
        # touching intervals are deliberately *not* treated as overlapping,
        # neither attribution carried an ambiguity caveat. It was silently
        # counted twice.
        #
        # The earlier action wins the boundary: it had been running up to
        # that instant, while the later one had not yet done anything.
        during = [
            i for i in range(len(result))
            if _matches(i) and intervals[i][0] < flow.timestamp <= intervals[i][1]
        ]
        after = [
            i for i in range(len(result))
            if _matches(i)
            and intervals[i][1] < flow.timestamp <= intervals[i][1] + grace
        ]
        candidates = during or after
        inferred = not during

        for i in candidates:
            attribution = result[i]
            if inferred:
                _note(
                    attribution,
                    "some flows arrived after the action returned and are "
                    "attributed by timing rather than observed causation",
                )
            # With no device on either side, time is all there is. Say so
            # rather than presenting it as a firm attribution.
            if not udid:
                _note(attribution, "some flows matched on time alone")
            elif not firm:
                _note(
                    attribution,
                    "device identified from a client_ip recorded over "
                    f"{IP_MAPPING_TRUSTED_FOR.days} days ago; it may have moved",
                )
            attribution.flows.append(flow)

    for entry in device_logs:
        if entry.source not in APP_LOG_SOURCES:
            continue
        for i, attribution in enumerate(result):
            start, end = intervals[i]
            if not (start <= entry.timestamp <= end):
                continue
            # Device logs carry the udid they came from in `device_id`, and
            # one server can be driving several devices for several callers at
            # once. Matching on time alone hands one caller another's log
            # lines -- which is worse than no trace, because it reads as
            # evidence about their own run.
            if (
                entry.device_id
                and attribution.action.udid
                and entry.device_id != attribution.action.udid
            ):
                continue
            # Clock mismatch, stated rather than silently compared. An action
            # interval is on the host clock; a device log line is stamped by
            # OSLog on the *device's* clock. A simulator shares the host's, so
            # there is nothing to reconcile. A physical device does not, and
            # quern applies no offset -- so an attribution near an interval
            # boundary may be on the wrong side of it.
            #
            # Measurable, just not measured here: alignment under a
            # millisecond has been observed over a held lockdown connection,
            # but only while it is held, so it is a live reading rather than a
            # constant a trace could cache.
            if entry.source in _DEVICE_CLOCK_SOURCES:
                _note(
                    attribution,
                    "device log times come from the device's own clock and "
                    "are compared against host-clock intervals with no offset",
                )
            attribution.logs.append(entry)

    return result


def _note(attribution: Attribution, text: str) -> None:
    if text not in attribution.caveats:
        attribution.caveats.append(text)
