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

from server.models import FlowRecord, FlowRequest, LogEntry, LogLevel, LogSource
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
