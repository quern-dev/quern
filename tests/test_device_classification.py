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
    "ro.build.version.sdk": "34",
}

# Captured verbatim from the physical LG H932 over USB.
PHONE_PROPS = {
    "ro.hardware": "joan",
    "ro.product.model": "LG-H932",
    "ro.build.tags": "release-keys",
    "ro.debuggable": "0",
    "ro.build.characteristics": "default",
    "ro.build.version.sdk": "28",
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

    @pytest.mark.parametrize("signal", [
        {"ro.kernel.qemu": "1"},
        {"ro.boot.qemu": "1"},
        {"ro.hardware": "ranchu"},
        {"ro.hardware": "goldfish"},
        {"ro.build.characteristics": "emulator"},
        {"ro.build.characteristics": "nosdcard,emulator"},
        {"ro.product.model": "sdk_gphone64_arm64"},
    ])
    def test_any_one_signal_is_enough(self, signal):
        # Plus the sentinel, so this tests the signal rather than accidentally
        # testing the did-the-read-work guard.
        props = {"ro.build.version.sdk": "34", **signal}
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
            {"ro.build.version.sdk": "34", "ro.hardware": "qcom",
             "ro.product.model": "Pixel 3 XL"}
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
        monkeypatch.setattr(adb, "get_device_properties", AsyncMock(
            return_value={"ro.build.version.sdk": "34", "ro.build.tags": "dev-keys"}))
        assert await adb.is_rootable("X") is True

    async def test_a_userdebug_build_is_rootable_despite_release_keys(self, monkeypatch):
        """`ro.debuggable` alone is sufficient; testing tags only under-reports."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties", AsyncMock(
            return_value={"ro.build.version.sdk": "34",
                          "ro.build.tags": "release-keys", "ro.debuggable": "1"}))
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

    async def test_a_value_containing_a_comma_survives(self, monkeypatch):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device", AsyncMock(
            return_value=("[bluetooth.device.class_of_device]: [90,2,12]\n", "")))
        props = await adb.get_device_properties("X")
        assert props["bluetooth.device.class_of_device"] == "90,2,12"

    async def test_a_value_ending_in_a_bracket_keeps_it(self, monkeypatch):
        """The old name of this test said "containing brackets" while its
        fixture was `90,2,12`, which has none -- so `rstrip("]")` and a plain
        `[:-1]` were indistinguishable, though they differ exactly here. No
        property on the four attached devices ends in `]`, so this is latent
        rather than observed, which is the reason to pin it rather than not."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "_run_adb_for_device", AsyncMock(
            return_value=("[some.prop]: [value]]\n", "")))
        props = await adb.get_device_properties("X")
        assert props["some.prop"] == "value]"

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


class TestClearAppDataHasAnAndroidPath:
    """Inverting the simulator guard turned this from a cryptic simctl failure
    into a confident false claim: "only supported on simulators" about a device
    whose `pm clear` answers `Success`. Measured on an unrooted release-keys
    Pixel 3 XL, which needs no root for it."""

    async def test_android_clears_through_pm(self, monkeypatch):
        ctrl = DeviceController()
        ctrl._device_type_cache["PHONE"] = DeviceType.ANDROID_DEVICE
        ctrl.adb.clear_app_data = AsyncMock()
        ctrl.simctl.clear_app_data = AsyncMock()
        monkeypatch.setattr(ctrl, "resolve_udid", AsyncMock(return_value="PHONE"))

        assert await ctrl.clear_app_data("com.example.App") == "PHONE"
        ctrl.adb.clear_app_data.assert_awaited_once_with("PHONE", "com.example.App")
        ctrl.simctl.clear_app_data.assert_not_awaited()

    async def test_a_simulator_still_goes_to_simctl(self, monkeypatch):
        """The positive control: an Android path that swallowed everything
        would satisfy the test above on its own."""
        ctrl = DeviceController()
        ctrl._device_type_cache["SIM"] = DeviceType.SIMULATOR
        ctrl.adb.clear_app_data = AsyncMock()
        ctrl.simctl.terminate_app = AsyncMock()
        ctrl.simctl.clear_app_data = AsyncMock()
        monkeypatch.setattr(ctrl, "resolve_udid", AsyncMock(return_value="SIM"))

        await ctrl.clear_app_data("com.example.App")
        ctrl.simctl.clear_app_data.assert_awaited_once()
        ctrl.adb.clear_app_data.assert_not_awaited()

    async def test_a_failed_clear_is_reported(self):
        adb = AdbBackend()
        adb._run_adb_for_device = AsyncMock(return_value=("Failed", ""))
        with pytest.raises(DeviceError):
            await adb.clear_app_data("PHONE", "com.does.not.exist")

    async def test_a_successful_clear_does_not_raise(self):
        adb = AdbBackend()
        adb._run_adb_for_device = AsyncMock(return_value=("Success", ""))
        await adb.clear_app_data("PHONE", "com.example.App")


