"""A physical device has two identifiers; both must reach the same device.

`devicectl`'s JSON calls the CoreDevice UUID `identifier` and the ECID-based
one `hardwareProperties.udid`. Its *printed* table shows only the latter,
under a column headed `(UDID)` -- and so do Xcode and the device's own About
screen. quern keys on the former. So a caller who read the udid off any of
those was passing a real identifier for a real connected device and getting
HTTP 500 back, naming simctl, about a phone.

Measured on a connected iPhone 11 before the fix:

    screenshot?udid=B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91  -> 200
    screenshot?udid=00008030-000C59623A69802E             -> 500
        "[simctl] simctl io failed: Invalid device"

The 500 is the lesser half. Attribution compares identifiers by equality, so
two spellings of one phone are FOREIGN to each other -- an action logged under
one and a proxy config recorded under the other never join, and the flow
vanishes from the trace looking exactly like a quiet device. See #270.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from server.device import devicectl as dc
from server.device.controller import DeviceController
from server.models import DeviceType

CD_UUID = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
HW_UDID = "00008030-000C59623A69802E"

_DEVICECTL_JSON = json.dumps({
    "result": {"devices": [{
        "identifier": CD_UUID,
        "deviceProperties": {"name": "iPhone 11", "osVersionNumber": "18.6"},
        "connectionProperties": {
            "transportType": "wired", "tunnelState": "connected",
            "pairingState": "paired",
        },
        "hardwareProperties": {
            "udid": HW_UDID, "deviceType": "iPhone", "reality": "physical",
        },
    }]},
})


def _action_on(udid: str):
    """A completed action entry, as the server log adapter would write it."""
    import uuid
    from datetime import UTC, datetime

    from server.models import LogEntry, LogLevel, LogSource

    return LogEntry(
        id=uuid.uuid4().hex, timestamp=datetime.now(UTC), device_id="server",
        process="server.api.actions", category="device.read",
        level=LogLevel.INFO, message="take_screenshot ok", source=LogSource.SERVER,
        action="take_screenshot", udid=udid, duration_ms=10, outcome="ok",
    )


def _isolated_controller() -> DeviceController:
    """A controller whose `list_devices` touches nothing real.

    It must still be *called*: `_ensure_device_type_cached` refreshes the list
    when it meets an unknown udid, and that refresh is what learns the second
    spelling. Stubbing the cache instead would skip the very step under test.

    Without this the tests shelled out to the real `simctl` and `devicectl` --
    ~1s each, and green on this machine because an iPhone 11 happened to be
    plugged into it. That is the house failure: a pass that came from the room
    rather than the code.
    """
    ctrl = DeviceController()

    async def _fake_list_devices():
        await _listed()                      # fills the alias map, as production does
        ctrl._device_type_cache[CD_UUID] = DeviceType.DEVICE
        return []

    ctrl.list_devices = _fake_list_devices
    return ctrl


async def _listed() -> None:
    """Populate the alias map the way production does -- via list_devices."""
    backend = dc.DevicectlBackend()
    with patch.object(
        backend, "_run_devicectl", AsyncMock(return_value=(_DEVICECTL_JSON, "")),
    ), patch("server.device.devicectl.xcode_available", return_value=True):
        await backend.list_devices()


class TestTheAliasMapIsBuiltFromTheDeviceList:
    async def test_the_hardware_udid_maps_to_the_canonical_one(self):
        await _listed()

        assert dc.canonical_device_id(HW_UDID) == CD_UUID

    async def test_the_canonical_one_maps_to_itself(self):
        """Idempotent, so callers can canonicalise without checking first."""
        await _listed()

        assert dc.canonical_device_id(CD_UUID) == CD_UUID

    async def test_an_unknown_udid_passes_through(self):
        """This canonicalises; it does not validate. A simulator UDID has one
        spelling and must come back untouched."""
        await _listed()

        assert dc.canonical_device_id("SIM-1234") == "SIM-1234"

    async def test_it_works_before_any_list_call(self):
        assert dc.canonical_device_id(HW_UDID) == HW_UDID


class TestResolutionAcceptsEitherSpelling:
    """The canonicalisation has to happen in `resolve_udid`, because that is
    the one place that decides which device a call targets -- and therefore
    the one place whose answer everything downstream stores, logs and
    compares."""

    def _controller(self):
        return _isolated_controller()

    async def test_the_hardware_udid_resolves_to_the_canonical_one(self):
        ctrl = self._controller()

        assert await ctrl.resolve_udid(HW_UDID) == CD_UUID

    async def test_the_active_device_is_stored_canonically(self):
        """Or the next unqualified call resolves to a spelling nothing else
        recognises, and the bug comes back one step removed."""
        ctrl = self._controller()

        await ctrl.resolve_udid(HW_UDID)

        assert ctrl._active_udid == CD_UUID

    async def test_the_action_log_records_the_canonical_one(self):
        """Two actions on one phone, named differently, must compare equal --
        that is the whole point for the trace and for #254."""
        from server.api.actions import ActionScope
        from server.logging_ext import reset_current_action, set_current_action

        ctrl = self._controller()
        scope = ActionScope("tap", "device.action")
        token = set_current_action(scope)
        try:
            await ctrl.resolve_udid(HW_UDID)
        finally:
            reset_current_action(token)

        assert scope.udid == CD_UUID

    async def test_set_active_false_still_canonicalises(self):
        """The read path must agree with the write path about identity."""
        ctrl = self._controller()
        ctrl._active_udid = "SOMETHING-ELSE"

        resolved = await ctrl.resolve_udid(HW_UDID, set_active=False)

        assert resolved == CD_UUID
        assert ctrl._active_udid == "SOMETHING-ELSE", "set_active=False still wrote"


