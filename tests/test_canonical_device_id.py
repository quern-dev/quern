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

import pytest

from server.device import devicectl as dc
from server.device.controller import DeviceController
from server.models import DeviceType

#: This file *is* the discovery code -- the autouse stubs in conftest replace
#: these very methods, so it opts out rather than asserting against them.
#: The subprocess layer beneath is mocked, so nothing here reaches the machine.
pytestmark = pytest.mark.device_discovery

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


@pytest.fixture(autouse=True)
def _clean_alias_map():
    dc._identity_aliases.clear()
    yield
    dc._identity_aliases.clear()


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