class TestARefusalSaysWhyRatherThanJustNo:
    """Eleven operations went from silently reaching simctl with an adb serial
    to being refused. A refusal that does not say what the device is leaves the
    caller no better off than the cryptic failure it replaced."""

    def test_an_android_refusal_names_android(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["Z"] = DeviceType.ANDROID_DEVICE
        with pytest.raises(DeviceError) as e:
            ctrl._require_simulator("Z", "Set hardware keyboard")
        assert "Android" in str(e.value)

    def test_an_unknown_refusal_says_it_is_unknown(self):
        ctrl = DeviceController()
        with pytest.raises(DeviceError) as e:
            ctrl._require_simulator("Z", "Set hardware keyboard")
        assert "does not recognise" in str(e.value)

    @pytest.mark.parametrize("kind", [
        DeviceType.ANDROID_DEVICE, DeviceType.ANDROID_EMULATOR, DeviceType.DEVICE, None,
    ])
    def test_every_refusal_keeps_the_phrase_that_maps_to_400(self, kind):
        """`_handle_device_error` matches the literal string to return 400
        (`api/device.py`). Rewording it wholesale would turn each of these
        refusals into a 500 with nothing to indicate it had happened."""
        ctrl = DeviceController()
        if kind is not None:
            ctrl._device_type_cache["Z"] = kind
        with pytest.raises(DeviceError) as e:
            ctrl._require_simulator("Z", "Erase")
        assert "only supported on simulators" in str(e.value)


class TestAPartialReadIsNotAnAnswer:
    """Emptiness alone was too weak a test. A truncated `getprop` that happens
    to omit the qemu keys would fall through to "physical device" with full
    confidence -- a failed check reading as a passing one."""

    def test_a_read_missing_the_sentinel_is_unknown(self):
        """Every emulator signal absent, but so is `ro.build.version.sdk`, so
        this is a broken read rather than a phone."""
        assert AdbBackend.classify_from_properties(
            {"persist.sys.locale": "en-US", "apexd.status": "ready"}
        ) is None

    def test_a_truncated_read_that_kept_the_model_is_still_unknown(self):
        """The dangerous shape: enough to look like a real answer, not enough
        to be one."""
        assert AdbBackend.classify_from_properties(
            {"ro.product.model": "sdk_gphone64_arm64"}
        ) is None

    def test_the_sentinel_is_present_on_real_reads(self):
        """Both captured fixtures carry it, so requiring it costs nothing on a
        healthy device -- the guard would be useless if it rejected real
        output."""
        for props in (EMULATOR_PROPS, PHONE_PROPS):
            assert "ro.build.version.sdk" in props
            assert AdbBackend.classify_from_properties(props) is not None


@pytest.mark.device_discovery
class TestTransportIsRecordedInTheModel:
    """`DeviceInfo.connection_type` is filled in by devicectl and usbmux for
    iOS and was left empty for Android, so transport was modelled on one
    platform and re-derived from the serial on the other.

    Marked `device_discovery` because conftest otherwise stubs every backend's
    `list_devices` to `[]`, deliberately -- an unmarked test that called the
    real one would enumerate the developer's actual hardware. These drive it
    with captured `adb devices -l` output instead.
    """

    async def _list(self, monkeypatch, devices_line):
        adb = AdbBackend()
        monkeypatch.setattr(adb, "is_installed", lambda: True)
        monkeypatch.setattr(adb, "_run_adb", AsyncMock(
            return_value=(f"List of devices attached\n{devices_line}\n", "")))
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=EMULATOR_PROPS))
        monkeypatch.setattr(adb, "_get_emulator_name", AsyncMock(return_value=""))
        monkeypatch.setattr(adb, "list_avds", AsyncMock(return_value=[]))
        return await adb.list_devices()

    async def test_a_usb_device_says_usb(self, monkeypatch):
        d = await self._list(monkeypatch,
            "8BAY0WCL7\tdevice usb:0-1.2 product:crosshatch model:Pixel_3_XL")
        assert d[0].connection_type == "usb"

    async def test_a_console_emulator_says_emulator(self, monkeypatch):
        d = await self._list(monkeypatch,
            "emulator-5554\tdevice product:sdk_gphone64_arm64 model:sdk_gphone64_arm64")
        assert d[0].connection_type == "emulator"

    async def test_a_tcp_attachment_says_tcp(self, monkeypatch):
        """The case the whole change is about: an emulator over the wire is
        neither 'usb' nor the local console."""
        d = await self._list(monkeypatch,
            "localhost:5555\tdevice product:sdk_gphone64_arm64 model:sdk_gphone64_arm64")
        assert d[0].connection_type == "tcp"
        assert d[0].device_type == DeviceType.ANDROID_EMULATOR


