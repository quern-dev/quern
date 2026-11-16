"""A Mac with no WebDriverAgent to fall back on (#664).

No WDA is the default way to drive an iPhone on iOS 27 and later, and WDA is
built and run with Xcode. So on a Mac without Xcode, or with no WDA built,
quern must never start a WDA that cannot run, and every place it would fall
back to one says why it cannot. After a #669 crash the device stays on the
push, rather than being moved to a WDA that is not there.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.ios import coredevice_ax
from server.models import DeviceError, WdaNotSetUpError
from server.tooling import wda_availability
from tests.test_coredevice_ax import PHONE, _phone_controller

NO_XCODE = "Xcode is not installed, and WebDriverAgent is built and run with it"

#: The real cached check, taken before conftest's fixture replaces the name.
REAL_REASON = wda_availability.wda_unavailable_reason


@pytest.fixture
def no_wda(monkeypatch):
    async def missing():
        return NO_XCODE
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", missing)


# -- the check ---------------------------------------------------------------------


class TestTheCheck:
    def _patch(self, monkeypatch, *, xcode=True, build="17A1", built=True, raises=None):
        monkeypatch.setattr("server.device.ios._xcode.xcode_available", lambda: xcode)

        async def build_id():
            if raises:
                raise raises
            return build
        monkeypatch.setattr("server.device.ios.wda._xcode_build_id", build_id)

        def xctestrun():
            if not built:
                raise WdaNotSetUpError("no .xctestrun", tool="wda")
            return "x.xctestrun"
        monkeypatch.setattr("server.device.ios.wda._find_xctestrun", xctestrun)

    @pytest.mark.parametrize(("kw", "expect"), [
        ({}, None),
        ({"xcode": False}, "Xcode is not installed"),
        ({"build": None}, "Xcode is not installed"),       # the Command Line Tools only
        ({"built": False}, "has not been built"),
        ({"raises": OSError("no")}, None),                 # could not tell: no refusal
    ])
    async def test_the_reasons(self, monkeypatch, kw, expect):
        self._patch(monkeypatch, **kw)
        reason = await wda_availability._check()
        if expect is None:
            assert reason is None
        else:
            assert expect in reason

    async def test_the_answer_is_kept_a_while_and_forgotten_on_setup(self, monkeypatch):
        check = AsyncMock(return_value=NO_XCODE)
        monkeypatch.setattr(wda_availability, "_check", check)
        monkeypatch.setattr(wda_availability, "_cached", None)

        assert await REAL_REASON() == NO_XCODE
        assert await REAL_REASON() == NO_XCODE
        assert check.await_count == 1
        wda_availability.forget()
        await REAL_REASON()
        assert check.await_count == 2

    async def test_it_is_asked_again_after_a_minute(self, monkeypatch):
        check = AsyncMock(return_value=None)
        monkeypatch.setattr(wda_availability, "_check", check)
        monkeypatch.setattr(wda_availability, "_cached", None)
        clock = [100.0]
        monkeypatch.setattr(wda_availability.time, "monotonic", lambda: clock[0])

        await REAL_REASON()
        clock[0] += wda_availability._TTL_S + 1        # Xcode installed meanwhile
        await REAL_REASON()
        assert check.await_count == 2

    async def test_setup_forgets_it_whatever_happens(self, monkeypatch):
        from server.device.ios import wda

        forgot = []
        monkeypatch.setattr("server.tooling.wda_availability.forget", lambda: forgot.append(1))
        monkeypatch.setattr(wda, "_setup_wda", AsyncMock(side_effect=DeviceError("x", tool="wda")))

        with pytest.raises(DeviceError):
            await wda.setup_wda("UDID", "iOS 27.2")
        assert forgot == [1]


# -- the gate ----------------------------------------------------------------------


async def test_wda_is_not_started_where_it_cannot_run(monkeypatch, no_wda):
    from server.device.ios.wda_client import WdaBackend

    client = WdaBackend()
    client._device_os_versions[PHONE] = "iOS 26.4"
    monkeypatch.setattr(client, "_try_tunneld_connection", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_start_usbmux_forward",
                        AsyncMock(side_effect=DeviceError("no forward", tool="wda")))
    start = AsyncMock()
    monkeypatch.setattr("server.device.ios.wda.start_driver", start)

    with pytest.raises(WdaNotSetUpError, match="Xcode is not installed"):
        await client._get_base_url(PHONE)
    start.assert_not_called()


# -- the push's fallbacks ------------------------------------------------------------


class TestThePushWithNothingBehindIt:
    async def test_a_failed_read_says_there_is_no_wda(self, monkeypatch, no_wda):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("refused", tool="coredevice"))

        with pytest.raises(DeviceError, match="refused.*no WebDriverAgent.*Xcode"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_a_crash_under_a_read_shows_what_it_left_and_stays_on_the_push(
            self, monkeypatch, no_wda):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        crash = coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711)
        springboard = [{"type": "Application", "AXLabel": " ", "AXUniqueId": None,
                        "frame": {"x": 0, "y": 0, "width": 414, "height": 896}}]
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=[crash, springboard])

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert [e.label for e in elements] == [" "]
        assert "no WebDriverAgent" in ctrl.read_note()
        ctrl.wda_client.describe_all.assert_not_called()
        # Recorded, so an action whose own read it was refuses (#698), but not
        # a reason to move the device to a WDA that is not there.
        assert ctrl._push_crashed_apps[PHONE] == {"QuernProbeScene"}
        assert ctrl._device_tree_reader(PHONE) is ctrl.coredevice_ax

    async def test_a_crash_under_a_lookup_refuses_and_stays_on_the_push(
            self, monkeypatch, no_wda):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))

        with pytest.raises(DeviceError, match="Nothing was looked up.*stays on the"):
            await ctrl.get_ui_elements(PHONE, filter_label="Wi-Fi", lookup=True)
        ctrl.wda_client.find_elements_by_query.assert_not_called()
        assert ctrl._device_tree_reader(PHONE) is ctrl.coredevice_ax

    async def test_with_wda_a_crash_still_moves_the_device(self, monkeypatch):
        """Unchanged where there is a WDA (#669)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert ctrl._device_tree_reader(PHONE) is None


async def test_after_a_crash_with_no_wda_typing_stays_on_hid(monkeypatch, no_wda):
    """#698 sends a named field to WDA after a crash; with none, the push
    and HID carry on."""
    from tests.test_coredevice_typing import _phone

    ctrl, _ = _phone(monkeypatch, before="", reads=["a"])
    ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}
    await ctrl._no_wda_fallback(PHONE)          # what a failed read would have asked
    ctrl.wda_client.tap = AsyncMock()

    result = await ctrl.type_text("a", udid=PHONE, identifier="field_default")

    assert result["input"] == "coredevice"
    ctrl.wda_client.type_text.assert_not_called()