class TestScreenshotGoesThroughResolution:
    """It used to bypass `resolve_udid` entirely when given a udid. That cost
    the action log its device once already; the bypass is what allowed it, so
    the bypass is what this pins."""

    async def test_it_routes_a_hardware_udid_to_the_physical_backend(self):
        ctrl = _isolated_controller()
        ctrl.pmd3.screenshot = AsyncMock(return_value=b"\x89PNGfake")
        ctrl.simctl.screenshot = AsyncMock(return_value=b"wrong")

        with patch("server.device.controller.process_screenshot") as proc:
            proc.return_value = (b"ok", "image/png")
            await ctrl.screenshot(udid=HW_UDID)

        ctrl.pmd3.screenshot.assert_awaited_once_with(CD_UUID)
        ctrl.simctl.screenshot.assert_not_called()

    async def test_it_does_not_change_the_active_device(self):
        """The reason the bypass existed. Removing it must not cost this."""
        ctrl = _isolated_controller()
        ctrl._active_udid = "SIM-KEEP-ME"
        ctrl.pmd3.screenshot = AsyncMock(return_value=b"\x89PNGfake")

        with patch("server.device.controller.process_screenshot") as proc:
            proc.return_value = (b"ok", "image/png")
            await ctrl.screenshot(udid=HW_UDID)

        assert ctrl._active_udid == "SIM-KEEP-ME"


class TestTheTraceAcceptsEitherSpelling:
    """The query side has to canonicalise too, or the fix is half done in the
    worst way: device calls succeed under both spellings while the trace
    returns nothing for one of them.

    Measured before this: two screenshots of one iPhone, both logged under
    `B34C4EE9-...`, and `?udid=00008030-...` returned zero actions. An empty
    trace is indistinguishable from a quiet device, which is the confusion the
    endpoint exists to prevent.
    """

    async def _trace(self, udid):
        from types import SimpleNamespace

        from server.api.trace import get_trace
        from server.storage.ring_buffer import RingBuffer

        await _listed()
        server_buffer = RingBuffer(max_size=100)
        await server_buffer.append(_action_on(CD_UUID))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            server_buffer=server_buffer, ring_buffer=RingBuffer(max_size=10),
            flow_store=None, proxy_adapter=None,
        )))
        return await get_trace(request=request, since=None, udid=udid, limit=10)

    async def test_the_hardware_udid_finds_the_action(self):
        result = await self._trace(HW_UDID)

        assert len(result["actions"]) == 1

    async def test_the_canonical_one_still_does(self):
        result = await self._trace(CD_UUID)

        assert len(result["actions"]) == 1

    async def test_another_device_still_finds_nothing(self):
        """Canonicalising must not turn the filter into a pass-through."""
        result = await self._trace("SOME-OTHER-DEVICE")

        assert result["actions"] == []


class TestEveryWriterOfTheActiveDeviceCanonicalises:
    """`resolve_udid` canonicalising what it *returns* is not enough.

    The active device is also written directly -- by `POST /device/active` and
    by four paths in `DevicePool` -- and those stored the raw udid. Branch 2 of
    `_resolve_udid` then handed it back uncanonicalised, so once
    `GET /trace?udid=` began canonicalising its query, **both** spellings
    matched nothing. That is worse than before the canonicalisation existed.

    So the property setter canonicalises, which is the one place all eleven
    writers land, and is also what the sidecar persists.
    """

    async def test_a_raw_hardware_udid_is_stored_canonically(self):
        await _listed()          # the map is filled by list_devices, not by construction
        ctrl = _isolated_controller()

        ctrl._active_udid = HW_UDID

        assert ctrl._active_udid == CD_UUID

    def test_the_canonical_spelling_is_unchanged(self):
        ctrl = _isolated_controller()

        ctrl._active_udid = CD_UUID

        assert ctrl._active_udid == CD_UUID

    def test_clearing_it_still_works(self):
        """`None` must not be canonicalised into something truthy."""
        ctrl = _isolated_controller()
        ctrl._active_udid = HW_UDID

        ctrl._active_udid = None

        assert ctrl._active_udid is None

    def test_an_unknown_udid_passes_through(self):
        ctrl = _isolated_controller()

        ctrl._active_udid = "SIM-1234"

        assert ctrl._active_udid == "SIM-1234"

    async def test_the_restored_active_path_returns_the_canonical(self):
        """Branch 2 of `_resolve_udid`. This is the path that carried the raw
        value out to the trace filter."""
        await _listed()
        ctrl = _isolated_controller()
        ctrl._active_udid = HW_UDID

        assert await ctrl.resolve_udid() == CD_UUID


