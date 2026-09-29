"""An Android emulator's flows can be told apart, and from the host's own.

Flows from an emulator arrive carrying the *host's* address, because QEMU NATs
them: `client_ip` is `192.168.1.189` for every emulator on the machine and for
the machine itself. Measured, and it is why attribution by address cannot work
here however many configs are recorded — the address is not unique.

What is unique is the process. The connection QEMU opens to the proxy is a
socket this host owns, so the source port names the process that opened it, and
an emulator's serial *is* the console port it listens on. Measured end to end:
two emulators running at once, one host address, eleven of eleven flows
attributed to the right one of the two.

The redirector route is closed, also measured: with the QEMU binary in
`local_capture`, driving traffic produced zero flows, against a positive
control in the same run where `curl` was captured with its pid. QEMU's
user-mode networking does not surface to the socket layer the redirector hooks.

See #262.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "quern_addon", Path(__file__).resolve().parent.parent / "server/proxy/addon.py",
)


@pytest.fixture
def addon():
    """The addon module, loaded directly.

    It runs as a separate `mitmdump -s addon.py` process rather than being
    imported by the server, so there is no package path to import it by.
    """
    module = importlib.util.module_from_spec(_SPEC)
    try:
        _SPEC.loader.exec_module(module)
    except SystemExit:
        pass
    return module


class TestTheSerialComesFromTheConsolePort:
    """An emulator's serial is `emulator-<console port>`, and the process
    listens on that port — so the serial is recoverable from the process with
    no call to adb. Measured: the QEMU process for `Pixel_6_Dev` listens on
    5554 and 5555, and adb calls it `emulator-5554`."""

    def test_the_lower_even_port_in_range_is_the_serial(self, addon, monkeypatch):
        monkeypatch.setattr(addon.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": "n127.0.0.1:5554\nn127.0.0.1:5555\n", "returncode": 0},
        )())
        assert addon._emulator_serial_for_pid(999) == "emulator-5554"

    def test_a_second_emulator_gets_its_own_serial(self, addon, monkeypatch):
        monkeypatch.setattr(addon.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": "n127.0.0.1:5556\nn127.0.0.1:5557\n", "returncode": 0},
        )())
        assert addon._emulator_serial_for_pid(998) == "emulator-5556"

    def test_a_process_listening_outside_the_range_is_not_an_emulator(
        self, addon, monkeypatch,
    ):
        """The negative control. Without a bounded range any process with a
        listening socket would be read as a device — the proxy itself listens,
        and so does every other server on the machine."""
        monkeypatch.setattr(addon.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": "n*:9183\nn127.0.0.1:8080\n", "returncode": 0},
        )())
        assert addon._emulator_serial_for_pid(997) is None

    def test_nothing_listening_is_not_an_emulator(self, addon, monkeypatch):
        monkeypatch.setattr(addon.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": "", "returncode": 0},
        )())
        assert addon._emulator_serial_for_pid(996) is None


class TestTheSocketLookupPicksTheRightEndOfTheConnection:
    """Both ends of an emulator's connection are on this host, so `lsof`
    returns two processes for the port — the device's QEMU and the proxy
    itself. Taking the first is a coin toss, and it came up heads for one
    emulator and tails for the next, attributing six flows to the proxy's own
    pid."""

    #: Verbatim `lsof -Fpcn` output for one live emulator connection.
    LSOF = (
        "p40687\n"
        "cqemu-system-aarch64-headless\n"
        "f82\n"
        "n192.168.1.189:55569->192.168.1.189:9183\n"
        "p67490\n"
        "cPython\n"
        "f7\n"
        "n192.168.1.189:9183->192.168.1.189:55569\n"
    )

    def _writer(self, peer):
        def get_extra_info(_self, key):
            return peer if key == "peername" else None

        return type("W", (), {"get_extra_info": get_extra_info})()

    def test_the_opener_is_chosen_not_the_listener(self, addon, monkeypatch):
        monkeypatch.setattr(addon, "_is_local_address", lambda ip: True)
        monkeypatch.setattr(addon.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": self.LSOF, "returncode": 0},
        )())
        pid, name = addon._pid_from_local_socket(
            self._writer(("192.168.1.189", 55569)),
        )
        assert pid == 40687
        assert name == "qemu-system-aarch64-headless"

    def test_a_remote_client_is_not_looked_up_at_all(self, addon, monkeypatch):
        """A phone arrives from its own LAN address and is already
        attributable by it. The lookup costs ~40ms and must not run for every
        connection a physical device makes."""
        called = []
        monkeypatch.setattr(addon, "_is_local_address", lambda ip: False)
        monkeypatch.setattr(addon.subprocess, "run",
                            lambda *a, **k: called.append(a) or None)
        assert addon._pid_from_local_socket(
            self._writer(("192.168.1.244", 51000)),
        ) == (None, None)
        assert called == []

    def test_a_peername_without_a_port_is_declined(self, addon, monkeypatch):
        monkeypatch.setattr(addon, "_is_local_address", lambda ip: True)
        assert addon._pid_from_local_socket(self._writer(("1.2.3.4",))) == (None, None)


class TestAttributionSurvivesTheClientHangingUp:
    """A flow is serialised when its response completes; the process info used
    to be dropped the instant the socket closed. Measured on one emulator in
    one run: every `connectivitycheck.gstatic.com` flow was attributed and
    every `www.google.com` one was not, purely on which connections lingered.

    Pre-existing and not Android-specific — an iOS simulator's flows go
    through the same two hooks — but invisible until something depended on the
    lookup succeeding."""

    def test_info_survives_disconnection(self, addon):
        addon._client_process_info["c1"] = {"pid": 7, "process_name": "qemu"}
        addon.IOSDebugAddon.client_disconnected(
            object(), type("C", (), {"id": "c1"})(),
        )
        assert "c1" not in addon._client_process_info
        assert addon._lookup_process_info("c1") == {"pid": 7, "process_name": "qemu"}

    def test_a_live_client_is_still_found(self, addon):
        addon._client_process_info["c2"] = {"pid": 8, "process_name": "qemu"}
        assert addon._lookup_process_info("c2")["pid"] == 8

    def test_retired_entries_do_not_accumulate_without_bound(self, addon):
        """A long-running proxy would otherwise keep an entry for every
        connection it has ever served."""
        addon._recent_process_info.clear()
        for i in range(addon._RECENT_PROCESS_INFO_MAX + 50):
            addon._client_process_info[f"x{i}"] = {"pid": i, "process_name": "p"}
            addon.IOSDebugAddon.client_disconnected(
                object(), type("C", (), {"id": f"x{i}"})(),
            )
        assert len(addon._recent_process_info) == addon._RECENT_PROCESS_INFO_MAX
        # The oldest went first, so the newest are the ones still answerable.
        assert addon._lookup_process_info("x0") is None
        assert addon._lookup_process_info(
            f"x{addon._RECENT_PROCESS_INFO_MAX + 49}") is not None

    def test_an_unknown_client_is_not_invented(self, addon):
        assert addon._lookup_process_info("never-seen") is None
        assert addon._lookup_process_info(None) is None


class TestTwoEmulatorsBehindOneAddressStayApart:
    """The case the serial exists for. Both emulators arrive as the host, so
    every `client_ip`-based narrowing sees one device where there are two."""

    @staticmethod
    def _flow(serial, fid, ts):
        from server.models import FlowRecord, FlowRequest

        return FlowRecord(
            id=fid, timestamp=ts, device_serial=serial,
            client_ip="192.168.1.189",
            request=FlowRequest(
                method="GET", url="http://x/y", host="x", path="/y",
            ),
        )

    async def test_a_query_narrows_to_one_emulator(self):
        from datetime import UTC, datetime

        from server.models import FlowQueryParams
        from server.proxy.flow_store import FlowStore

        store = FlowStore(max_size=10)
        now = datetime.now(UTC)
        for serial, fid in (("emulator-5554", "f1"), ("emulator-5556", "f2"),
                            ("emulator-5556", "f3")):
            await store.add(self._flow(serial, fid, now))

        flows, _ = await store.query(FlowQueryParams(device_serial="emulator-5556"))
        assert {f.id for f in flows} == {"f2", "f3"}

    async def test_client_ip_cannot_narrow_between_them(self):
        """Not a limitation being worked around -- the reason the serial is
        there. Filtering by the address both of them carry returns both, and
        would return the host's own traffic too."""
        from datetime import UTC, datetime

        from server.models import FlowQueryParams
        from server.proxy.flow_store import FlowStore

        store = FlowStore(max_size=10)
        now = datetime.now(UTC)
        await store.add(self._flow("emulator-5554", "f1", now))
        await store.add(self._flow("emulator-5556", "f2", now))

        flows, _ = await store.query(FlowQueryParams(client_ip="192.168.1.189"))
        assert len(flows) == 2

    async def test_one_emulators_eviction_does_not_truncate_the_others_answer(self):
        """The completeness half, and the subtler one. Eviction marks were
        keyed by `sim:` and `ip:` only, so both emulators shared the host's
        mark: shedding one device's traffic flagged the other's answer as
        truncated. A serial stands alone as a key now rather than being
        combined with the shared address."""
        from datetime import UTC, datetime, timedelta

        from server.proxy.flow_store import FlowStore

        store = FlowStore(max_size=2)
        base = datetime.now(UTC)
        await store.add(self._flow("emulator-5554", "old", base))
        await store.add(self._flow("emulator-5556", "b", base + timedelta(seconds=1)))
        # Pushes the 5554 flow out.
        await store.add(self._flow("emulator-5556", "c", base + timedelta(seconds=2)))

        since = base + timedelta(seconds=1)
        assert store.is_complete_since(since, device_serial="emulator-5556")
        assert not store.is_complete_since(base, device_serial="emulator-5554")


