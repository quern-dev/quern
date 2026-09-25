"""A device's kind is a fact about the device, not about how it is reached.

`DeviceType` used to be inferred from the transport address —
`serial.startswith("emulator-")` — and then used as a proxy for capability.
Both halves were wrong, and they compounded: one running AVD reachable as both
`emulator-5554` and `localhost:5555` was reported as two different kinds of
device, and the TCP spelling was refused a certificate install that the very
same device, through the very same `adb root`, could complete.

Measured on a Pixel_6_Dev AVD with both transports live at once. Every device
property was byte-identical across the two serials, `adb root` returned
`uid=0(root)` through both, and only `adb emu avd name` differed — which is
the shape of the fix: what the device *is* comes from its properties, what
this *connection* can do comes from the transport.

The property strings below are captured from that emulator, from a physical
LG H932, and from the matrix in #299.

See #299, #264, #263.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.adb import AdbBackend
from server.device.controller import DeviceController
from server.models import DeviceError, DeviceType

# Captured verbatim from the booted Pixel_6_Dev AVD.
EMULATOR_PROPS = {
    "ro.kernel.qemu": "1",
    "ro.hardware": "ranchu",
    "ro.build.characteristics": "emulator",
    "ro.product.model": "sdk_gphone64_arm64",
    "ro.build.tags": "dev-keys",
    "ro.debuggable": "1",
}

# Captured verbatim from the physical LG H932 over USB.
PHONE_PROPS = {
    "ro.hardware": "joan",
    "ro.product.model": "LG-H932",
    "ro.build.tags": "release-keys",
    "ro.debuggable": "0",
    "ro.build.characteristics": "default",
}


class TestTheDeviceIsAskedWhatItIs:
    def test_an_emulator_is_recognised_by_its_properties(self):
        assert AdbBackend.classify_from_properties(EMULATOR_PROPS) == (
            DeviceType.ANDROID_EMULATOR
        )

    def test_a_phone_is_recognised_by_its_properties(self):
        assert AdbBackend.classify_from_properties(PHONE_PROPS) == (
            DeviceType.ANDROID_DEVICE
        )

    @pytest.mark.parametrize("props", [
        {"ro.kernel.qemu": "1"},
        {"ro.boot.qemu": "1"},
        {"ro.hardware": "ranchu"},
        {"ro.hardware": "goldfish"},
        {"ro.build.characteristics": "emulator"},
        {"ro.build.characteristics": "nosdcard,emulator"},
        {"ro.product.model": "sdk_gphone64_arm64"},
    ])
    def test_any_one_signal_is_enough(self, props):
        """Images vary in which of these they set, so no single property can
        be the test on its own."""
        assert AdbBackend.classify_from_properties(props) == DeviceType.ANDROID_EMULATOR

    def test_a_device_that_could_not_be_asked_is_not_guessed_at(self):
        """None, not a default. An offline or unauthorized device has no shell
        to answer with, and inventing an answer is what #263 is about."""
        assert AdbBackend.classify_from_properties({}) is None

    def test_a_phone_is_not_an_emulator_merely_for_having_properties(self):
        """The negative control: without this, a classifier that returned
        ANDROID_EMULATOR unconditionally would pass every test above."""
        assert AdbBackend.classify_from_properties(
            {"ro.hardware": "qcom", "ro.product.model": "Pixel 3 XL"}
        ) == DeviceType.ANDROID_DEVICE


