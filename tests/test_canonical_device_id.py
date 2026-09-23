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
from server.proxy.cert_state import CERT_STATE_FILE

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
def _a_clean_cert_state():
    """Cert state is a file under `QUERN_STATE_DIR`, shared by every test here.

    Without this, records accumulate across tests and the assertions become
    order-dependent: a config written by an earlier test turns up in a later
    one's merge. Scoped to this file rather than conftest -- these are the only
    tests that write it, and a repo-wide clear would be a bigger claim than is
    warranted.
    """
    from server.proxy.cert_state import CERT_STATE_FILE

    CERT_STATE_FILE.unlink(missing_ok=True)
    yield
    CERT_STATE_FILE.unlink(missing_ok=True)


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

        await _listed()          # discovery has run, as on any live server
        await _record_proxy_config(HW_UDID)

        # The *file*, not `read_cert_state`, which canonicalises on the way out
        # and would report success whether or not the writer did anything.
        # Asserting through the layer that masks the behaviour under test is
        # this file's own subject; the first version did exactly that and the
        # "store it raw again" mutation survived.
        on_disk = json.loads(CERT_STATE_FILE.read_text())

        assert CD_UUID in on_disk, "the writer did not canonicalise"
        assert HW_UDID not in on_disk

    async def test_a_key_from_before_canonicalisation_is_repaired_on_read(self):
        """No migration. Cert state written by an older quern holds raw
        hardware udids, and those must still join actions logged
        canonically."""
        from server.proxy.cert_state import read_cert_state, record_device_proxy_config
        from server.trace import ip_to_udid

        record_device_proxy_config(HW_UDID, "old", "10.0.0.2", 9101, client_ip="10.0.0.7")
        await _listed()

        assert ip_to_udid(read_cert_state())["10.0.0.7"][0] == CD_UUID

    async def test_the_recorded_ip_then_joins_a_canonical_action(self):
        """The join itself, which is the point of storing it canonically."""
        from server.proxy.cert_state import read_cert_state
        from server.trace import ip_to_udid, owns

        await _listed()
        await _record_proxy_config(HW_UDID)
        ip_map = ip_to_udid(read_cert_state())

        assert owns(CD_UUID, ip_map["10.0.0.9"][0]).value == "owns"

    async def test_a_cold_map_stores_raw_and_the_reader_repairs_it(self):
        """The endpoint no longer refreshes the device list, so a cold map
        stores the raw udid -- and `ip_to_udid` canonicalises on the way out,
        which is what makes that harmless.

        Warming here was wrong twice over: every unrecognised udid triggered a
        full four-backend enumeration with no negative cache (CWE-400), and a
        refresh that *failed* swallowed the error and wrote the raw udid
        anyway, leaving a permanently wrong key that survived discovery
        recovering. Reading fixes the cold case, the failed case, and every
        file written before canonicalisation existed.
        """
        from server.proxy.cert_state import read_cert_state
        from server.trace import ip_to_udid

        assert dc.canonical_device_id(HW_UDID) == HW_UDID, "map should start cold"
        await _record_proxy_config(HW_UDID)
        assert HW_UDID in read_cert_state(), "cold map stores raw, as expected"

        await _listed()          # discovery runs later, as it does on a live server

        assert ip_to_udid(read_cert_state())["10.0.0.9"][0] == CD_UUID, (
            "a key written while the map was cold never became usable"
        )


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