class TestAnEmptyCanonicalIsRefused:
    """The guard was on the key and never the value, so a devicectl entry with
    no `identifier` -- read defensively as `""` -- produced `{HW: ""}`.

    `canonical_device_id` then returned `""` for a real device, and `""` is
    falsy, so the trace's `if udid:` filter became a pass-through: it answered
    with *other* devices' actions and echoed `udid: ""`.
    """

    def test_a_missing_identifier_records_nothing(self):
        dc._remember_identity("", HW_UDID)

        assert dc.canonical_device_id(HW_UDID) == HW_UDID

    def test_a_real_identifier_still_records(self):
        dc._remember_identity(CD_UUID, HW_UDID)

        assert dc.canonical_device_id(HW_UDID) == CD_UUID


class TestIdentityIsRecordedBeforeTheFilters:
    """Deliberate: a device that is unpaired, unreachable or simulated is still
    one someone can name, and knowing both its spellings is what lets a refusal
    say which device it means instead of blaming simctl for an "invalid
    device".

    Untested until a surviving mutant said so -- moving `_remember_identity`
    below both filters left all 168 tests green, because every fixture was
    paired, reachable and physical.
    """

    async def _listed_with(self, **overrides) -> None:
        import json as _json
        from unittest.mock import AsyncMock as _AsyncMock
        from unittest.mock import patch as _patch

        dev = {
            "identifier": CD_UUID,
            "deviceProperties": {"name": "iPhone 11", "osVersionNumber": "18.6"},
            "connectionProperties": {
                "transportType": "wired", "tunnelState": "connected",
                "pairingState": "paired",
            },
            "hardwareProperties": {
                "udid": HW_UDID, "deviceType": "iPhone", "reality": "physical",
            },
        }
        for path, value in overrides.items():
            section, key = path.split(".")
            dev[section][key] = value
        backend = dc.DevicectlBackend()
        with _patch.object(
            backend, "_run_devicectl",
            _AsyncMock(return_value=(_json.dumps({"result": {"devices": [dev]}}), "")),
        ), _patch("server.device.devicectl.xcode_available", return_value=True):
            await backend.list_devices()

    async def test_an_unpaired_device_is_still_nameable(self):
        await self._listed_with(**{"connectionProperties.pairingState": "unpaired"})

        assert dc.canonical_device_id(HW_UDID) == CD_UUID

    async def test_an_unreachable_device_is_still_nameable(self):
        await self._listed_with(**{"connectionProperties.tunnelState": "unavailable"})

        assert dc.canonical_device_id(HW_UDID) == CD_UUID

    async def test_a_simulated_entry_is_still_nameable(self):
        await self._listed_with(**{"hardwareProperties.reality": "simulated"})

        assert dc.canonical_device_id(HW_UDID) == CD_UUID


async def _record_proxy_config(udid: str) -> None:
    """Drive the real endpoint, without letting it ask the machine anything.

    It derives `proxy_host` from the host's own interfaces, so calling it
    unmocked reads whatever network this happens to be on -- a test touching
    the real machine, which is the defect #272 exists to end. Pinned to a
    fixed address instead; the value is irrelevant here, only the udid the
    config is filed under matters.
    """
    from types import SimpleNamespace
    from unittest.mock import patch as _patch

    from server.api.proxy_certs import (
        RecordDeviceProxyRequest,
        record_device_proxy_config_endpoint,
    )

    ctrl = _isolated_controller()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        device_controller=ctrl,
    )))
    with _patch(
        "server.lifecycle.state.detect_host_ip_for_subnet", return_value="10.0.0.2",
    ), _patch("server.lifecycle.state.detect_local_ip", return_value="10.0.0.2"):
        await record_device_proxy_config_endpoint(
            RecordDeviceProxyRequest(udid=udid, ssid="wifi", client_ip="10.0.0.9"),
            request,
        )


