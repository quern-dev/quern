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
| iOS simulator + local capture | `simulator_udid`, resolved from the pid | yes, `source_process` |
| physical device + Wi-Fi proxy | `client_ip`, via recorded proxy config | no |
| iOS simulator + Wi-Fi proxy | nothing; interval only | no |
| **Android emulator, any route** | **nothing; interval only** | no |

The third row is why `set_local_capture` is worth recommending to anyone who
wants a trace.

The fourth is a gap rather than a limit. There are three kinds of device
here -- iOS simulators, physical devices and Android emulators -- and the
flow side identifies two. An emulator is not a `launchd_sim` child, so the
pid walk finds nothing, and it is not in `wifi_proxy_configs` either, so its
traffic falls to the weakest regime with nothing saying why. The equivalent
walk exists in principle (an emulator process carries its console port, which
is its serial), it has simply never been written. Logs are fine: `LOGCAT` is
in `APP_LOG_SOURCES` and the adapter names its device.

Android is the one that gets forgotten because the other two are what anyone
tests against, which is how it was found here -- by being asked about, not by
failing.

**Ambiguity is marked, never guessed.** Two actions running against one device
with no app to tell them apart produce overlapping intervals, and a flow
inside both genuinely cannot be attributed. Picking one would make the trace
confidently wrong, which is the failure this codebase keeps finding. See
docs/proposals/logging-spec.md.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

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



class Ownership(enum.Enum):
    """Whether an action could own work seen on a given device.

    One concept, in one place, deliberately. Today an action's scope is the
    device it resolved. When sessions land (#254) it becomes the session that
    reserved one or more devices -- and the attribution rules below do not
    change, only what `owns` compares. Writing this as three inline udid
    checks was how the device-less case went wrong in the first place.
    """

    #: Both sides name the same device. The firm case.
    OWNS = "owns"
    #: The flow's device is unknown, so only time connects them.
    UNKNOWN_WORK = "unknown_work"
    #: The *action* resolved no device. It may still have caused this, but it
    #: cannot claim work that belongs to a device it never named.
    UNSCOPED_ACTION = "unscoped_action"
    #: Both known and different. Never the same work.
    FOREIGN = "foreign"


def owns(action_udid: str, work_udid: str | None) -> Ownership:
    """Could an action scoped to `action_udid` own work seen on `work_udid`?"""
    if action_udid and work_udid:
        return Ownership.OWNS if action_udid == work_udid else Ownership.FOREIGN
    if not work_udid:
        return Ownership.UNKNOWN_WORK
    return Ownership.UNSCOPED_ACTION


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
    now = now or datetime.now(UTC)
    mapping: dict[str, tuple[str, bool]] = {}
    recorded_at: dict[str, datetime | None] = {}
    for udid, record in (cert_state or {}).items():
        for config in (record.get("wifi_proxy_configs") or {}).values():
            ip = config.get("client_ip")
            if not ip:
                continue
            # Bound every iteration. It was only assigned inside the
            # `if set_at:` branch, so a config with a `client_ip` and no
            # `set_at` -- which the schema permits -- raised UnboundLocalError.
            # `_ip_map` swallows that with a bare except and returns {}, so a
            # single malformed record silently disabled *all* physical-device
            # attribution with no signal at all.
            #
            # Worse, it leaked: an undated entry kept the previous loop's
            # timestamp, so it could win the tie-break below on a date that
            # belonged to another device and be reported as firmly fresh.
            recorded: datetime | None = None
            set_at = config.get("set_at")
            if set_at:
                try:
                    recorded = datetime.fromisoformat(set_at)
                except ValueError:
                    recorded = None
                # Naive is possible and the consequence is severe. Our writer
                # stamps UTC-aware, but `set_at` is typed `str | None` and the
                # file is plain JSON on disk -- hand-edited, restored from a
                # backup, or written by an older build. A naive value here
                # makes `now - recorded` raise TypeError, which `_ip_map`
                # swallows with a bare except and returns `{}`: one bad record
                # would silently disable *every* physical device's
                # attribution, and an empty map is indistinguishable from
                # having no devices on Wi-Fi. Assume UTC, which is what the
                # writer records.
                if recorded is not None and recorded.tzinfo is None:
                    recorded = recorded.replace(tzinfo=UTC)
            # Unknown age is not freshness. Claiming it would be the exact
            # thing the caveat exists to prevent -- a stale mapping presented
            # as trustworthy attributes another device's traffic to this one.
            fresh = recorded is not None and (now - recorded) <= IP_MAPPING_TRUSTED_FOR
            # Two devices can hold the same address over time -- DHCP
            # reuses them. Keeping whichever the dict happened to reach last
            # would attribute a flow to an arbitrary one of them, so the most
            # recently recorded wins.
            # A dated record beats an undated one: knowing something beats
            # knowing nothing. Between two dated records the later wins.
            # Between two undated ones there is no basis to choose, so the
            # last read stands.
            if ip in recorded_at:
                previous = recorded_at[ip]
                if recorded is None and previous is not None:
                    continue
                if (
                    recorded is not None
                    and previous is not None
                    and recorded < previous
                ):
                    continue
            recorded_at[ip] = recorded
            mapping[ip] = (udid, fresh)
    return mapping