# -- the setting ---------------------------------------------------------------------


@pytest.mark.parametrize(("arg", "stored"), [("no-wda", "coredevice"), ("wda", "wda"),
                                             ("WDA", "wda"), ("nowda", "coredevice")])
def test_the_command_sets_it(monkeypatch, capsys, arg, stored):
    from server import __main__ as cli
    from server import config

    written = {}
    monkeypatch.setattr(config, "update_user_config",
                        lambda f: f(written))
    monkeypatch.setattr(config, "read_user_config", lambda **kw: written)

    assert cli._cmd_set_device_reader([arg]) == 0
    assert written == {"device_ui_reader": stored}
    assert "iPhone control:" in capsys.readouterr().out


def test_the_command_refuses_what_it_does_not_know(capsys):
    from server import __main__ as cli

    assert cli._cmd_set_device_reader(["maybe"]) == 2
    assert "no-wda" in capsys.readouterr().err


def test_the_command_says_when_wda_cannot_run(monkeypatch, capsys, no_wda):
    from server import __main__ as cli
    from server import config

    monkeypatch.setattr(config, "read_user_config", lambda **kw: {"device_ui_reader": "wda"})
    assert cli._cmd_set_device_reader([]) == 0
    out = capsys.readouterr().out
    assert "iPhone control: wda" in out and "cannot run here" in out



# -- a "wda" setting where WDA cannot run --------------------------------------------


class TestAWdaSettingWithNoWda:
    def test_it_is_followed_as_no_wda_and_each_read_says_so(
            self, monkeypatch):
        monkeypatch.setattr(wda_availability, "_cached", (0.0, NO_XCODE))
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)

        assert ctrl._device_tree_reader(PHONE) is ctrl.coredevice_ax
        assert ctrl._input_backend(PHONE) is ctrl.coredevice_input

    async def test_the_read_carries_the_warning(self, monkeypatch):
        monkeypatch.setattr(wda_availability, "_cached", (0.0, NO_XCODE))
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert "set to WDA" in ctrl.read_note() and "Xcode" in ctrl.read_note()
        ctrl.wda_client.describe_all.assert_not_called()

    @pytest.mark.parametrize("cached", [None, (0.0, None)])
    def test_where_wda_can_run_or_nobody_has_looked_it_is_wda(
            self, monkeypatch, cached):
        monkeypatch.setattr(wda_availability, "_cached", cached)
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)

        assert ctrl._device_tree_reader(PHONE) is None
        assert ctrl._input_backend(PHONE) is not ctrl.coredevice_input