class TestTheProxyConfigHalfOfTheJoin:
    """The damage `_identity_aliases` exists to end, on the half that *writes*.

    `record_device_proxy_config` stored the raw udid and `_ip_map` feeds those
    keys straight into `owns()`, so a config recorded under the spelling Xcode
    shows never joined the actions logged under the other. Canonicalising the
    query alone made it worse: the flows then matched neither spelling, and the
    caller saw an action with an empty `flows` list -- "the app made no
    requests", which is the confidently wrong reading.

    The first version of this file's query-side test passed `flow_store=None`,
    so it never touched the half that was actually half-done. That is why this
    one builds a real flow.
    """

    def _flow(self, ip: str):
        import uuid as _uuid
        from datetime import UTC, datetime, timedelta

        from server.models import FlowRecord, FlowRequest

        return FlowRecord(
            id=_uuid.uuid4().hex,
            timestamp=datetime.now(UTC) - timedelta(seconds=1),
            request=FlowRequest(method="GET", url="https://x/", host="x", path="/"),
            client_ip=ip,
        )

    async def test_the_endpoint_canonicalises_what_it_stores(self):
        """Through the real handler with a *raw* udid.

        The first version of this test called `canonical_device_id` itself and
        passed the result in -- so it exercised the function and not the
        endpoint's use of it, and a mutation putting the raw udid back survived
        it. Mocking the thing under test is this file's own subject, arriving
        one level up.
        """
        from server.proxy.cert_state import read_cert_state

        await _record_proxy_config(HW_UDID)

        assert CD_UUID in read_cert_state(), "stored under the raw udid"
        assert HW_UDID not in read_cert_state()

    async def test_the_recorded_ip_then_joins_a_canonical_action(self):
        """The join itself, which is the point of storing it canonically."""
        from server.proxy.cert_state import read_cert_state
        from server.trace import ip_to_udid, owns

        await _record_proxy_config(HW_UDID)
        ip_map = ip_to_udid(read_cert_state())

        assert owns(CD_UUID, ip_map["10.0.0.9"][0]).value == "owns"

    async def test_a_cold_alias_map_is_warmed_rather_than_stored_raw(self):
        """`canonical_device_id` returns its input unchanged when nothing has
        enumerated, so on a server that has not listed devices this stored the
        raw udid and the canonicalisation silently did not apply -- the failure
        looking exactly like success.

        `device_pool.refresh()` warms the map at startup, but this endpoint is
        called early in setup and must not depend on that having happened. Note
        the map is *not* pre-warmed here: that is the point.
        """
        from server.proxy.cert_state import read_cert_state

        assert dc.canonical_device_id(HW_UDID) == HW_UDID, "map should start cold"

        await _record_proxy_config(HW_UDID)

        assert CD_UUID in read_cert_state()

    async def test_the_raw_spelling_would_not_have_joined(self):
        """Pins why this has to happen at the writer: `owns` tests equal
        strings and nothing else."""
        from server.trace import Ownership, owns

        await _listed()

        assert owns(CD_UUID, HW_UDID) is Ownership.FOREIGN


class TestTheHardwareSpellingIsCachedToo:
    """The type cache is keyed on the canonical spelling only, so
    `_ensure_device_type_cached(hardware_udid)` missed on *every* call -- a
    full simctl+devicectl+usbmux+adb enumeration each time, for a device
    already known.

    Counted rather than timed: a duration assertion on a mocked backend
    measures the mock.
    """

    async def test_a_repeated_hardware_udid_enumerates_once(self):
        await _listed()
        ctrl = DeviceController()
        calls = {"n": 0}

        async def _counting_list_devices():
            calls["n"] += 1
            await _listed()
            ctrl._device_type_cache[CD_UUID] = DeviceType.DEVICE
            return []

        ctrl.list_devices = _counting_list_devices

        for _ in range(3):
            await ctrl.resolve_udid(HW_UDID)

        assert calls["n"] == 1, (
            f"enumerated {calls['n']} times for one device already known"
        )


class TestTheActiveDeviceEndpointReportsWhatItStored:
    """Echoing the request told a caller who passed the hardware udid that it
    was the active device, while every later comparison -- the trace filter
    among them -- used the canonical one. The same shape as `simctl launch`
    reporting the launch it was asked for."""

    async def test_it_echoes_the_canonical_udid(self):
        from types import SimpleNamespace

        from server.api.device import set_active_device
        from server.models import ShutdownDeviceRequest

        await _listed()
        ctrl = _isolated_controller()
        ctrl._ensure_device_type_cached = AsyncMock()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            device_controller=ctrl,
        )))

        result = await set_active_device(request, ShutdownDeviceRequest(udid=HW_UDID))

        assert result["active_udid"] == CD_UUID
