"""open_url across the matrix: simulators on each UI backend, a physical
iPhone, Android on both routes, and a device quern cannot place.

The default everywhere is the system's own routing, the way a tapped link
arrives, because that is the only route that tests what a user gets (#388).
The transport is chosen by the kind of device, never by the UI backend: a
simulator goes through `simctl openurl` whether sim-bridge, idb or a WDA
runner reads its screen; a physical iPhone through WDA's `/url` without a
bundle id; Android through a package-less VIEW intent carrying BROWSABLE.
`direct=true` is the Android-only opt-in for links the system will not route
to the app, delivered to the package the way Espresso does.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    UIElement,
)

SIM = "66EF8B35-4384-447E-84E8-4951BA26B181"
PHONE = "48CF8DD9-2492-5F8E-A737-49DF96422F09"
PIXEL = "R58M1234ABC"
APP = "com.example.App"
URL = "https://example.com/dl/profile"


def _app_element(label: str) -> UIElement:
    return UIElement(type="Application", label=label, identifier="",
                     frame={"x": 0, "y": 0, "width": 393, "height": 852})


def _reads(*values):
    """A front-of-screen reader that answers `values` in turn and then keeps
    answering the last one, as a screen that has settled does. An exception
    in `values` is raised for that read."""
    it = iter(values)
    last = values[-1]

    async def read(_udid):
        value = next(it, last)
        if isinstance(value, Exception):
            raise value
        return value
    return read


def _timeline(*steps):
    """A front-of-screen reader driven by the clock: `steps` are
    (seconds after the first read, value) pairs, each holding until the
    next. The first read starts the clock -- it is the before-open read."""
    import time
    t0: list[float] = []

    async def read(_udid):
        if not t0:
            t0.append(time.monotonic())
        now = time.monotonic() - t0[0]
        value = [v for at, v in steps if at <= now][-1]
        if isinstance(value, Exception):
            raise value
        return value
    return read


def _ctrl(udid: str, kind: DeviceType) -> DeviceController:
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = kind
    ctrl.resolve_udid = AsyncMock(return_value=udid)
    ctrl.simctl.open_url = AsyncMock()
    ctrl.simctl.app_display_name = AsyncMock(return_value="Example")
    ctrl.wda_client.open_url = AsyncMock()
    ctrl.wda_client.active_app = AsyncMock(return_value=APP)
    ctrl.adb.open_url = AsyncMock()
    ctrl.adb.resumed_activity = AsyncMock(return_value=(APP, f"{APP}/.MainActivity"))
    ctrl.adb.device_time = AsyncMock(return_value="1791040468.688")
    ctrl.adb.crashed_since = AsyncMock(return_value=None)
    ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 0.05
    ctrl._OPEN_URL_FRONTMOST_INTERVAL_S = 0.01
    ctrl._OPEN_URL_SETTLE_S = 0.05
    return ctrl


def _simulator_on(backend: str) -> DeviceController:
    """A simulator whose screen `backend` reads, set up the way the server
    sets it up -- and asserted, so a routing change cannot quietly turn
    three parametrized cases into one."""
    ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
    ctrl._sim_bridge_ok = backend == "sim-bridge"
    if backend == "wda":
        ctrl.wda_client.register_simulator(SIM, 8200)
    expected = {"sim-bridge": ctrl.sim_bridge, "idb": ctrl.idb, "wda": ctrl.wda_client}
    assert ctrl._ui_backend(SIM) is expected[backend]
    # A default screen, so no path through open_url can reach a real
    # backend -- including the ones only a broken guard would take.
    ctrl.get_ui_elements = AsyncMock(return_value=([_app_element("Example")], SIM))
    return ctrl


@pytest.mark.parametrize("backend", ["sim-bridge", "idb", "wda"])
class TestASimulatorOnAnyBackend:
    async def test_the_url_goes_through_simctl(self, backend):
        ctrl = _simulator_on(backend)
        udid, outcome = await ctrl.open_url(URL)
        assert udid == SIM
        ctrl.simctl.open_url.assert_awaited_once_with(SIM, URL)
        ctrl.wda_client.open_url.assert_not_awaited()
        assert outcome == {"via": "simctl", "route": "system"}, "no bundle id, nothing to confirm"

    async def test_bundle_id_is_confirmed_on_screen_not_used_to_deliver(self, backend):
        ctrl = _simulator_on(backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([_app_element("Example")], SIM))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.simctl.open_url.assert_awaited_once_with(SIM, URL)
        assert outcome == {"via": "simctl", "route": "system",
                           "opened_in_app": True, "foreground_app": "Example"}
        # Read through whichever backend serves it, never a WDA-only call.
        ctrl.wda_client.active_app.assert_not_awaited()

    async def test_a_link_that_opened_elsewhere_says_where(self, backend):
        ctrl = _simulator_on(backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([_app_element("Safari")], SIM))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is False
        assert outcome["foreground_app"] == "Safari"
        assert "apple-app-site-association" in outcome["warning"]

    async def test_direct_is_refused_rather_than_ignored(self, backend):
        """A caller who asked to bypass verification must not be told it
        worked while the system route was quietly taken."""
        ctrl = _simulator_on(backend)
        with pytest.raises(DeviceOperationUnsupportedError, match="Android-only"):
            await ctrl.open_url(URL, bundle_id=APP, direct=True)
        ctrl.simctl.open_url.assert_not_awaited()


class TestASimulatorCannotAlwaysTell:
    async def test_the_app_is_found_among_several_application_elements(self):
        ctrl = _simulator_on("sim-bridge")
        ctrl.get_ui_elements = AsyncMock(return_value=(
            [_app_element("SpringBoard"), _app_element("Example")], SIM))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_a_display_name_lookup_that_raises_does_not_stop_the_open(self):
        ctrl = _simulator_on("sim-bridge")
        ctrl.simctl.app_display_name = AsyncMock(side_effect=RuntimeError("plist"))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.simctl.open_url.assert_awaited_once_with(SIM, URL)
        assert outcome["opened_in_app"] is None
        assert "plist" in outcome["opened_in_app_error"]

    async def test_an_app_whose_name_cannot_be_read_is_unknown_not_absent(self):
        ctrl = _simulator_on("sim-bridge")
        ctrl.simctl.app_display_name = AsyncMock(return_value=None)
        ctrl.get_ui_elements = AsyncMock(return_value=([_app_element("Safari")], SIM))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is None
        assert "is it installed?" in outcome["opened_in_app_error"]

    async def test_a_screen_that_cannot_be_read_is_unknown_not_absent(self):
        ctrl = _simulator_on("idb")
        ctrl.get_ui_elements = AsyncMock(side_effect=DeviceError("idb gone", tool="idb"))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is None
        assert "idb gone" in outcome["opened_in_app_error"]


class TestAPhysicalIPhone:
    async def test_the_url_goes_through_wda_not_simctl(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        udid, outcome = await ctrl.open_url(URL)
        assert udid == PHONE
        ctrl.wda_client.open_url.assert_awaited_once_with(PHONE, URL)
        ctrl.simctl.open_url.assert_not_awaited()
        assert outcome == {"via": "wda", "route": "system"}

    async def test_direct_is_refused_rather_than_ignored(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        with pytest.raises(DeviceOperationUnsupportedError, match="Android-only"):
            await ctrl.open_url(URL, bundle_id=APP, direct=True)
        ctrl.wda_client.open_url.assert_not_awaited()

    async def test_the_app_coming_forward_is_waited_for(self):
        """The open returns before the hand-over; a single read would catch
        the app that was in front before it."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 1.0
        ctrl.wda_client.active_app = _reads(
            "com.apple.Preferences",                           # before the open
            "com.apple.Preferences", "com.apple.Preferences", APP)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome == {"via": "wda", "route": "system",
                           "opened_in_app": True, "foreground_app": APP}

    async def test_a_link_its_domains_do_not_claim_opens_in_safari_and_says_so(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.active_app = AsyncMock(return_value="com.apple.mobilesafari")
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is False
        assert outcome["foreground_app"] == "com.apple.mobilesafari"
        assert "com.apple.mobilesafari is in front" in outcome["warning"]
        assert "apple-app-site-association" in outcome["warning"]

    async def test_an_app_already_in_front_is_watched_until_the_link_would_have_left(self):
        """Measured: Safari takes the front ~1s after `/url` returns, and a
        read in that gap still sees the app. Reading early reported an
        unclaimed path as opened in it."""
        import time

        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.15
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 2.0
        ctrl.wda_client.active_app = _timeline((0, APP), (0.08, "com.apple.mobilesafari"))
        t = time.monotonic()
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is False
        assert outcome["foreground_app"] == "com.apple.mobilesafari"
        # It was there and has gone: that is the answer, with no wait for a
        # cold start that is not coming.
        assert time.monotonic() - t < 1.0

    async def test_an_app_already_in_front_that_stays_is_a_success(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_a_cold_start_slower_than_the_settle_is_waited_for(self):
        """Not in front before the open, so something else in front after
        the settle is "not yet", not "no"."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.05
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 1.0
        ctrl.wda_client.active_app = _timeline((0, "com.apple.springboard"), (0.2, APP))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_an_unreadable_front_before_the_open_is_not_taken_as_the_app(self):
        """Review: treating "could not read" as "the app was there" turned a
        slow cold start into a false no, with a warning to match."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.05
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 1.0
        ctrl.wda_client.active_app = _timeline(
            (0, DeviceError("blip", tool="wda")), (0.01, "com.apple.springboard"), (0.2, APP))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_a_before_read_that_raises_anything_does_not_stop_the_open(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.active_app = _reads(RuntimeError("bug in a reader"), APP)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.wda_client.open_url.assert_awaited_once()
        assert outcome["opened_in_app"] is True

    async def test_reads_that_fail_after_the_hand_over_cannot_decide(self):
        """A sighting from before the open must not stand in for the reads
        that failed after it."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 0.2
        ctrl.wda_client.active_app = _reads(APP, DeviceError("WDA gone", tool="wda"))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is None
        assert "WDA gone" in outcome["opened_in_app_error"]
        assert "warning" not in outcome, "could not tell is not a failure"

    async def test_a_slow_read_cannot_hold_the_open_past_its_deadline(self):
        import asyncio
        import time

        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.05
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 0.3
        ctrl._OPEN_URL_PREREAD_TIMEOUT_S = 0.2

        async def stuck(_udid):
            await asyncio.sleep(30)
        ctrl.wda_client.active_app = stuck
        t = time.monotonic()
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert time.monotonic() - t < 2.0
        assert outcome["opened_in_app"] is None

    async def test_a_read_that_fails_then_works_still_answers(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 1.0
        ctrl.wda_client.active_app = _reads(
            "com.apple.Preferences",                           # before the open
            DeviceError("blip", tool="wda"), APP)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_reads_that_never_work_are_unknown_not_absent(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.active_app = AsyncMock(side_effect=DeviceError("WDA down", tool="wda"))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is None
        assert "WDA down" in outcome["opened_in_app_error"]

    async def test_the_screen_is_read_fresh_after_the_open(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._invalidate_ui_cache = MagicMock()
        await ctrl.open_url(URL)
        ctrl._invalidate_ui_cache.assert_called_once_with(PHONE)


class TestAndroid:
    async def test_the_default_is_a_tapped_link_not_a_package_delivery(self):
        """No package, and BROWSABLE: the intent a browser tap sends, so App
        Links verification decides -- and an activity a test can open but a
        tap cannot is caught rather than reached."""
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.adb.open_url.assert_awaited_once_with(PIXEL, URL, package=None, browsable=True)
        assert outcome["via"] == "adb" and outcome["route"] == "system"
        assert outcome["opened_in_app"] is True
        assert outcome["foreground_activity"] == f"{APP}/.MainActivity"
        ctrl.simctl.open_url.assert_not_awaited()
        ctrl.wda_client.open_url.assert_not_awaited()

    async def test_a_crash_is_read_from_the_crash_buffer_not_the_screen(self):
        """Review: a crash can leave the app's own previous activity in
        front, which reads as the link having opened. Measured on a Pixel 5
        (production `/dl/profile`): the crash buffer names the process."""
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        ctrl.adb.crashed_since = AsyncMock(return_value="java.lang.RuntimeException: boom")
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.adb.crashed_since.assert_awaited_once_with(PIXEL, APP, "1791040468.688")
        assert outcome["crashed"] is True
        assert outcome["opened_in_app"] is False, "the app in front is not the link"
        assert "crashed on the link" in outcome["warning"]
        assert "RuntimeException: boom" in outcome["warning"]

    async def test_no_crash_is_said_as_such(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["crashed"] is False and outcome["opened_in_app"] is True

    @pytest.mark.parametrize("broken", ["device_time", "crashed_since"])
    async def test_a_crash_check_that_could_not_run_is_unknown(self, broken):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        setattr(ctrl.adb, broken, AsyncMock(side_effect=DeviceError("adb hung", tool="adb")))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["crashed"] is None
        assert "adb hung" in outcome["crash_check_error"]
        assert outcome["opened_in_app"] is True, "the screen check still answers"

    async def test_only_web_links_carry_browsable(self):
        """Review: an intent with a category reaches only filters declaring
        it, and the Contacts viewer for `content://` URIs does not."""
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        await ctrl.open_url("content://com.android.contacts/contacts/1")
        ctrl.adb.open_url.assert_awaited_once_with(
            PIXEL, "content://com.android.contacts/contacts/1", package=None, browsable=False)

    async def test_a_link_that_went_to_the_browser_points_at_direct(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        chrome = ("com.android.chrome", "com.android.chrome/.IntentDispatcher")
        ctrl.adb.resumed_activity = AsyncMock(return_value=chrome)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["foreground_app"] == "com.android.chrome"
        assert "pass direct=true" in outcome["warning"]

    async def test_a_direct_delivery_that_did_not_stay_does_not_suggest_direct(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        chrome = ("com.android.chrome", "com.android.chrome/.IntentDispatcher")
        ctrl.adb.resumed_activity = AsyncMock(return_value=chrome)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP, direct=True)
        assert "direct=true" not in outcome["warning"], "it was already direct"
        assert "handed the link on" in outcome["warning"]

    async def test_direct_delivers_to_the_package_as_espresso_does(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP, direct=True)
        ctrl.adb.open_url.assert_awaited_once_with(PIXEL, URL, package=APP, browsable=False)
        assert outcome["route"] == "direct" and outcome["opened_in_app"] is True

    async def test_direct_without_a_package_is_refused(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        with pytest.raises(DeviceOperationUnsupportedError, match="needs bundle_id"):
            await ctrl.open_url(URL, direct=True)
        ctrl.adb.open_url.assert_not_awaited()

    async def test_no_bundle_id_still_takes_the_system_route_and_confirms_nothing(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, outcome = await ctrl.open_url(URL)
        ctrl.adb.open_url.assert_awaited_once_with(PIXEL, URL, package=None, browsable=True)
        assert outcome == {"via": "adb", "route": "system"}
        ctrl.adb.resumed_activity.assert_not_awaited()

    async def test_the_chooser_is_named_not_reported_as_another_app(self):
        """Measured on a Pixel 5: a production `/dl/` path claimed by two
        activities in one app shows Android's chooser to a direct delivery."""
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        ctrl.adb.resumed_activity = _reads(
            ("com.android.launcher", "com.android.launcher/.Launcher"),    # before
            ("android", "android/com.android.internal.app.ResolverActivity"))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP, direct=True)
        assert outcome["opened_in_app"] is False
        assert "app chooser" in outcome["warning"]
        assert "more than one activity" in outcome["warning"]

    async def test_nothing_resumed_is_seen_as_nothing_in_front(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        ctrl.adb.resumed_activity = AsyncMock(return_value=None)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is False
        assert outcome["foreground_app"] is None


class TestTheUnknown:
    async def test_a_device_quern_cannot_place_is_refused(self):
        """Neither simctl nor WDA nor adb: an unknown udid is not a simulator."""
        ctrl = _ctrl("WHO-KNOWS", DeviceType.SIMULATOR)
        ctrl._device_type_cache.clear()
        with pytest.raises(DeviceError, match="only supported on simulators"):
            await ctrl.open_url(URL)
        ctrl.simctl.open_url.assert_not_awaited()
        ctrl.wda_client.open_url.assert_not_awaited()
        ctrl.adb.open_url.assert_not_awaited()


class TestTheAdbCalls:
    def _adb(self, stdout=""):
        from server.device.adb import AdbBackend

        adb = AdbBackend()
        adb._run_adb_for_device = AsyncMock(return_value=(stdout, ""))
        return adb

    async def test_the_system_route_sends_browsable_and_no_package(self):
        adb = self._adb("Starting: Intent { act=android.intent.action.VIEW }")
        await adb.open_url(PIXEL, URL, browsable=True)
        args = adb._run_adb_for_device.call_args.args
        assert args == (PIXEL, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                        "-c", "android.intent.category.BROWSABLE", "-d", URL)

    async def test_the_direct_route_is_the_espresso_intent(self):
        """`setPackage`, no BROWSABLE -- what the app's DeepLinkTest sends."""
        adb = self._adb("Starting: Intent { act=android.intent.action.VIEW }")
        await adb.open_url(PIXEL, URL, package=APP)
        args = adb._run_adb_for_device.call_args.args
        assert args == (PIXEL, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                        "-d", URL, APP)

    async def test_a_tap_nothing_accepts_says_how_to_deliver_it_anyway(self):
        adb = self._adb("Error: Activity not started, unable to resolve Intent { ... }")
        with pytest.raises(DeviceError, match="direct=true"):
            await adb.open_url(PIXEL, URL, browsable=True)

    async def test_an_unresolved_direct_delivery_does_not_suggest_direct(self):
        adb = self._adb("Error: Activity not started, unable to resolve Intent { ... }")
        with pytest.raises(DeviceError) as err:
            await adb.open_url(PIXEL, URL, package=APP)
        assert "direct=true" not in str(err.value)

    @pytest.mark.parametrize("line,expected", [
        # Measured, Pixel 5 (Android 14).
        ("    topResumedActivity=ActivityRecord{1951b44 u0 com.example.App/"
         "com.example.App.main.MainActivity t1755}",
         ("com.example.App", "com.example.App/com.example.App.main.MainActivity")),
        # Measured, API 32 emulator: a relative activity name is expanded.
        ("      topResumedActivity=ActivityRecord{2d3e7b9 u0 "
         "com.google.android.apps.nexuslauncher/.NexusLauncherActivity t180}",
         ("com.google.android.apps.nexuslauncher",
          "com.google.android.apps.nexuslauncher/"
          "com.google.android.apps.nexuslauncher.NexusLauncherActivity")),
        ("  mResumedActivity: ActivityRecord{7 u0 android/"
         "com.android.internal.app.ResolverActivity t9}",
         ("android", "android/com.android.internal.app.ResolverActivity")),
    ])
    async def test_the_resumed_activity_is_read(self, line, expected):
        adb = self._adb("ACTIVITY MANAGER ACTIVITIES\n" + line + "\n")
        assert await adb.resumed_activity(PIXEL) == expected

    async def test_the_system_wide_answer_beats_a_per_task_line(self):
        """Review: with split screen, a task's `mResumedActivity` can come
        first in the dump; `topResumedActivity` is the one that answers."""
        adb = self._adb(
            "  mResumedActivity: ActivityRecord{1 u0 com.other.split/.Pane t2}\n"
            "  topResumedActivity=ActivityRecord{2 u0 com.example.App/.Main t3}\n")
        assert await adb.resumed_activity(PIXEL) == ("com.example.App",
                                                     "com.example.App/com.example.App.Main")

    async def test_reads_are_bounded(self):
        adb = self._adb("")
        await adb.resumed_activity(PIXEL)
        assert adb._run_adb_for_device.call_args.kwargs["timeout"] is not None

    async def test_the_device_clock_is_read_to_the_millisecond(self):
        adb = self._adb("1791040468.688123456\n")
        assert await adb.device_time(PIXEL) == "1791040468.688"

    async def test_an_unreadable_device_clock_raises(self):
        with pytest.raises(DeviceError, match="clock"):
            await self._adb("date: bad format\n").device_time(PIXEL)

    # Measured on a Pixel 5 (Android 14), `logcat -b crash -d -v epoch -T <t>`.
    _CRASH = (
        "--------- beginning of crash\n"
        "         1791040468.874 24251 24251 E AndroidRuntime: FATAL EXCEPTION: main\n"
        "         1791040468.874 24251 24251 E AndroidRuntime: Process: "
        "com.example.App, PID: 24251\n"
        "         1791040468.874 24251 24251 E AndroidRuntime: java.lang.RuntimeException: "
        "Unable to start activity ComponentInfo{com.example.App/.LinkAccountActivity}\n"
        "         1791040468.874 24251 24251 E AndroidRuntime: \tat android.app."
        "ActivityThread.performLaunchActivity(ActivityThread.java:3782)\n")

    async def test_a_crash_of_the_app_is_found_with_its_exception(self):
        adb = self._adb(self._CRASH)
        found = await adb.crashed_since(PIXEL, "com.example.App", "1791040468.688")
        assert found.startswith("java.lang.RuntimeException: Unable to start activity")
        args = adb._run_adb_for_device.call_args.args
        assert args == (PIXEL, "logcat", "-b", "crash", "-d", "-v", "epoch",
                        "-T", "1791040468.688")
        assert "-c" not in args, "the crash buffer is the user's: read, never cleared"

    async def test_another_apps_crash_is_not_this_ones(self):
        adb = self._adb(self._CRASH)
        assert await adb.crashed_since(PIXEL, "com.example.App.other", "1") is None
        assert await adb.crashed_since(PIXEL, "com.example", "1") is None

    async def test_an_empty_crash_buffer_is_no_crash(self):
        assert await self._adb("").crashed_since(PIXEL, "com.example.App", "1") is None

    async def test_no_resumed_activity_is_none(self):
        assert await self._adb("ACTIVITY MANAGER ACTIVITIES\n").resumed_activity(PIXEL) is None


class TestTheWdaCalls:
    def _backend(self, response_json=None, *, raises=None):
        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        resp = MagicMock()
        if raises is not None:
            resp.json = MagicMock(side_effect=raises)
        else:
            resp.json = MagicMock(return_value=response_json)
        wda._request = AsyncMock(return_value=resp)
        return wda

    async def test_open_url_withholds_the_bundle_id(self):
        """With `bundleId` WDA delivers to the app directly, past the
        apple-app-site-association check -- measured to do nothing for a
        link the app handles only as a universal link."""
        wda = self._backend({"value": None})
        await wda.open_url(PHONE, URL)
        args, kwargs = wda._request.call_args
        assert args[:3] == ("post", PHONE, "/url")
        assert kwargs["json"] == {"url": URL}
        assert kwargs["use_session"] is True

    async def test_a_timed_out_open_is_not_sent_twice(self):
        """Review: `_request` reconnects and re-sends on a timeout, and WDA
        may already have opened the URL."""
        import httpx

        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        wda._request = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(DeviceError, match="may have opened anyway"):
            await wda.open_url(PHONE, URL)
        assert wda._request.await_count == 1
        assert wda._request.call_args.kwargs["raise_on_timeout"] is True

    async def test_a_slow_front_read_is_a_short_error_not_a_reconnect(self):
        import httpx

        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        wda._request = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(DeviceError, match="did not answer"):
            await wda.active_app(PHONE)
        kwargs = wda._request.call_args.kwargs
        assert kwargs["raise_on_timeout"] is True and kwargs["timeout"] <= 5

    async def test_an_answer_of_the_wrong_shape_raises(self):
        wda = self._backend(["not", "an", "object"])
        with pytest.raises(DeviceError, match="unreadable"):
            await wda.active_app(PHONE)

    async def test_active_app_names_the_bundle(self):
        wda = self._backend({"value": {"bundleId": APP, "pid": 1, "name": ""}})
        assert await wda.active_app(PHONE) == APP

    @pytest.mark.parametrize("payload", [{"value": {}}, {"value": None},
                                         {"value": {"bundleId": ""}}, {}])
    async def test_an_answer_naming_nothing_is_none(self, payload):
        assert await self._backend(payload).active_app(PHONE) is None

    async def test_an_unreadable_answer_raises(self):
        wda = self._backend(raises=ValueError("not json"))
        with pytest.raises(DeviceError, match="unreadable"):
            await wda.active_app(PHONE)


class TestTheResponse:
    @pytest.fixture
    def app(self):
        from server.config import ServerConfig
        from server.main import create_app

        app = create_app(config=ServerConfig(api_key="k"),
                         enable_oslog=False, enable_crash=False, enable_proxy=False)
        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        app.state.device_controller = ctrl
        return app

    async def _post(self, app, body, *, outcome=None, raises=None):
        from httpx import ASGITransport, AsyncClient

        ctrl = app.state.device_controller
        ctrl.open_url = (AsyncMock(side_effect=raises) if raises
                         else AsyncMock(return_value=(PHONE, outcome)))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/v1/device/open-url", json={"url": URL, **body},
                                headers={"Authorization": "Bearer k"})

    async def test_direct_reaches_the_controller(self, app):
        r = await self._post(app, {"bundle_id": APP, "direct": True},
                             outcome={"via": "adb", "route": "direct"})
        assert r.status_code == 200, r.text
        app.state.device_controller.open_url.assert_awaited_once_with(
            url=URL, udid=None, bundle_id=APP, direct=True)

    @pytest.mark.parametrize("outcome", [
        {"via": "wda", "route": "system", "opened_in_app": False,
         "foreground_app": "com.apple.mobilesafari", "warning": "went to Safari"},
        {"via": "wda", "route": "system", "opened_in_app": True, "foreground_app": APP},
        {"via": "wda", "route": "system", "opened_in_app": None, "opened_in_app_error": "x"},
        {"via": "simctl", "route": "system"},
    ])
    async def test_the_outcome_is_returned_as_it_was_found(self, app, outcome):
        r = await self._post(app, {"bundle_id": APP}, outcome=outcome)
        assert r.status_code == 200, r.text
        body = r.json()
        assert {k: body[k] for k in outcome} == outcome
        assert ("warning" in body) == ("warning" in outcome)

    async def test_a_refused_direct_is_a_400_not_a_500(self, app):
        r = await self._post(app, {"bundle_id": APP, "direct": True},
                             raises=DeviceOperationUnsupportedError("direct=true is Android-only",
                                                                    tool="wda"))
        assert r.status_code == 400
        assert "Android-only" in r.json()["detail"]