class TestAnAndroidRejectionIsRecognisedAtAll:
    """Android says `certificate unknown` where the docs long assumed
    `unknown ca`.

    Measured 2026-09-28: 113 rejections across two emulators, every one
    `certificate unknown`, with the CA simply not installed — installing it
    turned those same endpoints into captured flows, which is what proves they
    were trust failures rather than pinning. Two endpoints kept refusing
    afterwards and said `certificate unknown` too, so the alert does not
    separate the two causes on Android.

    Detection was never wrong — `_CERT_REJECTION_ALERTS` carries both — but
    nothing pinned it, and the surrounding comments asserted a distinction the
    measurement contradicts. Removing `certificate unknown` as a tidy-up would
    make every Android rejection invisible.
    """

    def test_the_android_alert_counts_as_a_certificate_rejection(self, addon):
        assert "certificate unknown" in addon._CERT_REJECTION_ALERTS

    def test_the_ios_style_alert_still_counts(self, addon):
        assert "unknown ca" in addon._CERT_REJECTION_ALERTS

    def test_an_unrelated_handshake_failure_does_not(self, addon):
        """The negative control. `tls_failed_client` fires for every
        client-side failure — a suspended app, a cancelled request, a flaky
        network — and reporting those as refusals buries the real signal."""
        for alert in ("close notify", "handshake failure", "internal error"):
            assert alert not in addon._CERT_REJECTION_ALERTS


