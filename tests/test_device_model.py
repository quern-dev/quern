"""Each device says what model it is (remote device listing follow-up).

A simulator's name is only its default and can be renamed; quern read the real
model (`deviceTypeIdentifier`) and kept only its family. Now `model` carries
it, physical devices report theirs, and a name criterion matches it -- so an
agent asking for an "iPad Pro 11-inch (M4)" finds one called "CI 3".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from server.device.ios.devicectl import DevicectlBackend
from server.device.ios.simctl import SimctlBackend
from server.device.pool import DevicePool
from server.models import DeviceError, DevicePoolEntry, DeviceState, DeviceType

PRO = "com.apple.CoreSimulator.SimDeviceType.iPhone-15-Pro"
IPAD = "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-11-inch-M4-8GB"
DEVICES = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-6": [
    {"udid": "A", "name": "CI 3", "state": "Booted", "isAvailable": True,
     "deviceTypeIdentifier": IPAD},
    {"udid": "B", "name": "iPhone 15 Pro", "state": "Shutdown", "isAvailable": True,
     "deviceTypeIdentifier": PRO},
]}}
TYPES = {"devicetypes": [{"identifier": PRO, "name": "iPhone 15 Pro"},
                         {"identifier": IPAD, "name": "iPad Pro 11-inch (M4)"}]}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(SimctlBackend, "_device_type_names", None)
    monkeypatch.setattr("server.device.ios.simctl.xcode_available", lambda: True)


def _simctl(answers):
    backend = SimctlBackend()
    calls = []

    async def run(*args):
        calls.append(args[:2])
        got = answers[args[1]]
        if isinstance(got, BaseException):
            raise got
        return json.dumps(got), ""

    backend._run_simctl = run
    return backend, calls


@pytest.mark.device_discovery
async def test_a_simulator_carries_its_model_whatever_it_is_called():
    backend, _ = _simctl({"devices": DEVICES, "devicetypes": TYPES})
    by = {d.udid: d for d in await backend.list_devices()}
    assert by["A"].name == "CI 3" and by["A"].model == "iPad Pro 11-inch (M4)"
    assert by["B"].model == "iPhone 15 Pro"


@pytest.mark.device_discovery
async def test_device_types_are_read_once_and_again_only_for_a_new_one():
    backend, calls = _simctl({"devices": DEVICES, "devicetypes": TYPES})
    await backend.list_devices()
    await backend.list_devices()
    assert calls.count(("list", "devicetypes")) == 1
    newer = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-1": [
        {"udid": "C", "name": "x", "state": "Shutdown", "isAvailable": True,
         "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17"}]}}
    backend2, calls2 = _simctl({"devices": newer, "devicetypes": TYPES})
    await backend2.list_devices()
    assert calls2.count(("list", "devicetypes")) == 1          # unknown id: asked again


@pytest.mark.device_discovery
async def test_device_types_that_cannot_be_read_leave_the_model_unknown():
    backend, _ = _simctl({"devices": DEVICES,
                          "devicetypes": DeviceError("simctl failed", tool="simctl")})
    devices = await backend.list_devices()
    assert len(devices) == 2 and all(d.model == "" for d in devices)


def _devicectl(hardware):
    return json.dumps({"result": {"devices": [{
        "identifier": "D1", "deviceProperties": {"name": "Jer's iPhone",
                                                 "osVersionNumber": "26.1"},
        "connectionProperties": {"transportType": "wired", "tunnelState": "connected",
                                 "pairingState": "paired"},
        "hardwareProperties": {"deviceType": "iPhone", "reality": "physical", "udid": "U1",
                               **hardware}}]}})


@pytest.mark.device_discovery
@pytest.mark.parametrize("hardware, model", [
    ({"marketingName": "iPhone 17 Pro", "productType": "iPhone18,1"}, "iPhone 17 Pro"),
    ({"productType": "iPhone16,1"}, "iPhone16,1"),     # no marketing name: still the model
    ({}, ""),
])
async def test_a_physical_device_carries_its_model(hardware, model):
    backend = DevicectlBackend()
    backend._run_devicectl = AsyncMock(return_value=(_devicectl(hardware), ""))
    devices = await backend.list_devices()
    assert [d.model for d in devices] == [model]


def _entry(udid, name, model=""):
    return DevicePoolEntry(udid=udid, name=name, state=DeviceState.BOOTED,
                           device_type=DeviceType.SIMULATOR, os_version="iOS 18.6",
                           runtime="r", model=model, last_used=datetime.now(UTC),
                           is_available=True)


def test_a_name_criterion_finds_a_renamed_simulator_by_its_model():
    pool = [_entry("A", "CI 3", "iPad Pro 11-inch (M4)"), _entry("B", "iPad Air", "iPad Air")]
    assert [d.udid for d in DevicePool._filter_by_name(pool, "iPad Pro 11-inch (M4)")] == ["A"]
    assert [d.udid for d in DevicePool._filter_by_name(pool, "ipad pro")] == ["A"]


def test_an_exact_name_or_model_still_beats_a_substring():
    pool = [_entry("A", "iPhone 15 Pro", "iPhone 15 Pro"),
            _entry("B", "iPhone 15 Pro Max", "iPhone 15 Pro Max")]
    assert [d.udid for d in DevicePool._filter_by_name(pool, "iPhone 15 Pro")] == ["A"]


def test_an_unknown_model_never_matches_by_accident():
    pool = [_entry("A", "CI 3", "")]
    assert DevicePool._filter_by_name(pool, "iPad") == []