class TestRootabilityCanSayItDoesNotKnow:
    """`is_rootable` used to answer a confident `False` from a truncated read,
    while `classify_from_properties` answered `None` from byte-identical
    input. A dev-keys emulator mid-boot was told its build was "release-keys,
    not debuggable" -- a reason invented about data that never arrived."""

    async def test_a_truncated_read_is_unknown_not_unrootable(self, monkeypatch):
        adb = AdbBackend()
        truncated = {"persist.sys.locale": "en-US", "apexd.status": "ready"}
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=truncated))
        monkeypatch.setattr(adb, "_get_device_property", AsyncMock(return_value=""))

        assert await adb.is_rootable("X") is None
        # The sibling agrees, which is the point: same data, same verdict.
        assert adb.classify_from_properties(truncated) is None

    async def test_the_single_property_fallback_still_answers(self, monkeypatch):
        """A bulk read can fail while individual reads work; that is a real
        answer and must not be flattened into None."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties", AsyncMock(return_value={}))
        monkeypatch.setattr(adb, "_get_device_property",
                            AsyncMock(side_effect=["dev-keys", "0"]))
        assert await adb.is_rootable("X") is True

    async def test_a_definite_no_is_still_false(self, monkeypatch):
        """The negative control: None must not swallow a real refusal."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value=PHONE_PROPS))
        assert await adb.is_rootable("X") is False


class TestRootabilityNeedsTheKeysItAsksAbout:
    """A read can carry the sentinel and still not answer *this* question."""

    async def test_sentinel_without_the_rootability_keys_falls_back(self, monkeypatch):
        """`ro.build.version.sdk` present, `ro.build.tags` and `ro.debuggable`
        absent. Comparing two missing values returned a confident False while
        a single-property read of the same device said `dev-keys`."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties",
                            AsyncMock(return_value={"ro.build.version.sdk": "34"}))
        monkeypatch.setattr(adb, "_get_device_property",
                            AsyncMock(side_effect=["dev-keys", "0"]))

        assert await adb.is_rootable("X") is True

    async def test_one_key_present_is_enough_to_answer(self, monkeypatch):
        """`ro.debuggable` alone is a real answer and must not trigger a
        second round of reads."""
        adb = AdbBackend()
        monkeypatch.setattr(adb, "get_device_properties", AsyncMock(
            return_value={"ro.build.version.sdk": "34", "ro.debuggable": "1"}))
        single = AsyncMock(return_value="")
        monkeypatch.setattr(adb, "_get_device_property", single)

        assert await adb.is_rootable("X") is True
        single.assert_not_awaited()


class TestBootWarmsTheCacheItGuardsOn:
    """`boot` is the one device entry point that does not go through
    `resolve_udid`, so nothing else warms the cache -- and since `_device_type`
    stopped guessing SIMULATOR, an unwarmed cache made a valid simulator
    unrecognised and refused."""

    async def test_an_unwarmed_simulator_is_not_refused(self, monkeypatch):
        ctrl = DeviceController()
        ctrl.simctl.boot = AsyncMock()
        monkeypatch.setattr(ctrl, "_restore_input_after_boot", AsyncMock())

        async def warm(udid):
            ctrl._device_type_cache[udid] = DeviceType.SIMULATOR

        monkeypatch.setattr(ctrl, "_ensure_device_type_cached", warm)

        assert await ctrl.boot(udid="SIM-1") == "SIM-1"
        ctrl.simctl.boot.assert_awaited_once_with("SIM-1")

    async def test_a_device_still_unknown_after_warming_is_refused(self, monkeypatch):
        """The positive control: warming must not become a way to admit
        anything at all."""
        ctrl = DeviceController()
        ctrl.simctl.boot = AsyncMock()
        monkeypatch.setattr(ctrl, "_ensure_device_type_cached", AsyncMock())

        with pytest.raises(DeviceError):
            await ctrl.boot(udid="STILL-UNKNOWN")
        ctrl.simctl.boot.assert_not_awaited()