class IdentifiedBy(enum.StrEnum):
    """How a flow's or a log line's device was established.

    Said positively, on every item, because the alternative is inference from
    an absent field -- and `source_process: None` already means two different
    things ("not a simulator" and "could not be resolved"). A reader deciding
    how far to trust an attribution should not have to know which.

    The three flow regimes are not equally good and the gap is large: PROCESS
    is the client process resolved from the pid, which is exact; CLIENT_IP is
    an address recorded once at proxy setup, which DHCP can reassign
    underneath; UNIDENTIFIED means only time connects the flow to the action.
    """

    #: Resolved from the client's pid. Exact.
    PROCESS = "process"
    #: From a `client_ip` recorded at proxy setup, still inside the trust
    #: window. Right unless the address has been reassigned since.
    CLIENT_IP = "client_ip"
    #: The same, but recorded longer ago than we are willing to vouch for.
    #: Carried on the attribution as a caveat too.
    CLIENT_IP_EXPIRED = "client_ip_expired"
    #: The log adapter named the device when it captured the line.
    ADAPTER = "adapter"
    #: No device could be established. Time alone connects this to the action.
    UNIDENTIFIED = "unidentified"


def identified_by(
    flow: FlowRecord, ip_map: dict[str, tuple[str, bool]],
) -> IdentifiedBy:
    """Which regime identified this flow's device. Mirrors `device_of`."""
    if flow.simulator_udid:
        return IdentifiedBy.PROCESS
    if flow.client_ip and flow.client_ip in ip_map:
        _, fresh = ip_map[flow.client_ip]
        return IdentifiedBy.CLIENT_IP if fresh else IdentifiedBy.CLIENT_IP_EXPIRED
    return IdentifiedBy.UNIDENTIFIED


def log_identified_by(entry: LogEntry) -> IdentifiedBy:
    """The same for a log line, which has only the one source."""
    return IdentifiedBy.ADAPTER if entry.device_id else IdentifiedBy.UNIDENTIFIED


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

    _attribute(
        flows, result, intervals, grace,
        device_of_item=lambda f: device_of(f, ip_map),
        sink=lambda a, f: a.flows.append(f),
        noun="flows",
        extra_caveat=_flow_caveat,
    )
    _attribute(
        [e for e in device_logs if e.source in APP_LOG_SOURCES],
        result, intervals, grace,
        # A device log line names its device outright and does not need
        # resolving, so it is always firmly known.
        device_of_item=lambda e: (e.device_id or None, True),
        sink=lambda a, e: a.logs.append(e),
        noun="log lines",
        extra_caveat=_log_caveat,
    )

    return result