class TestTheConfidenceReportedMatchesTheAttribution:
    """`identified_by` says it mirrors `device_of`, and briefly did not.

    `device_of` learned to read `device_serial` and this did not, so an
    emulator's flow was attributed to the right device and then reported as
    `unidentified` — telling the caller to weight as guesswork something that
    was resolved from the client's pid. Two functions that must agree, in two
    places, with only a docstring saying so."""

    @staticmethod
    def _flow(**kw):
        from datetime import UTC, datetime

        from server.models import FlowRecord, FlowRequest

        return FlowRecord(
            id="f", timestamp=datetime.now(UTC),
            request=FlowRequest(method="GET", url="http://x/", host="x", path="/"),
            **kw,
        )

    def test_an_emulator_flow_is_reported_as_exact(self):
        from server.trace import IdentifiedBy, identified_by

        flow = self._flow(device_serial="emulator-5554", client_ip="192.168.1.189")
        assert identified_by(flow, {}) is IdentifiedBy.PROCESS

    def test_a_simulator_flow_still_is(self):
        from server.trace import IdentifiedBy, identified_by

        assert identified_by(self._flow(simulator_udid="ABC"), {}) is IdentifiedBy.PROCESS

    def test_the_two_functions_agree_on_every_shape(self):
        """The mirror stated as a test rather than a comment. A flow that
        `device_of` can place must not be reported as unidentified, and one it
        cannot must not be reported as exact."""
        from server.trace import IdentifiedBy, device_of, identified_by

        ip_map = {"10.0.0.5": ("phone-udid", True)}
        shapes = [
            self._flow(device_serial="emulator-5554", client_ip="192.168.1.189"),
            self._flow(simulator_udid="ABC"),
            self._flow(client_ip="10.0.0.5"),
            self._flow(client_ip="192.168.1.189"),
            self._flow(),
        ]
        for flow in shapes:
            udid, _ = device_of(flow, ip_map)
            reported = identified_by(flow, ip_map)
            assert (udid is not None) == (reported is not IdentifiedBy.UNIDENTIFIED), (
                f"{flow.device_serial=} {flow.simulator_udid=} {flow.client_ip=}: "
                f"device_of says {udid!r}, identified_by says {reported}"
            )