class TestEveryCertStateReaderSeesOneSpelling:
    """Canonicalising one reader was not enough, and the gap was specific.

    `ip_to_udid` was fixed, which repaired trace lookups -- while
    `_verify_physical_device` read cert state by the *canonical* key, found
    nothing under it, and reported `proxy_not_configured` for a device whose
    proxy was configured. There are eight readers; `read_cert_state` is the one
    place they all pass through.
    """

    def _record_raw(self, ssid: str = "home", ip: str = "10.0.0.9") -> None:
        """A config filed under the hardware udid, as a cold map or an older
        quern would have written it."""
        from server.proxy.cert_state import record_device_proxy_config

        record_device_proxy_config(HW_UDID, ssid, "10.0.0.2", 9101, client_ip=ip)

    async def test_a_direct_key_lookup_finds_it(self):
        """The shape `_verify_physical_device` uses: state[canonical_udid]."""
        from server.proxy.cert_state import read_cert_state

        self._record_raw()
        await _listed()

        assert read_cert_state().get(CD_UUID) is not None, (
            "a reader keyed on the canonical udid cannot see the record"
        )

    async def test_read_cert_state_for_device_finds_it_too(self):
        from server.proxy.cert_state import read_cert_state_for_device

        self._record_raw()
        await _listed()

        assert read_cert_state_for_device(CD_UUID) is not None

    async def test_an_unknown_udid_is_left_alone(self):
        """Simulators have one spelling. Canonicalisation must not rename
        anything it does not recognise."""
        from server.proxy.cert_state import read_cert_state, record_device_proxy_config

        record_device_proxy_config("SIM-1234", "home", "10.0.0.2", 9101)
        await _listed()

        assert "SIM-1234" in read_cert_state()


class TestTwoSpellingsOfOneDeviceAreMerged:
    """An old file can hold both spellings -- one written before
    canonicalisation, one after. Taking the later record wholesale drops the
    other's `wifi_proxy_configs`, which is the data the trace needs to
    attribute that device's flows at all.

    Caught by a test going red, not by review: a config recorded while the
    alias map was cold vanished when a second record for the same device
    arrived."""

    def _file_with_both_spellings(self) -> None:
        """Written directly, because the writer cannot produce this state.

        `record_device_proxy_config` reads through `read_cert_state`, which
        canonicalises and merges -- so going through it merges the records
        before they reach disk, and a merge bug in the reader becomes
        invisible. The file this builds is what an older quern left behind:
        both spellings, each with its own configs.
        """
        CERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CERT_STATE_FILE.write_text(json.dumps({
            HW_UDID: {"wifi_proxy_configs": {"old-wifi": {
                "client_ip": "10.0.0.7", "proxy_host": "10.0.0.2",
                "proxy_port": 9101, "set_at": "2026-09-01T00:00:00+00:00",
            }}},
            CD_UUID: {"wifi_proxy_configs": {"new-wifi": {
                "client_ip": "10.0.0.9", "proxy_host": "10.0.0.2",
                "proxy_port": 9101, "set_at": "2026-09-20T00:00:00+00:00",
            }}},
        }))

    async def test_configs_from_both_spellings_survive(self):
        from server.proxy.cert_state import read_cert_state

        self._file_with_both_spellings()
        await _listed()

        configs = read_cert_state()[CD_UUID]["wifi_proxy_configs"]

        assert set(configs) == {"old-wifi", "new-wifi"}, (
            "merging two spellings dropped a proxy config"
        )

    async def test_both_addresses_still_map_to_the_device(self):
        """The reason it matters: each config carries a client_ip, and a lost
        one is a device whose flows stop being attributed."""
        from server.proxy.cert_state import read_cert_state
        from server.trace import ip_to_udid

        self._file_with_both_spellings()
        await _listed()
        ip_map = ip_to_udid(read_cert_state())

        assert ip_map["10.0.0.7"][0] == CD_UUID
        assert ip_map["10.0.0.9"][0] == CD_UUID


class TestTheLookupKeyIsCanonicalisedToo:
    """`read_cert_state` canonicalises what it returns, so a caller asking by
    the hardware udid looked for a key that had just been rewritten to the
    other spelling -- the same miss this change exists to end, one layer
    down."""

    async def test_a_hardware_udid_finds_the_record(self):
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            record_device_proxy_config,
        )

        record_device_proxy_config(CD_UUID, "home", "10.0.0.2", 9101, client_ip="10.0.0.9")
        await _listed()

        assert read_cert_state_for_device(HW_UDID) is not None

    async def test_the_canonical_one_still_does(self):
        from server.proxy.cert_state import (
            read_cert_state_for_device,
            record_device_proxy_config,
        )

        record_device_proxy_config(CD_UUID, "home", "10.0.0.2", 9101, client_ip="10.0.0.9")
        await _listed()

        assert read_cert_state_for_device(CD_UUID) is not None