def _flow_caveat(attribution: Attribution, flow: FlowRecord, firm: bool) -> None:
    if not firm:
        _note(
            attribution,
            "device identified from a client_ip recorded over "
            f"{IP_MAPPING_TRUSTED_FOR.days} days ago; it may have moved",
        )


def _log_caveat(attribution: Attribution, entry: LogEntry, firm: bool) -> None:
    # An action interval is on the host clock; a device log line is stamped by
    # OSLog on the *device's* clock. A simulator shares the host's, so there
    # is nothing to reconcile. A physical device does not, and quern applies
    # no offset -- so an attribution near an interval boundary may be on the
    # wrong side of it.
    if entry.source in _DEVICE_CLOCK_SOURCES:
        _note(
            attribution,
            "device log times come from the device's own clock and are "
            "compared against host-clock intervals with no offset",
        )


def _attribute(
    items: list,
    result: list[Attribution],
    intervals: list[tuple[datetime, datetime]],
    grace: timedelta,
    *,
    device_of_item: Callable[[Any], tuple[str | None, bool]],
    sink: Callable[[Attribution, Any], None],
    noun: str,
    extra_caveat: Callable[[Attribution, Any, bool], None],
) -> None:
    """Attach each item to the action that owns it.

    One function for flows and log lines, deliberately. They were two loops
    with the same rules, and the rules drifted three separate times: half-open
    intervals, the grace window and the ownership check were each fixed for
    flows and left wrong for logs, every time under a comment that read as
    though it covered both. Parameterising the two differences -- how an item
    names its device, and what caveat it carries -- is what stops the next fix
    landing on half the problem.
    """
    for item in items:
        work_udid, firm = device_of_item(item)

        claims: dict[Ownership, list[int]] = {k: [] for k in Ownership}
        windows: dict[int, str] = {}
        for i in range(len(result)):
            verdict = owns(result[i].action.udid, work_udid)
            if verdict is Ownership.FOREIGN:
                continue
            start, end = intervals[i]
            at = item.timestamp
            # Half-open, `(start, end]`, so an instant shared by two adjacent
            # actions has exactly one owner -- the earlier, which had been
            # running up to it while the later had not yet done anything.
            if start < at <= end:
                window = "during"
            elif end < at <= end + grace:
                window = "after"
            else:
                continue
            windows[i] = window
            claims[verdict].append(i)

        scoped = claims[Ownership.OWNS] or claims[Ownership.UNKNOWN_WORK]
        candidates = scoped or claims[Ownership.UNSCOPED_ACTION]
        unscoped_fallback = not scoped and bool(candidates)

        during = [i for i in candidates if windows[i] == "during"]
        chosen = during or candidates
        inferred = not during

        if len(chosen) > 1:
            for i in chosen:
                _note(
                    result[i],
                    f"some {noun} here are also attributed to another action; "
                    "the trace cannot tell which caused them",
                )

        for i in chosen:
            attribution = result[i]
            if inferred:
                _note(
                    attribution,
                    f"some {noun} arrived after the action returned and are "
                    "attributed by timing rather than observed causation",
                )
            if unscoped_fallback:
                _note(
                    attribution,
                    "this action resolved no device, so work from "
                    f"{work_udid[:8] if work_udid else 'an unknown device'} "
                    "is attributed to it on timing alone",
                )
            elif not work_udid:
                _note(attribution, f"some {noun} matched on time alone")
            # Not `elif`. The per-kind caveat is a different fact from how
            # firmly the item was attributed, and chaining them meant a
            # physical-device log line attributed to an unscoped action said
            # the attribution was weak but not that the two timestamps come
            # from different clocks -- the one case where that matters most.
            # The consolidation lost this; it is the only rule from the old
            # log loop that did not survive.
            extra_caveat(attribution, item, firm)
            sink(attribution, item)


def _note(attribution: Attribution, text: str) -> None:
    if text not in attribution.caveats:
        attribution.caveats.append(text)
