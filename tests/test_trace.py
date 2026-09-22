"""Attributing flows and logs to the action that caused them.

The join has no correlation id to lean on -- the proxy is a separate process
and the requests are the app's -- so it works on what the flows already carry:
a `simulator_udid` resolved from the client's pid, or a `client_ip` matched
against recorded proxy config. See server/trace.py.

The tests that matter most are the ones about *not* attributing: a trace that
confidently assigns a flow to the wrong action is worse than one that says it
cannot tell.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from server.models import (
    FlowRecord,
    FlowRequest,
    LogEntry,
    LogLevel,
    LogSource,
)
from server.trace import (
    IP_MAPPING_TRUSTED_FOR,
    IdentifiedBy,
    build_trace,
    device_of,
    identified_by,
    ip_to_udid,
    log_identified_by,
)

BASE = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _action(name, *, at_s, duration_ms, udid="SIM-A"):
    return LogEntry(
        id=uuid.uuid4().hex,
        timestamp=BASE + timedelta(seconds=at_s),
        device_id="server",
        process="server.api.actions",
        category="device.action",
        level=LogLevel.INFO,
        message=f"{name} ok",
        source=LogSource.SERVER,
        action=name,
        udid=udid,
        duration_ms=duration_ms,
        outcome="ok",
    )


def _log(*, at_s, udid="SIM-A", process="MyApp"):
    return LogEntry(
        id=uuid.uuid4().hex,
        timestamp=BASE + timedelta(seconds=at_s),
        device_id=udid,
        process=process,
        level=LogLevel.INFO,
        message="x",
        source=LogSource.SIMULATOR,
    )


def _flow(*, at_s, udid=None, ip=None, process=None, host="example.com"):
    return FlowRecord(
        id=uuid.uuid4().hex,
        timestamp=BASE + timedelta(seconds=at_s),
        request=FlowRequest(
            method="GET", url=f"https://{host}/x", host=host, path="/x",
        ),
        simulator_udid=udid,
        client_ip=ip,
        source_process=process,
    )


class TestAFlowLandsOnTheActionThatCausedIt:
    def test_a_flow_inside_the_interval_is_attributed(self):
        # tap finished at t=10 having taken 2s, so it ran from t=8 to t=10.
        action = _action("tap", at_s=10, duration_ms=2000)
        flow = _flow(at_s=9, udid="SIM-A")

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == [flow]
        assert not attribution.ambiguous

    def test_a_flow_before_the_action_started_is_not(self):
        """The interval runs backwards from the entry's timestamp, because an
        action entry is written when the action *finishes*. Treating the
        timestamp as the start would attribute the next action's traffic to
        this one."""
        action = _action("tap", at_s=10, duration_ms=2000)
        flow = _flow(at_s=5, udid="SIM-A")

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == []

    def test_a_flow_from_another_device_is_not_attributed(self):
        """Time alone would say it was. Two simulators running at once is an
        ordinary thing to be doing."""
        action = _action("tap", at_s=10, duration_ms=2000, udid="SIM-A")
        flow = _flow(at_s=9, udid="SIM-B")

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == []


class TestAmbiguityIsMarkedNotGuessed:
    def test_overlapping_actions_on_one_device_are_marked(self):
        first = _action("tap", at_s=10, duration_ms=4000)      # 6 -> 10
        second = _action("swipe", at_s=11, duration_ms=4000)   # 7 -> 11

        first_a, second_a = build_trace([first, second], [], [])

        assert first_a.ambiguous and second_a.ambiguous
        assert "swipe" in first_a.overlaps
        assert "tap" in second_a.overlaps

    def test_a_flow_inside_both_is_reported_under_both(self):
        """It genuinely could belong to either. Putting it under one would be
        a guess the reader cannot see."""
        first = _action("tap", at_s=10, duration_ms=4000)
        second = _action("swipe", at_s=11, duration_ms=4000)
        flow = _flow(at_s=9, udid="SIM-A")

        first_a, second_a = build_trace([first, second], [flow], [])

        assert first_a.flows == [flow]
        assert second_a.flows == [flow]
        assert first_a.caveats and second_a.caveats

    def test_actions_on_different_devices_do_not_overlap(self):
        first = _action("tap", at_s=10, duration_ms=4000, udid="SIM-A")
        second = _action("tap", at_s=11, duration_ms=4000, udid="SIM-B")

        first_a, second_a = build_trace([first, second], [], [])

        assert not first_a.ambiguous
        assert not second_a.ambiguous

    def test_actions_that_merely_touch_are_not_overlapping(self):
        """One ending exactly as the next begins is a sequence, not a race."""
        first = _action("tap", at_s=10, duration_ms=2000)     # 8  -> 10
        second = _action("swipe", at_s=12, duration_ms=2000)  # 10 -> 12

        first_a, second_a = build_trace([first, second], [], [])

        assert not first_a.ambiguous
        assert not second_a.ambiguous


class TestPhysicalDevicesJoinOnTheAddress:
    def test_a_recorded_client_ip_identifies_the_device(self):
        state = {
            "PHONE-1": {
                "wifi_proxy_configs": {
                    "home": {
                        "client_ip": "192.168.1.50",
                        "set_at": (BASE - timedelta(days=1)).isoformat(),
                    },
                },
            },
        }
        ip_map = ip_to_udid(state, now=BASE)
        flow = _flow(at_s=9, ip="192.168.1.50")

        udid, firm = device_of(flow, ip_map)

        assert udid == "PHONE-1"
        assert firm is True

    def test_a_stale_mapping_still_attributes_but_says_so(self):
        """DHCP reassigns. A stale address does not fail -- it points at the
        wrong device, which is worse -- so the attribution carries a caveat
        rather than being silently dropped or silently trusted."""
        state = {
            "PHONE-1": {
                "wifi_proxy_configs": {
                    "home": {
                        "client_ip": "192.168.1.50",
                        "set_at": (
                            BASE - IP_MAPPING_TRUSTED_FOR - timedelta(days=1)
                        ).isoformat(),
                    },
                },
            },
        }
        ip_map = ip_to_udid(state, now=BASE)
        action = _action("tap", at_s=10, duration_ms=2000, udid="PHONE-1")
        flow = _flow(at_s=9, ip="192.168.1.50")

        [attribution] = build_trace([action], [flow], [], ip_map=ip_map)

        assert attribution.flows == [flow]
        assert any("may have moved" in c for c in attribution.caveats)

    def test_the_simulator_udid_wins_over_the_address(self):
        """The pid walk is exact; the address mapping is a record that may
        have rotted."""
        ip_map = {"192.168.1.50": ("PHONE-1", True)}
        flow = _flow(at_s=9, udid="SIM-A", ip="192.168.1.50")

        udid, firm = device_of(flow, ip_map)

        assert udid == "SIM-A"


class TestTheWeakRegimeAdmitsItIsWeak:
    def test_a_flow_with_no_device_is_attributed_on_time_with_a_caveat(self):
        """A simulator behind a plain Wi-Fi proxy arrives from the host, so
        `client_ip` is loopback and identifies nothing. The interval is all
        there is, and the trace should say that rather than implying the
        attribution is as firm as the local-capture case."""
        action = _action("tap", at_s=10, duration_ms=2000)
        flow = _flow(at_s=9)  # no udid, no usable ip

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == [flow]
        assert any("time alone" in c for c in attribution.caveats)


class TestDeviceLogsJoinOnTheIntervalToo:
    def test_a_log_line_inside_the_action_is_attributed(self):
        action = _action("tap", at_s=10, duration_ms=2000)
        line = LogEntry(
            id=uuid.uuid4().hex,
            timestamp=BASE + timedelta(seconds=9),
            device_id="SIM-A",
            process="MyApp",
            level=LogLevel.INFO,
            message="tapped",
            source=LogSource.SIMULATOR,
        )

        [attribution] = build_trace([action], [], [line])

        assert attribution.logs == [line]


class TestTwoCallersOnOneServer:
    """One quern, two agents, a simulator each, at the same time.

    The only thing separating them is the device: quern has no notion of who
    is asking. So every join has to be device-aware, and a trace that mixes
    them is worse than no trace at all -- it reads as evidence about a run it
    has nothing to do with.
    """

    def test_neither_actions_nor_flows_cross_over(self):
        a = _action("tap", at_s=10, duration_ms=2000, udid="SIM-A")
        b = _action("tap", at_s=10, duration_ms=2000, udid="SIM-B")
        flow_a = _flow(at_s=9, udid="SIM-A", host="a.example")
        flow_b = _flow(at_s=9, udid="SIM-B", host="b.example")

        a_attr, b_attr = build_trace([a, b], [flow_a, flow_b], [])

        assert a_attr.flows == [flow_a]
        assert b_attr.flows == [flow_b]

    def test_simultaneous_actions_on_different_devices_are_not_ambiguous(self):
        """Concurrency across devices is the normal case here, not a race.
        Marking it ambiguous would make every multi-agent trace unreadable."""
        a = _action("tap", at_s=10, duration_ms=4000, udid="SIM-A")
        b = _action("swipe", at_s=10, duration_ms=4000, udid="SIM-B")

        a_attr, b_attr = build_trace([a, b], [], [])

        assert not a_attr.ambiguous
        assert not b_attr.ambiguous

    def test_device_logs_do_not_cross_over(self):
        """The bug this class was written to catch: log attribution matched on
        the interval and nothing else, so each caller collected the other's
        device log lines."""
        a = _action("tap", at_s=10, duration_ms=2000, udid="SIM-A")
        b = _action("tap", at_s=10, duration_ms=2000, udid="SIM-B")

        def line(device, text):
            return LogEntry(
                id=uuid.uuid4().hex,
                timestamp=BASE + timedelta(seconds=9),
                device_id=device,
                process="MyApp",
                level=LogLevel.INFO,
                message=text,
                source=LogSource.SIMULATOR,
            )

        log_a, log_b = line("SIM-A", "from A"), line("SIM-B", "from B")

        a_attr, b_attr = build_trace([a, b], [], [log_a, log_b])

        assert [e.message for e in a_attr.logs] == ["from A"]
        assert [e.message for e in b_attr.logs] == ["from B"]


class TestClocksAreDeclaredNotAssumed:
    """A device log line is stamped on the device's clock; an action interval
    is on the host's. For a simulator they are the same clock. For a physical
    device they are not, and quern applies no offset -- so the trace says so
    rather than comparing them silently.

    Raised by the media-engine work (PR #164), which aligns video keyframes
    against these intervals and needs to know which numbers are facts.
    """

    @staticmethod
    def _line(source, device="SIM-A"):
        return LogEntry(
            id=uuid.uuid4().hex,
            timestamp=BASE + timedelta(seconds=9),
            device_id=device,
            process="MyApp",
            level=LogLevel.INFO,
            message="hello",
            source=source,
        )

    def test_a_physical_device_log_carries_the_clock_caveat(self):
        action = _action("tap", at_s=10, duration_ms=2000, udid="PHONE-1")
        line = self._line(LogSource.DEVICE, device="PHONE-1")

        [attribution] = build_trace([action], [], [line])

        assert attribution.logs == [line]
        assert any("own clock" in c for c in attribution.caveats)

    def test_a_simulator_log_does_not(self):
        """A simulator runs on the host clock. Declaring skew that cannot
        exist would make the caveat noise, and a caveat nobody believes is
        worse than none."""
        action = _action("tap", at_s=10, duration_ms=2000, udid="SIM-A")
        line = self._line(LogSource.SIMULATOR)

        [attribution] = build_trace([action], [], [line])

        assert attribution.logs == [line]
        assert not any("own clock" in c for c in attribution.caveats)


class TestOnlyAppLogsAppearAsLogs:
    """The ring buffer is shared: syslog, oslog, crash, build and proxy
    entries all land in it. A trace showing build output inside a tap, or
    proxy entries beside the same requests already listed under `flows`, reads
    as two things having happened.
    """

    @staticmethod
    def _line(source):
        return LogEntry(
            id=uuid.uuid4().hex,
            timestamp=BASE + timedelta(seconds=9),
            device_id="SIM-A",
            process="x",
            level=LogLevel.INFO,
            message=source.value,
            source=source,
        )

    def test_build_output_is_not_an_app_log(self):
        action = _action("tap", at_s=10, duration_ms=2000)

        [attribution] = build_trace([action], [], [self._line(LogSource.BUILD)])

        assert attribution.logs == []

    def test_proxy_entries_are_not_repeated_as_logs(self):
        """They are already under `flows`, with more detail."""
        action = _action("tap", at_s=10, duration_ms=2000)

        [attribution] = build_trace([action], [], [self._line(LogSource.PROXY)])

        assert attribution.logs == []

    def test_a_crash_during_an_action_is_kept(self):
        """The single most useful thing a trace can show, and genuinely
        something the app did."""
        action = _action("tap", at_s=10, duration_ms=2000)

        [attribution] = build_trace([action], [], [self._line(LogSource.CRASH)])

        assert [e.source for e in attribution.logs] == [LogSource.CRASH]

    def test_ordinary_device_output_is_kept(self):
        action = _action("tap", at_s=10, duration_ms=2000)

        [attribution] = build_trace([action], [], [self._line(LogSource.SIMULATOR)])

        assert len(attribution.logs) == 1


class TestTrafficCausedAfterAnActionReturns:
    """Most actions hand work to the device and return before it happens.

    Measured: `open_url` finished in 143ms and the HTTP request it caused
    arrived 174ms later. Attributing only what happens *during* an action
    misses the traffic that action caused -- which is the question a trace
    exists to answer.
    """

    def test_a_flow_just_after_the_action_is_attributed(self):
        action = _action("open_url", at_s=10, duration_ms=150)   # 9.85 -> 10
        flow = _flow(at_s=11, udid="SIM-A")                      # 1s later

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == [flow]

    def test_it_is_marked_as_inferred_not_observed(self):
        """A reader must be able to tell causation we saw from causation we
        guessed at from timing."""
        action = _action("open_url", at_s=10, duration_ms=150)
        flow = _flow(at_s=11, udid="SIM-A")

        [attribution] = build_trace([action], [flow], [])

        assert any("after the action returned" in c for c in attribution.caveats)

    def test_a_flow_during_the_action_is_not_marked(self):
        """Observed is not inferred, and conflating them would make the
        caveat meaningless."""
        action = _action("tap", at_s=10, duration_ms=2000)       # 8 -> 10
        flow = _flow(at_s=9, udid="SIM-A")

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == [flow]
        assert not any("after the action returned" in c for c in attribution.caveats)

    def test_a_flow_beyond_the_grace_window_is_not_attributed(self):
        """The window is a trade. Without an upper bound every later request
        would be blamed on the last action that ran."""
        action = _action("open_url", at_s=10, duration_ms=150)
        flow = _flow(at_s=60, udid="SIM-A")

        [attribution] = build_trace([action], [flow], [])

        assert attribution.flows == []

    def test_a_flow_inside_one_action_is_not_also_blamed_on_the_previous(self):
        """Preferring the containing action over a preceding one's grace
        window: otherwise every flow is attributed twice."""
        first = _action("open_url", at_s=10, duration_ms=150)     # 9.85 -> 10
        second = _action("tap", at_s=12, duration_ms=2000)        # 10   -> 12
        flow = _flow(at_s=11, udid="SIM-A")   # inside `tap`, inside first's grace

        first_a, second_a = build_trace([first, second], [flow], [])

        assert first_a.flows == []
        assert second_a.flows == [flow]


class TestDeviceMatchingComesBeforeTimeWindowChoice:
    """Choosing `during` over `after` before checking the device dropped
    flows entirely.

    A flow landing inside *another* device's action made `during` non-empty,
    so the grace window was never consulted; the device check then rejected
    the only candidate and the flow was attributed to nothing — though it
    belonged to an action on its own device (CodeRabbit, #259).
    """

    def test_a_flow_reaches_its_own_device_past_another_devices_action(self):
        # B's action contains the flow in time, but the flow is A's.
        # A's action ended just before it, so the grace window is its home.
        a = _action("open_url", at_s=10, duration_ms=150, udid="SIM-A")
        b = _action("tap", at_s=13, duration_ms=4000, udid="SIM-B")
        flow = _flow(at_s=11, udid="SIM-A")

        a_attr, b_attr = build_trace([a, b], [flow], [])

        assert a_attr.flows == [flow], "the flow never reached its own device"
        assert b_attr.flows == []


class TestADuplicateAddressResolvesToTheLatestDevice:
    def test_the_most_recently_recorded_device_wins(self):
        """DHCP reuses addresses. Whichever the dict reached last is an
        arbitrary answer; the most recent recording is a defensible one."""
        state = {
            "PHONE-OLD": {"wifi_proxy_configs": {"home": {
                "client_ip": "192.168.1.50",
                "set_at": (BASE - timedelta(days=30)).isoformat(),
            }}},
            "PHONE-NEW": {"wifi_proxy_configs": {"home": {
                "client_ip": "192.168.1.50",
                "set_at": (BASE - timedelta(hours=1)).isoformat(),
            }}},
        }

        ip_map = ip_to_udid(state, now=BASE)

        assert ip_map["192.168.1.50"][0] == "PHONE-NEW"


class TestAFlowOnASharedBoundaryHasOneOwner:
    """A flow at the instant one action ends and the next begins satisfied
    both inclusive interval checks — and because touching intervals are
    deliberately not treated as overlapping, neither attribution carried an
    ambiguity caveat. It was silently counted twice (CodeRabbit, #259).
    """

    def test_the_earlier_action_owns_the_boundary(self):
        first = _action("tap", at_s=10, duration_ms=2000)      # (8, 10]
        second = _action("swipe", at_s=12, duration_ms=2000)   # (10, 12]
        flow = _flow(at_s=10, udid="SIM-A")                    # exactly at 10

        first_a, second_a = build_trace([first, second], [flow], [])

        assert first_a.flows == [flow], "the action that was running lost it"
        assert second_a.flows == []

    def test_it_is_not_double_counted(self):
        first = _action("tap", at_s=10, duration_ms=2000)
        second = _action("swipe", at_s=12, duration_ms=2000)
        flow = _flow(at_s=10, udid="SIM-A")

        attributions = build_trace([first, second], [flow], [])

        assert sum(len(a.flows) for a in attributions) == 1


class TestAnActionWithNoDeviceCannotClaimAnothersWork:
    """The headline promise of this module, and it was false.

    An action that resolved no device claimed anything inside its interval —
    including a flow firmly identified as another device's — with no caveat,
    because the overlap pass never pairs an action that has a device with one
    that does not. `wait_for_flow` blocks for ten seconds by default and names
    no device, so this was the likely case rather than a corner one.

    Found by an independent review, after two CodeRabbit rounds missed it.
    """

    @staticmethod
    def _unscoped(at_s=10, duration_ms=10_000):
        return _action("wait_for_flow", at_s=at_s, duration_ms=duration_ms, udid="")

    def test_a_device_owning_action_wins_over_an_unscoped_one(self):
        unscoped = self._unscoped()
        owner = _action("tap", at_s=10, duration_ms=2000, udid="SIM-B")
        flow = _flow(at_s=9, udid="SIM-B")

        attributions = build_trace([unscoped, owner], [flow], [])
        by_name = {a.action.action: a for a in attributions}

        assert by_name["tap"].flows == [flow]
        assert by_name["wait_for_flow"].flows == [], (
            "an action with no device took work belonging to one that had it"
        )

    def test_an_unscoped_action_is_a_fallback_and_says_so(self):
        """It may genuinely have caused the work — dropping it would lose
        data silently, which is the failure this file exists to avoid. So it
        is attributed, and the attribution admits what it rests on."""
        unscoped = self._unscoped()
        flow = _flow(at_s=9, udid="SIM-B")

        [attribution] = build_trace([unscoped], [flow], [])

        assert attribution.flows == [flow]
        assert any("resolved no device" in c for c in attribution.caveats)

    def test_it_never_takes_work_from_a_device_another_action_owns(self):
        """Two agents, one blocked in wait_for_flow. Without this, the
        blocked one collects the other's traffic for ten seconds."""
        unscoped = self._unscoped()
        a = _action("tap", at_s=10, duration_ms=2000, udid="SIM-A")
        b = _action("tap", at_s=10, duration_ms=2000, udid="SIM-B")

        flow = _flow(at_s=9, udid="SIM-A")

        attributions = build_trace([unscoped, a, b], [flow], [])
        # Keyed by udid, not by name: both taps are called "tap", so a
        # name-keyed dict silently keeps only the last and the assertions
        # below would be about SIM-B while claiming to be about SIM-A.
        by_udid = {x.action.udid: x for x in attributions}

        # Selected rather than indexed. The previous version read
        # `assert X == [] if attributions[0].action.udid == "" else True`,
        # which is a conditional *expression*: whenever the first attribution
        # was not the unscoped one, it evaluated to `assert True` and checked
        # nothing at all. The only surviving assertion was that the total was
        # one, which is equally true when the wrong action holds it -- so the
        # test could pass against the bug it names.
        assert by_udid[""].flows == [], (
            "the unscoped action took work belonging to SIM-A"
        )
        assert by_udid["SIM-A"].flows == [flow], "SIM-A's own action lost its flow"
        assert by_udid["SIM-B"].flows == [], "SIM-B claimed SIM-A's flow"
        assert sum(len(x.flows) for x in attributions) == 1


class TestAFlowInTwoGraceWindowsSaysSo:
    """Two sequential actions can both have a flow in their grace window
    while neither interval overlaps the other — so the overlap marking, which
    is about actions racing on one device, never fires. A consumer summing
    flows across actions would over-count with nothing to warn it."""

    def test_both_attributions_admit_the_other(self):
        first = _action("open_url", at_s=10, duration_ms=200)
        second = _action("open_url", at_s=11, duration_ms=200)
        flow = _flow(at_s=11.5, udid="SIM-A")

        first_a, second_a = build_trace([first, second], [flow], [])

        assert first_a.flows == [flow]
        assert second_a.flows == [flow]
        assert any("also attributed" in c for c in first_a.caveats)
        assert any("also attributed" in c for c in second_a.caveats)


class TestLateLogOutputIsAttributedToo:
    """The grace window applied to flows and not to logs.

    This module's central argument — that most actions return before the work
    they cause happens — is as true of an app's log output as of its HTTP
    requests. Without it the trace could show the request an action caused but
    not the NSLog beside it, which is the common debugging case. Measured: an
    `open_url` finishing in 150ms, with both a flow and a log line 300ms
    after, attributed the flow and dropped the line.
    """

    @staticmethod
    def _line(at_s, device="SIM-A"):
        return LogEntry(
            id=uuid.uuid4().hex,
            timestamp=BASE + timedelta(seconds=at_s),
            device_id=device,
            process="MyApp",
            level=LogLevel.INFO,
            message="late",
            source=LogSource.SIMULATOR,
        )

    def test_a_line_just_after_the_action_is_attributed(self):
        action = _action("open_url", at_s=10, duration_ms=150)
        line = self._line(11)

        [attribution] = build_trace([action], [], [line])

        assert attribution.logs == [line]

    def test_it_is_marked_as_inferred(self):
        action = _action("open_url", at_s=10, duration_ms=150)

        [attribution] = build_trace([action], [], [self._line(11)])

        assert any("after the action returned" in c for c in attribution.caveats)

    def test_a_line_during_the_action_is_not_marked(self):
        action = _action("tap", at_s=10, duration_ms=2000)

        [attribution] = build_trace([action], [], [self._line(9)])

        assert attribution.logs
        assert not any("after the action returned" in c for c in attribution.caveats)

    def test_a_line_beyond_the_window_is_not_attributed(self):
        action = _action("open_url", at_s=10, duration_ms=150)

        [attribution] = build_trace([action], [], [self._line(60)])

        assert attribution.logs == []

    def test_a_line_on_a_shared_boundary_has_one_owner(self):
        """The flow path was made half-open; this loop was still closed, so a
        line at the instant one action ended and the next began went to both
        with nothing saying so."""
        first = _action("tap", at_s=10, duration_ms=2000)
        second = _action("swipe", at_s=12, duration_ms=2000)

        first_a, second_a = build_trace([first, second], [], [self._line(10)])

        assert len(first_a.logs) + len(second_a.logs) == 1


class TestTheLogPathObeysTheSameRulesAsFlows:
    """Three fixes on this branch landed on flows and not on logs: half-open
    intervals, the grace window, and the ownership check. Each time the
    comment read as though it covered both loops.

    The loops are one function now, so these assert the shared rules through
    the log path specifically — if the two ever diverge again, this is what
    notices.
    """

    @staticmethod
    def _line(at_s, device="SIM-A"):
        return LogEntry(
            id=uuid.uuid4().hex,
            timestamp=BASE + timedelta(seconds=at_s),
            device_id=device,
            process="MyApp",
            level=LogLevel.INFO,
            message="x",
            source=LogSource.SIMULATOR,
        )

    def test_an_unscoped_action_does_not_take_another_devices_log(self):
        """The headline bug, in the loop it was not fixed in."""
        unscoped = _action("wait_for_flow", at_s=10, duration_ms=10_000, udid="")
        owner = _action("tap", at_s=10, duration_ms=2000, udid="SIM-B")
        line = self._line(9, device="SIM-B")

        attributions = build_trace([unscoped, owner], [], [line])
        by_name = {a.action.action: a for a in attributions}

        assert by_name["tap"].logs == [line]
        assert by_name["wait_for_flow"].logs == []

    def test_a_line_in_two_grace_windows_says_so(self):
        first = _action("open_url", at_s=10, duration_ms=200)
        second = _action("open_url", at_s=11, duration_ms=200)

        first_a, second_a = build_trace([first, second], [], [self._line(11.5)])

        assert first_a.logs and second_a.logs
        assert any("also attributed" in c for c in first_a.caveats)

    def test_a_foreign_device_line_is_never_attributed(self):
        action = _action("tap", at_s=10, duration_ms=2000, udid="SIM-A")

        [attribution] = build_trace([action], [], [self._line(9, device="SIM-B")])

        assert attribution.logs == []


class TestANaiveSetAtDoesNotDisableAttribution:
    """`set_at` is typed `str | None` and lives in plain JSON on disk. Our
    writer stamps it UTC-aware, but a hand-edited file, a restored backup or
    an older build can leave it naive -- and then `now - recorded` raises
    TypeError, which `_ip_map` swallows with a bare except and returns `{}`.

    One bad record therefore disabled *every* physical device's attribution,
    and an empty map is indistinguishable from having no devices on Wi-Fi.
    Same shape as the naive `?since`, and with a worse failure: silent rather
    than a 500."""

    def _state(self, set_at):
        return {
            "PHONE-1": {
                "wifi_proxy_configs": {
                    "home": {"client_ip": "192.168.1.50", "set_at": set_at},
                },
            },
        }

    def test_a_recent_naive_stamp_is_trusted(self):
        naive = (BASE - timedelta(days=1)).replace(tzinfo=None).isoformat()

        ip_map = ip_to_udid(self._state(naive), now=BASE)

        assert ip_map == {"192.168.1.50": ("PHONE-1", True)}

    def test_an_old_naive_stamp_is_mapped_but_not_trusted(self):
        old = (
            BASE - IP_MAPPING_TRUSTED_FOR - timedelta(days=1)
        ).replace(tzinfo=None).isoformat()

        ip_map = ip_to_udid(self._state(old), now=BASE)

        assert ip_map == {"192.168.1.50": ("PHONE-1", False)}

    def test_a_naive_stamp_is_read_as_utc_not_local(self, pinned_timezone):
        """The choice of UTC is load-bearing, not incidental.

        `astimezone(UTC)` on a naive value reads it as *local* time, which is
        a plausible-looking alternative and wrong: it shifts the stamp by the
        server's offset, so a mapping an hour inside the trust window reads as
        six hours outside it. The server's timezone then decides whether a
        physical device's flows are trusted.

        The timezone is pinned rather than inherited. The bug is invisible
        under `TZ=UTC`, and CI runs there -- a test that only fails on a
        developer's machine is the shape this file exists to avoid."""
        pinned_timezone("America/Los_Angeles")
        # One hour *past* the window. Read as UTC: stale. Read as local
        # (UTC-7 in summer) the stamp lands seven hours later in UTC, so it
        # comes back inside the window and reports itself trustworthy -- the
        # direction that matters, because it attributes another device's
        # traffic to this one on a mapping that has expired.
        edge = (
            BASE - IP_MAPPING_TRUSTED_FOR - timedelta(hours=1)
        ).replace(tzinfo=None).isoformat()

        ip_map = ip_to_udid(self._state(edge), now=BASE)

        assert ip_map == {"192.168.1.50": ("PHONE-1", False)}

    def test_one_bad_record_does_not_take_the_others_with_it(self):
        """The reason this matters. The map is built in one pass, so the
        exception did not cost one device its attribution -- it cost all of
        them."""
        state = self._state((BASE - timedelta(days=1)).replace(tzinfo=None).isoformat())
        state["PHONE-2"] = {
            "wifi_proxy_configs": {
                "home": {
                    "client_ip": "192.168.1.51",
                    "set_at": (BASE - timedelta(days=1)).isoformat(),
                },
            },
        }

        ip_map = ip_to_udid(state, now=BASE)

        assert ip_map["192.168.1.51"] == ("PHONE-2", True)


class TestTheTraceSaysHowItIdentifiedTheDevice:
    """Every flow and log line carries `identified_by`, on every item.

    Found live: a flow attributed to a physical iPhone purely from a recorded
    `client_ip` came back with `caveats: []`, and the only thing distinguishing
    it from an exact pid-resolved attribution was that `source_process` was
    absent -- which also just means "not a simulator". Two very different
    levels of confidence, told apart by a missing field.

    The regimes are not equally good and the gap is wide: a pid is exact,
    while an address recorded at proxy setup can be reassigned by DHCP to a
    different device entirely. A reader deciding whether to trust an
    attribution has to be able to see which one it got.
    """

    def _map(self, *, fresh=True, at=None):
        return ip_to_udid(
            {
                "PHONE-1": {
                    "wifi_proxy_configs": {
                        "home": {
                            "client_ip": "192.168.1.50",
                            "set_at": (
                                at or (
                                    BASE - timedelta(days=1) if fresh
                                    else BASE - IP_MAPPING_TRUSTED_FOR
                                    - timedelta(days=1)
                                )
                            ).isoformat(),
                        },
                    },
                },
            },
            now=BASE,
        )

    def test_a_pid_resolved_flow_says_process(self):
        assert identified_by(_flow(at_s=1, udid="SIM-A"), {}) is IdentifiedBy.PROCESS

    def test_a_fresh_ip_mapped_flow_says_client_ip(self):
        flow = _flow(at_s=1, ip="192.168.1.50")

        assert identified_by(flow, self._map()) is IdentifiedBy.CLIENT_IP

    def test_a_stale_ip_mapped_flow_says_so_distinctly(self):
        """Not folded in with the fresh case. The caveat already reports it,
        but a caveat sits on the action while this sits on the flow, and an
        action can hold one of each."""
        flow = _flow(at_s=1, ip="192.168.1.50")

        assert (
            identified_by(flow, self._map(fresh=False))
            is IdentifiedBy.CLIENT_IP_EXPIRED
        )

    def test_an_unknown_address_is_unidentified(self):
        flow = _flow(at_s=1, ip="10.9.9.9")

        assert identified_by(flow, self._map()) is IdentifiedBy.UNIDENTIFIED

    def test_a_flow_with_nothing_at_all_is_unidentified(self):
        assert identified_by(_flow(at_s=1), {}) is IdentifiedBy.UNIDENTIFIED

    def test_a_pid_beats_an_ip_that_also_matches(self):
        """`device_of` prefers the pid, so this must agree with it -- the
        field would otherwise describe a resolution that did not happen."""
        flow = _flow(at_s=1, udid="SIM-A", ip="192.168.1.50")

        assert identified_by(flow, self._map()) is IdentifiedBy.PROCESS

    def test_a_named_log_line_says_adapter(self):
        entry = _log(at_s=1, udid="SIM-A")

        assert log_identified_by(entry) is IdentifiedBy.ADAPTER

    def test_an_unnamed_log_line_is_unidentified(self):
        entry = _log(at_s=1, udid="")

        assert log_identified_by(entry) is IdentifiedBy.UNIDENTIFIED


#: The process timezone as it was before any test touched it. Captured at
#: import so the restore assertion below has something to compare against
#: that does not assume the host is in any particular zone -- the first draft
#: asserted "not JST" and failed legitimately under `TZ=Asia/Tokyo`, where the
#: correct restore *is* JST.
_ORIGINAL_TZNAME = time.tzname


class TestThePinnedTimezoneFixturePutsItBack:
    """The fixture exists because `monkeypatch.setenv("TZ", ...)` restores the
    variable while the process keeps the timezone -- `time.tzset()` is what
    re-reads it, and nothing was calling it on the way out. Reproduced: `TZ`
    back to `UTC` and `time.tzname` still `('JST', 'JST')`.

    These two run in order and the second is the assertion; delete the
    teardown and it fails.
    """

    def test_one_pins_a_timezone(self, pinned_timezone):
        pinned_timezone("Asia/Tokyo")

        assert time.tzname[0] == "JST"

    def test_two_does_not_inherit_it(self):
        assert time.tzname == _ORIGINAL_TZNAME, (
            f"the previous test's timezone leaked: {time.tzname} "
            f"instead of {_ORIGINAL_TZNAME}"
        )
