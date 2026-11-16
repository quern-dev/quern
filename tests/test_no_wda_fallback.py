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

NO_XCODE = wda_availability.NO_XCODE

#: The real cached check, taken before conftest's fixture replaces the name.
REAL_REASON = wda_availability.wda_unavailable_reason


@pytest.fixture
def no_wda(monkeypatch):
    async def missing():
        return NO_XCODE
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", missing)
    # As the startup check leaves it, for the routing that asks synchronously.
    monkeypatch.setattr(wda_availability, "_cached", (wda_availability.time.monotonic(), NO_XCODE))


# -- the check ---------------------------------------------------------------------


class TestTheCheck:
    def _patch(self, monkeypatch, *, xcode=True, built=True, raises=None):
        def full_xcode():
            if raises:
                raise raises
            return xcode
        monkeypatch.setattr(wda_availability, "_full_xcode", full_xcode)

        def xctestrun():
            if not built:
                raise WdaNotSetUpError("no .xctestrun", tool="wda")
            return "x.xctestrun"
        monkeypatch.setattr("server.device.ios.wda._find_xctestrun", xctestrun)

    @pytest.mark.parametrize(("kw", "expect"), [
        ({}, None),
        ({"xcode": False}, "Xcode is not installed"),
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

    def test_xcode_is_an_xcodebuild_in_the_developer_directory(self, monkeypatch, tmp_path):
        clt = tmp_path / "CommandLineTools"
        (clt / "usr" / "bin").mkdir(parents=True)
        monkeypatch.setenv("DEVELOPER_DIR", str(clt))
        assert not wda_availability._full_xcode()          # the Command Line Tools

        tool = clt / "usr" / "bin" / "xcodebuild"
        tool.write_text("")
        tool.chmod(0o755)
        assert wda_availability._full_xcode()

    def test_developer_dir_may_name_the_app(self, monkeypatch, tmp_path):
        app = tmp_path / "Xcode-beta.app"
        tool = app / "Contents" / "Developer" / "usr" / "bin" / "xcodebuild"
        tool.parent.mkdir(parents=True)
        tool.write_text("")
        tool.chmod(0o755)
        monkeypatch.setenv("DEVELOPER_DIR", f"{app}/")
        assert wda_availability._full_xcode()

    async def test_a_stale_answer_is_asked_again_in_the_background(self, monkeypatch):
        """Xcode installed, or WDA built another way, while the server runs."""
        check = AsyncMock(return_value=None)
        monkeypatch.setattr(wda_availability, "_check", check)
        monkeypatch.setattr(wda_availability, "_refreshing", None)
        clock = [1000.0]
        monkeypatch.setattr(wda_availability.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(wda_availability, "_cached", (clock[0], NO_XCODE))
        # conftest stands in for the cached check; the real one is wanted here.
        monkeypatch.setattr(wda_availability, "wda_unavailable_reason", REAL_REASON)

        assert wda_availability.known_reason() == NO_XCODE
        check.assert_not_awaited()                        # still fresh
        clock[0] += wda_availability._TTL_S
        assert wda_availability.known_reason() == NO_XCODE  # as it stands
        await wda_availability._refreshing
        assert wda_availability.known_reason() is None
        assert check.await_count == 1

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

    with pytest.raises(WdaNotSetUpError, match="Xcode is not installed") as caught:
        await client._get_base_url(PHONE)
    start.assert_not_called()
    assert "earlier one needs Xcode" in str(caught.value)


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
    # A phone on iOS 27 or later needs no Xcode; an earlier one does (#706).
    assert "iPhone control: wda" in out and "driven without it" in out
    assert "earlier ones need Xcode" in out


def test_the_command_says_when_wda_is_not_built(monkeypatch, capsys):
    from server import __main__ as cli
    from server import config

    async def not_built():
        return wda_availability.NOT_BUILT
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", not_built)
    monkeypatch.setattr(config, "read_user_config", lambda **kw: {"device_ui_reader": "wda"})
    assert cli._cmd_set_device_reader([]) == 0
    out = capsys.readouterr().out
    assert "cannot run here" in out and "driven without it" in out



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


def test_without_xcode_wda_cannot_be_chosen(monkeypatch, capsys, no_wda):
    from server import __main__ as cli
    from server import config

    written = {}
    monkeypatch.setattr(config, "update_user_config", lambda f: f(written))
    assert cli._cmd_set_device_reader(["wda"]) == 1
    assert written == {}
    assert "Install Xcode" in capsys.readouterr().err


def test_where_wda_is_only_unbuilt_it_can_be_chosen(monkeypatch, capsys):
    """setup_wda can build it, so the choice stands."""
    from server import __main__ as cli
    from server import config

    async def not_built():
        return wda_availability.NOT_BUILT
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", not_built)
    written = {}
    monkeypatch.setattr(config, "update_user_config", lambda f: f(written))
    monkeypatch.setattr(config, "read_user_config", lambda **kw: written)
    assert cli._cmd_set_device_reader(["wda"]) == 0
    assert written == {"device_ui_reader": "wda"}



# -- crashes, counted ------------------------------------------------------------------


class TestASecondCrashOfTheSameApp:
    """With no WDA a device stays on the push after a crash, so the same app
    can crash again under an action's own read; the set of crashed apps
    does not change then, and the count does (CodeRabbit on #707)."""

    async def test_clearing_refuses(self, monkeypatch, no_wda):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._record_push_crash(PHONE, "QuernProbeScene")       # an earlier one
        crash = coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711)
        field = [{"type": "TextField", "AXLabel": "Email", "AXUniqueId": "email",
                  "frame": {"x": 10, "y": 100, "width": 300, "height": 40}}]
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=[crash, field])

        with pytest.raises(DeviceError, match="Nothing was cleared.*stays on the"):
            await ctrl.clear_text(udid=PHONE, identifier="email")


class TestAfterSetup:
    """setup_wda marks the answer stale whatever happened (CodeRabbit on #707)."""

    @pytest.fixture
    def crashed(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._record_push_crash(PHONE, "QuernProbeScene")
        monkeypatch.setattr(wda_availability, "_refreshing", None)
        monkeypatch.setattr(wda_availability, "wda_unavailable_reason", REAL_REASON)
        monkeypatch.setattr(wda_availability, "_cached",
                            (wda_availability.time.monotonic(), wda_availability.NOT_BUILT))
        return ctrl

    async def test_a_failed_setup_keeps_the_device_on_the_push(self, monkeypatch, crashed):
        monkeypatch.setattr(wda_availability, "_check",
                            AsyncMock(return_value=wda_availability.NOT_BUILT))
        wda_availability.forget()
        # Before the re-check answers, and after it.
        assert crashed._device_tree_reader(PHONE) is crashed.coredevice_ax
        await wda_availability._refreshing
        assert crashed._device_tree_reader(PHONE) is crashed.coredevice_ax

    async def test_a_wda_built_brings_the_latch_back(self, monkeypatch, crashed):
        monkeypatch.setattr(wda_availability, "_check", AsyncMock(return_value=None))
        wda_availability.forget()
        crashed._device_tree_reader(PHONE)                    # asks again, in the background
        await wda_availability._refreshing
        assert crashed._device_tree_reader(PHONE) is None
