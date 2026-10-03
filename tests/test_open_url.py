"""open_url across the matrix: simulators on each UI backend, a physical
iPhone, Android, and a device quern cannot place.

The route is chosen by the kind of device, never by the UI backend: opening a
URL is the system's routing, so a simulator goes through `simctl openurl`
whether sim-bridge, idb or a WDA runner reads its screen, and a physical
iPhone goes through WDA's `/url` -- without a bundle id, because with one WDA
hands the URL to the app directly and skips the universal-link check a deep
link test exists to exercise (#388).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.models import DeviceError, DeviceType, UIElement

SIM = "66EF8B35-4384-447E-84E8-4951BA26B181"
PHONE = "48CF8DD9-2492-5F8E-A737-49DF96422F09"
PIXEL = "R58M1234ABC"
APP = "com.example.App"
URL = "https://example.com/dl/profile"


def _app_element(label: str) -> UIElement:
    return UIElement(type="Application", label=label, identifier="",
                     frame={"x": 0, "y": 0, "width": 393, "height": 852})


def _ctrl(udid: str, kind: DeviceType) -> DeviceController:
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = kind
    ctrl.resolve_udid = AsyncMock(return_value=udid)
    ctrl.simctl.open_url = AsyncMock()
    ctrl.simctl.app_display_name = AsyncMock(return_value="Example")
    ctrl.wda_client.open_url = AsyncMock()
    ctrl.wda_client.active_app = AsyncMock(return_value=APP)
    ctrl.adb.open_url = AsyncMock()
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
    return ctrl


@pytest.mark.parametrize("backend", ["sim-bridge", "idb", "wda"])
class TestASimulatorOnAnyBackend:
    async def test_the_url_goes_through_simctl(self, backend):
        ctrl = _simulator_on(backend)
        udid, outcome = await ctrl.open_url(URL)
        assert udid == SIM
        ctrl.simctl.open_url.assert_awaited_once_with(SIM, URL)
        ctrl.wda_client.open_url.assert_not_awaited()
        assert outcome == {"via": "simctl"}, "no bundle id, nothing to confirm"

    async def test_bundle_id_is_confirmed_on_screen_not_used_to_deliver(self, backend):
        ctrl = _simulator_on(backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([_app_element("Example")], SIM))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.simctl.open_url.assert_awaited_once_with(SIM, URL)
        assert outcome == {"via": "simctl", "opened_in_app": True, "foreground_app": "Example"}
        # Read through whichever backend serves it, never a WDA-only call.
        ctrl.wda_client.active_app.assert_not_awaited()

    async def test_a_link_that_opened_elsewhere_says_where(self, backend):
        ctrl = _simulator_on(backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([_app_element("Safari")], SIM))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is False
        assert outcome["foreground_app"] == "Safari"


class TestASimulatorCannotAlwaysTell:
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
        assert outcome == {"via": "wda"}

    async def test_the_app_coming_forward_is_waited_for(self):
        """The open returns before the hand-over; a single read would catch
        the app that was in front before it."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 1.0
        ctrl.wda_client.active_app = AsyncMock(
            side_effect=["com.apple.Preferences",              # before the open
                         "com.apple.Preferences", "com.apple.Preferences", APP])
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome == {"via": "wda", "opened_in_app": True, "foreground_app": APP}

    async def test_a_link_its_domains_do_not_claim_opens_in_safari_and_says_so(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.active_app = AsyncMock(return_value="com.apple.mobilesafari")
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome == {"via": "wda", "opened_in_app": False,
                           "foreground_app": "com.apple.mobilesafari"}

    async def test_an_app_already_in_front_is_watched_until_the_link_would_have_left(self):
        """Measured: Safari takes the front ~1s after `/url` returns, and the
        first read in that gap still sees the app. Reading once reported an
        unclaimed path as opened in it."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.1
        reads = iter([APP, APP, APP])          # before, then twice inside the gap

        async def front(udid):
            return next(reads, "com.apple.mobilesafari")
        ctrl.wda_client.active_app = front
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome == {"via": "wda", "opened_in_app": False,
                           "foreground_app": "com.apple.mobilesafari"}

    async def test_an_app_already_in_front_that_stays_is_a_success(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_an_unreadable_front_before_the_open_is_watched_too(self):
        """Not knowing what was in front is treated as "maybe the app": the
        cautious side, which costs the settle time rather than a false yes."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.1
        reads = iter([DeviceError("blip", tool="wda"), APP, APP])

        async def front(udid):
            r = next(reads, "com.apple.mobilesafari")
            if isinstance(r, Exception):
                raise r
            return r
        ctrl.wda_client.active_app = front
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is False

    async def test_reads_that_fail_after_the_hand_over_cannot_decide(self):
        """A sighting from inside the gap is the one that cannot be trusted,
        so it must not stand in for the reads that failed after it."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_SETTLE_S = 0.1
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 0.2
        reads = iter([APP, APP])

        async def front(udid):
            r = next(reads, None)
            if r is None:
                raise DeviceError("WDA gone", tool="wda")
            return r
        ctrl.wda_client.active_app = front
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is None
        assert "WDA gone" in outcome["opened_in_app_error"]

    async def test_a_read_that_fails_then_works_still_answers(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._OPEN_URL_FRONTMOST_TIMEOUT_S = 1.0
        ctrl.wda_client.active_app = AsyncMock(
            side_effect=["com.apple.Preferences",              # before the open
                         DeviceError("blip", tool="wda"), APP])
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is True

    async def test_reads_that_never_work_are_unknown_not_absent(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.active_app = AsyncMock(side_effect=DeviceError("WDA down", tool="wda"))
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        assert outcome["opened_in_app"] is None
        assert "WDA down" in outcome["opened_in_app_error"]


class TestAndroidAndTheUnknown:
    async def test_android_targets_the_package_and_confirms_nothing(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, outcome = await ctrl.open_url(URL, bundle_id=APP)
        ctrl.adb.open_url.assert_awaited_once_with(PIXEL, URL, package=APP)
        assert outcome == {"via": "adb"}
        ctrl.simctl.open_url.assert_not_awaited()
        ctrl.wda_client.open_url.assert_not_awaited()

    async def test_a_device_quern_cannot_place_is_refused(self):
        """Neither simctl nor WDA: an unknown udid is not a simulator."""
        ctrl = _ctrl("WHO-KNOWS", DeviceType.SIMULATOR)
        ctrl._device_type_cache.clear()
        with pytest.raises(DeviceError, match="only supported on simulators"):
            await ctrl.open_url(URL)
        ctrl.simctl.open_url.assert_not_awaited()
        ctrl.wda_client.open_url.assert_not_awaited()


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

    async def _post(self, app, outcome):
        from httpx import ASGITransport, AsyncClient

        app.state.device_controller.open_url = AsyncMock(return_value=(PHONE, outcome))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/device/open-url", json={"url": URL, "bundle_id": APP},
                             headers={"Authorization": "Bearer k"})
        assert r.status_code == 200, r.text
        return r.json()

    async def test_opened_elsewhere_warns_and_names_where(self, app):
        body = await self._post(app, {"via": "wda", "opened_in_app": False,
                                      "foreground_app": "com.apple.mobilesafari"})
        assert body["opened_in_app"] is False and body["via"] == "wda"
        assert "com.apple.mobilesafari is in front" in body["warning"]
        assert "apple-app-site-association" in body["warning"]

    @pytest.mark.parametrize("outcome", [
        {"via": "wda", "opened_in_app": True, "foreground_app": APP},
        {"via": "wda", "opened_in_app": None, "opened_in_app_error": "x"},
        {"via": "simctl"},
    ])
    async def test_no_warning_unless_it_opened_elsewhere(self, app, outcome):
        """"Could not tell" is reported as such, not as a failure."""
        body = await self._post(app, outcome)
        assert "warning" not in body
        assert {k: body[k] for k in outcome} == outcome