class TestTheAddressIsNotTheDevice:
    """The decisive case from #299's matrix: one AVD, two serials."""

    async def test_an_emulator_reached_over_tcp_is_still_an_emulator(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=EMULATOR_PROPS))
        monkeypatch.setattr(adb, "_run_adb_for_device",
                            AsyncMock(return_value=("localhost:5555\tdevice", "")))

        props = await adb.get_device_properties("localhost:5555")
        assert adb.classify_from_properties(props) == DeviceType.ANDROID_EMULATOR

    async def test_a_phone_on_a_tcp_serial_is_still_a_phone(self, monkeypatch):
        """The other direction, which the prefix test got right only by
        coincidence -- 'no emulator- prefix' and 'is a phone' happened to
        agree for every physical device, which is why this hid for so long."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=PHONE_PROPS))

        props = await adb.get_device_properties("192.168.1.8:5555")
        assert adb.classify_from_properties(props) == DeviceType.ANDROID_DEVICE


class TestCapabilityIsAskedOfTheRightThing:
    """Cert install follows the device; `adb emu` follows the transport. One
    `DeviceType` cannot answer both, which is the finding under #299."""

    async def test_dev_keys_is_rootable(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value={"ro.build.tags": "dev-keys"}))
        assert await adb.is_rootable("X") is True

    async def test_a_userdebug_build_is_rootable_despite_release_keys(self, monkeypatch):
        """`ro.debuggable` alone is sufficient; testing tags only under-reports."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties", AsyncMock(
            return_value={"ro.build.tags": "release-keys", "ro.debuggable": "1"}))
        assert await adb.is_rootable("X") is True

    async def test_a_release_build_is_not_rootable(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=PHONE_PROPS))
        assert await adb.is_rootable("X") is False

    async def test_rootability_does_not_depend_on_the_serial(self, monkeypatch):
        """The false negative in the matrix: the same dev-keys emulator was
        refused a cert over TCP and allowed it over the console. `adb root`
        returned uid=0 through both."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=EMULATOR_PROPS))

        assert await adb.is_rootable("emulator-5554") is True
        assert await adb.is_rootable("localhost:5555") is True

    async def test_the_console_answers_on_the_console_serial(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device",
                            AsyncMock(return_value=("Pixel_6_Dev\nOK", "")))
        assert await adb.has_emulator_console("emulator-5554") is True

    async def test_the_console_does_not_answer_over_tcp(self, monkeypatch):
        """Measured: empty output on `localhost:5555` for the same AVD. This
        is a real incapacity, not a misclassification -- `emu kill` and
        `emu geo fix` genuinely cannot be issued through that serial."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device",
                            AsyncMock(return_value=("", "")))
        assert await adb.has_emulator_console("localhost:5555") is False

    async def test_an_error_reply_is_not_a_console(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device",
                            AsyncMock(return_value=("error: unknown command", "")))
        assert await adb.has_emulator_console("localhost:5555") is False


class TestPropertiesAreReadInOneCall:
    async def test_the_getprop_dump_is_parsed(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device", AsyncMock(return_value=(
            "[ro.build.tags]: [dev-keys]\n"
            "[ro.hardware]: [ranchu]\n"
            "[persist.sys.locale]: [en-US]\n", "")))

        props = await adb.get_device_properties("X")

        assert props["ro.build.tags"] == "dev-keys"
        assert props["ro.hardware"] == "ranchu"
        assert props["persist.sys.locale"] == "en-US"

    async def test_a_value_containing_brackets_survives(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device", AsyncMock(
            return_value=("[bluetooth.device.class_of_device]: [90,2,12]\n", "")))
        props = await adb.get_device_properties("X")
        assert props["bluetooth.device.class_of_device"] == "90,2,12"

    async def test_a_device_that_cannot_be_shelled_yields_nothing(self, monkeypatch):
        """Not an exception: an offline device during enumeration is normal."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device",
                            AsyncMock(side_effect=RuntimeError("device offline")))
        assert await adb.get_device_properties("X") == {}


class TestTheSimulatorGuardRefusesWhatItCannotServe:
    """It tested *against* physical iOS, so every other kind walked through a
    guard whose entire purpose was to stop them, and reached `simctl` with an
    adb serial. Verified before the fix: ANDROID_DEVICE, ANDROID_EMULATOR and
    '' all passed. See #263."""

    @pytest.mark.parametrize("kind", [
        DeviceType.ANDROID_DEVICE,
        DeviceType.ANDROID_EMULATOR,
        DeviceType.DEVICE,
    ])
    def test_a_non_simulator_is_refused(self, kind):
        ctrl = DeviceController()
        ctrl._device_type_cache["X"] = kind
        with pytest.raises(DeviceError):
            ctrl._require_simulator("X", "Erase")

    def test_a_simulator_passes(self):
        """The positive control: without it, a guard that refused everything
        would satisfy every other test here."""
        ctrl = DeviceController()
        ctrl._device_type_cache["X"] = DeviceType.SIMULATOR
        ctrl._require_simulator("X", "Erase")

    @pytest.mark.parametrize("udid", ["never-seen-before", ""])
    def test_an_unknown_device_is_refused_rather_than_assumed(self, udid):
        ctrl = DeviceController()
        with pytest.raises(DeviceError):
            ctrl._require_simulator(udid, "Erase")


class TestAnUnknownDeviceIsNotAGuess:
    def test_an_unseen_udid_has_no_type(self):
        assert DeviceController()._device_type("never-seen-before") is None

    def test_the_empty_udid_has_no_type(self):
        """It reached `simctl` as a simulator, which on an Android-only host
        means dispatching to a toolchain that is not installed -- how
        'unsupported device' became 'xcrun not found'."""
        assert DeviceController()._device_type("") is None

    def test_a_known_udid_still_answers(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["X"] = DeviceType.ANDROID_DEVICE
        assert ctrl._device_type("X") == DeviceType.ANDROID_DEVICE

    def test_an_unknown_device_is_not_android_either(self):
        """`_is_android` must not become the new default-shaped guess."""
        assert DeviceController()._is_android("never-seen") is False
        assert DeviceController()._is_physical("never-seen") is False