class TestADuplicateSsidIsResolvedByTime:
    """Both spellings of one device carry their own history, and file order
    says nothing about which was written later. Taking the later *entry* could
    resurrect a proxy address the device had already moved away from, and the
    trace would then attribute its flows by a stale `client_ip`."""

    def _both(self, hw_set_at: str, cd_set_at: str) -> None:
        CERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CERT_STATE_FILE.write_text(json.dumps({
            HW_UDID: {"wifi_proxy_configs": {"home": {
                "client_ip": "10.0.0.7", "proxy_host": "10.0.0.2",
                "proxy_port": 9101, "set_at": hw_set_at,
            }}},
            CD_UUID: {"wifi_proxy_configs": {"home": {
                "client_ip": "10.0.0.9", "proxy_host": "10.0.0.2",
                "proxy_port": 9101, "set_at": cd_set_at,
            }}},
        }))

    async def test_the_newer_entry_wins_even_when_listed_first(self):
        from server.proxy.cert_state import read_cert_state

        # The hardware record is listed first and is the *newer* one.
        self._both("2026-09-20T00:00:00+00:00", "2026-09-01T00:00:00+00:00")
        await _listed()

        config = read_cert_state()[CD_UUID]["wifi_proxy_configs"]["home"]

        assert config["client_ip"] == "10.0.0.7", (
            "file order beat the recorded time, resurrecting a stale address"
        )

    async def test_the_newer_entry_wins_when_listed_second(self):
        from server.proxy.cert_state import read_cert_state

        self._both("2026-09-01T00:00:00+00:00", "2026-09-20T00:00:00+00:00")
        await _listed()

        assert read_cert_state()[CD_UUID]["wifi_proxy_configs"]["home"]["client_ip"] == "10.0.0.9"

    async def test_a_dated_entry_beats_an_undated_one(self):
        """Knowing when beats not knowing, whichever order they appear in."""
        from server.proxy.cert_state import read_cert_state

        CERT_STATE_FILE.write_text(json.dumps({
            CD_UUID: {"wifi_proxy_configs": {"home": {
                "client_ip": "10.0.0.9", "proxy_host": "10.0.0.2", "proxy_port": 9101,
                "set_at": "2026-09-20T00:00:00+00:00",
            }}},
            HW_UDID: {"wifi_proxy_configs": {"home": {
                "client_ip": "10.0.0.7", "proxy_host": "10.0.0.2", "proxy_port": 9101,
            }}},
        }))
        await _listed()

        assert read_cert_state()[CD_UUID]["wifi_proxy_configs"]["home"]["client_ip"] == "10.0.0.9"


class TestARestoredSidecarIsCanonicalisedToo:
    """`__init__` writes the persisted udid straight into the backing field --
    deliberately, since restoring is not a change worth writing -- so it
    bypasses the setter that canonicalises. A sidecar written by an older
    quern, holding the hardware udid, therefore survives a restart.

    The restored-active branch returned that raw value on the first call, and
    `resolve_udid` records what it returns on the action. Trace ownership
    compares udids exactly, so the action read FOREIGN against everything
    recorded canonically -- the bug this branch exists to fix, arriving through
    a restart."""

    async def _restored(self, persisted: str) -> DeviceController:
        await _listed()
        ctrl = DeviceController()
        # Exactly what __init__ does with a persisted udid.
        ctrl._DeviceController__active_udid = persisted
        ctrl.list_devices = AsyncMock(return_value=[])
        ctrl._device_type_cache[CD_UUID] = DeviceType.DEVICE
        return ctrl

    async def test_a_raw_sidecar_resolves_to_the_canonical_udid(self):
        ctrl = await self._restored(HW_UDID)

        assert await ctrl.resolve_udid() == CD_UUID

    async def test_the_action_log_gets_the_canonical_one(self):
        """What the bug actually cost: the recorded udid."""
        from server.api.actions import ActionScope
        from server.logging_ext import reset_current_action, set_current_action

        ctrl = await self._restored(HW_UDID)
        scope = ActionScope("tap", "device.action")
        token = set_current_action(scope)
        try:
            await ctrl.resolve_udid()
        finally:
            reset_current_action(token)

        assert scope.udid == CD_UUID

    async def test_a_canonical_sidecar_is_unchanged(self):
        ctrl = await self._restored(CD_UUID)

        assert await ctrl.resolve_udid() == CD_UUID
