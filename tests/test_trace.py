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

import uuid
from datetime import UTC, datetime, timedelta

from server.models import (
    FlowRecord,
    FlowRequest,
    LogEntry,
    LogLevel,
    LogSource,
)
from server.trace import IP_MAPPING_TRUSTED_FOR, build_trace, device_of, ip_to_udid

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
