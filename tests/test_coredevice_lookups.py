"""Element lookups from a physical iPhone's tree push (#664, phase 2b).

`tap_element`, `wait_for_element` and `get_element_state` read the push when
it serves the device. Its rows carry VoiceOver's merged labels -- "Wi-Fi,
ExampleNetwork" where WDA has a "Wi-Fi" button -- so an exact `label` also
matches a row's title, the text before its first ", ", and says so. The push
is believed both ways: a miss is a miss, named as the push's.

The tree is the real Settings capture the phase 1 tests use.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.device.controller_ui import PUSHED_ABSENT_NOTE, PUSHED_MISS_DETAIL
from server.device.ios.coredevice_ax import AppExitedDuringRead, CoreDeviceAxReader
from server.device.ios.wda_client import ACTION_SNAPSHOT_DEPTH
from server.device.ui_elements import find_element, parse_elements
from server.models import DeviceError, UIElement, WaitCondition
from tests.test_coredevice_ax import PHONE, SETTINGS, _phone_controller, parse_push

ROWS = parse_elements(parse_push(SETTINGS)[1])
SPRINGBOARD = [
    {"type": "Application", "AXLabel": "SpringBoard", "AXUniqueId": None,
     "frame": {"x": 0, "y": 0, "width": 414, "height": 896}},
    {"type": "Button", "AXLabel": "Settings", "AXUniqueId": "Settings",
     "frame": {"x": 20, "y": 100, "width": 60, "height": 60}},
]
#: A WDA tree with a label that looks merged: WDA's labels are matched exactly.
WDA_ROW = [
    {"type": "Application", "AXLabel": "Settings", "AXUniqueId": None,
     "frame": {"x": 0, "y": 0, "width": 414, "height": 896}},
    {"type": "Button", "AXLabel": "Wi-Fi, Home", "AXUniqueId": "wifi",
     "frame": {"x": 20, "y": 370, "width": 374, "height": 52}},
]


def _wda_depth_spies(ctrl):
    """WDA's depth calls, faked and recorded: a None depth is the bug."""
    ctrl.wda_client._set_snapshot_depth = AsyncMock()
    ctrl._wda_direct_query = AsyncMock(return_value=([], 0.1))
    ctrl._deeper_query = AsyncMock(return_value=([], 0.1))


@pytest.fixture(autouse=True)
def no_real_device(monkeypatch):
    """A miss takes a screenshot and, through WDA, resolves and starts the
    device: none of it may reach the machine's real phones."""
    monkeypatch.setattr("server.device.controller_ui._capture_screenshot",
                        AsyncMock(return_value=None))
    monkeypatch.setattr("server.device.controller.DeviceController.resolve_udid",
                        AsyncMock(return_value=PHONE))


class TestTheTitleRule:
    def test_a_rows_title_finds_it(self):
        found = find_element(ROWS, label="Wi-Fi", merged_labels=True)
        assert [e.identifier for e in found] == ["com.apple.settings.wifi"]

    def test_only_for_merged_labels(self):
        """WDA's and the simulators' labels are matched exactly, as before."""
        assert find_element(ROWS, label="Wi-Fi") == []

    @pytest.mark.parametrize("label", [
        "ExampleNetwork",                          # the row's detail, not its title
        "the App Store",                           # a fragment of a subtitle
        "Sign in to access your iCloud data",
    ])
    def test_not_any_part(self, label):
        assert find_element(ROWS, label=label, merged_labels=True) == []

    def test_case_insensitive_like_the_exact_match(self):
        assert find_element(ROWS, label="bluetooth", merged_labels=True)[0].identifier == \
            "com.apple.settings.bluetooth"

    def test_an_exact_match_wins_over_a_title(self):
        exact = ROWS[0].model_copy(update={"label": "Wi-Fi", "identifier": "exact"})
        found = find_element([*ROWS, exact], label="Wi-Fi", merged_labels=True)
        assert [e.identifier for e in found] == ["exact"]

    def test_a_type_filter_reaches_the_row_past_a_stray_exact_text(self):
        """A section header "Wi-Fi" must not hide the row a Button asks for."""
        header = ROWS[0].model_copy(update={"label": "Wi-Fi", "type": "StaticText",
                                             "identifier": "header"})
        found = find_element([*ROWS, header], label="Wi-Fi", element_type="Button",
                             merged_labels=True)
        assert [e.identifier for e in found] == ["com.apple.settings.wifi"]

    def test_the_whole_label_still_matches(self):
        found = find_element(ROWS, label="Wi-Fi, ExampleNetwork", merged_labels=True)
        assert [e.identifier for e in found] == ["com.apple.settings.wifi"]


class TestGetElementState:
    async def test_found_by_title_from_the_push(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        result, _ = await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        assert result["identifier"] == "com.apple.settings.wifi"
        assert result["matched_via"] == "merged_label"
        ctrl.coredevice_ax.describe_all.assert_awaited_once()
        ctrl.wda_client.find_elements_by_query.assert_not_called()
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_an_identifier_match_says_nothing_about_labels(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        result, _ = await ctrl.get_element(identifier="com.apple.settings.general", udid=PHONE)
        assert result["label"] == "General" and "matched_via" not in result

    async def test_a_miss_is_the_pushs_and_asks_wda_nothing(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        with pytest.raises(DeviceError) as caught:
            await ctrl.get_element(label="Nowhere", udid=PHONE)

        assert PUSHED_MISS_DETAIL.lstrip(". ") in str(caught.value)
        ctrl.wda_client.find_elements_by_query.assert_not_called()
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_a_depth_asks_for_wdas_read(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.wda_client._set_snapshot_depth = AsyncMock()

        with pytest.raises(DeviceError) as caught:
            await ctrl.get_element(label="Wi-Fi", udid=PHONE, snapshot_depth=25)

        ctrl.coredevice_ax.describe_all.assert_not_called()
        assert "tree the device pushes" not in str(caught.value)

    async def test_a_push_that_cannot_be_read_falls_back_with_exact_labels(self, monkeypatch):
        """WDA's read is matched exactly, as it always was: no title rule, and
        the miss is not called the push's. At the action depth, never the
        full walk, and every WDA depth step gets a number."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("no tunnel", tool="coredevice"))
        ctrl.wda_client.describe_all = AsyncMock(return_value=WDA_ROW)

        with pytest.raises(DeviceError) as caught:
            await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        assert ctrl.wda_client.describe_all.await_args.kwargs["snapshot_depth"] == \
            ACTION_SNAPSHOT_DEPTH
        assert all(c.args[1] is not None
                   for c in ctrl.wda_client._set_snapshot_depth.await_args_list)
        assert "tree the device pushes" not in str(caught.value)

    async def test_a_pushed_screen_read_answers_the_next_lookup(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        # Its own task, as its own request would be: what it records about
        # its reader must not be what makes the lookup's answer right.
        await asyncio.create_task(ctrl.get_ui_elements(PHONE, whole_screen=True))

        result, _ = await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        assert result["matched_via"] == "merged_label"
        ctrl.coredevice_ax.describe_all.assert_awaited_once()


class TestACrashDuringALookup:
    """#669: the push read kills the app. A lookup must not then answer from
    the screen the crash left -- SpringBoard."""

    @pytest.fixture
    def crashing(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=AppExitedDuringRead("Settings", 711))
        ctrl.wda_client.describe_all = AsyncMock(return_value=SPRINGBOARD)
        ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
        return ctrl

    def _says_so(self, ctrl, error):
        assert "Nothing was looked up" in str(error)
        assert ctrl._push_crashed_apps[PHONE] == {"Settings"}
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_tap_element_taps_nothing(self, crashing):
        with pytest.raises(DeviceError) as caught:
            await crashing.tap_element(label="Settings", udid=PHONE, scroll_to_find=False)
        self._says_so(crashing, caught.value)
        crashing.coredevice_input.tap.assert_not_called()

    async def test_not_exists_does_not_pass(self, crashing):
        with pytest.raises(DeviceError) as caught:
            await crashing.wait_for_element(
                condition=WaitCondition.NOT_EXISTS, label="Wi-Fi", timeout=1, udid=PHONE)
        self._says_so(crashing, caught.value)

    async def test_get_element_state_says_why(self, crashing):
        with pytest.raises(DeviceError) as caught:
            await crashing.get_element(label="Wi-Fi", udid=PHONE)
        self._says_so(crashing, caught.value)


class TestWaitForElement:
    async def test_exists_by_title(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.EXISTS, label="Wi-Fi", timeout=1, udid=PHONE)

        assert result["matched"] is True
        assert result["matched_via"] == "merged_label"
        ctrl.wda_client.find_elements_by_query.assert_not_called()

    async def test_not_exists_does_not_pass_on_a_row_that_is_there(self, monkeypatch):
        """Phase 1's adversarial review: with exact labels over the push,
        not_exists on "Wi-Fi" reported success with the row on screen."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.NOT_EXISTS, label="Wi-Fi", timeout=0.3,
            interval=0.1, udid=PHONE)

        assert result["matched"] is False

    async def test_a_pushed_absence_says_whose(self, monkeypatch):
        """The network name is on screen, merged into the Wi-Fi row: absent as
        VoiceOver sees it, which the success has to say."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.NOT_EXISTS, label="ExampleNetwork", timeout=1, udid=PHONE)

        assert result["matched"] is True and result["note"] == PUSHED_ABSENT_NOTE

    async def test_a_found_element_that_times_out_names_the_push(self, monkeypatch):
        """Bluetooth's state is in its merged label, not its value."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._identify_for_miss = AsyncMock(return_value={})

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.VALUE_EQUALS, label="Bluetooth", value="On",
            timeout=0.3, interval=0.1, udid=PHONE)

        assert result["matched"] is False
        assert "tree the device pushes" in result["depth_note"]

    async def test_each_poll_is_matched_by_its_own_reader(self, monkeypatch):
        """A poll WDA made after the push failed matches exactly: WDA's
        "Wi-Fi, Home" is not the "Wi-Fi" the push's title rule found."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=[
            parse_push(SETTINGS)[1], DeviceError("no tunnel", tool="coredevice")])
        ctrl.wda_client.describe_all = AsyncMock(return_value=WDA_ROW)

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.NOT_EXISTS, label="Wi-Fi", timeout=3,
            interval=0.35, udid=PHONE)

        assert result["matched"] is True and result["polls"] == 2
        assert "note" not in result                  # WDA's absence, not the push's

    async def test_through_wda_a_merged_looking_label_is_matched_exactly(self, monkeypatch):
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl.wda_client.describe_all = AsyncMock(return_value=WDA_ROW)

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.NOT_EXISTS, label="Wi-Fi", timeout=1, udid=PHONE)

        assert result["matched"] is True and "note" not in result

    async def test_a_never_found_element_names_the_push(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._identify_for_miss = AsyncMock(return_value={})

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.EXISTS, label="Nowhere", timeout=0.3,
            interval=0.1, udid=PHONE)

        assert result["matched"] is False
        assert "tree the device pushes" in result["depth_note"]
        assert "snapshot_depth" not in result
        ctrl.wda_client.describe_all.assert_not_called()      # nor for its context


class TestTapElement:
    async def test_a_row_tapped_by_title_over_hid_with_no_wda(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
        ctrl.wda_client.is_hittable = AsyncMock(return_value=True)
        ctrl.wda_client.tap = AsyncMock()

        result = await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

        assert result["status"] == "ok", result
        assert result["tapped"]["identifier"] == "com.apple.settings.wifi"
        assert result["matched_via"] == "merged_label"
        ctrl.coredevice_input.tap.assert_awaited_once()
        ctrl.wda_client.is_hittable.assert_not_called()      # the push is the reachable set
        ctrl.wda_client.tap.assert_not_called()
        # One read: the push reader answers a settled screen, so there is no
        # second read to see whether the row moved.
        ctrl.coredevice_ax.describe_all.assert_awaited_once()

    async def test_a_miss_is_the_pushs(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._identify_for_miss = AsyncMock(return_value={})

        result = await ctrl.tap_element(label="Nowhere", udid=PHONE, scroll_to_find=False)

        assert result["status"] == "not_found"
        assert result["detail"].endswith(PUSHED_MISS_DETAIL)
        assert "snapshot_depth" not in result

    async def test_a_miss_takes_its_screen_context_from_the_push(self, monkeypatch):
        """Measured live: a WDA read for the context cost seconds per miss and
        made the answer's backend say wda for a lookup the push made."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._identify_for_miss = AsyncMock(return_value={})

        result = await ctrl.tap_element(label="Nowhere", udid=PHONE)   # sweep unset

        assert result["status"] == "not_found"
        ctrl.wda_client.describe_all.assert_not_called()
        ctrl.wda_client.find_elements_by_query.assert_not_called()
        assert ctrl.backend_that_served(PHONE) == CoreDeviceAxReader.TOOL_NAME

    async def test_through_wda_the_label_is_exact(self, monkeypatch):
        """Not opted in: the same lookup is WDA's, matched exactly -- its
        "Wi-Fi, Home" row is not found by "Wi-Fi"."""
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl._identify_for_miss = AsyncMock(return_value={})
        ctrl.wda_client.describe_all = AsyncMock(return_value=WDA_ROW)
        ctrl.wda_client.tap = AsyncMock()

        result = await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

        assert result["status"] == "not_found"
        ctrl.coredevice_ax.describe_all.assert_not_called()
        ctrl.wda_client.tap.assert_not_called()

    async def test_after_the_wda_sweep_the_stability_check_runs(self, monkeypatch):
        """The sweep finds the target through WDA after a swipe, while the list
        may still be moving: the push's 'settled' reason no longer holds."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl._identify_for_miss = AsyncMock(return_value={})
        ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
        found = UIElement(type="Button", label="Far", identifier="far",
                          frame={"x": 20, "y": 400, "width": 374, "height": 52})
        ctrl._ios_scroll_to_element = AsyncMock(return_value=found)
        ctrl._wda_direct_query = AsyncMock(return_value=([found], 0.1))

        result = await ctrl.tap_element(label="Far", udid=PHONE, scroll_to_find=True)

        assert result["status"] == "ok", result
        # The re-check: a WDA read of the target by its label. (The other
        # direct query here is the reachability check's, for the viewport.)
        assert any(c.kwargs.get("label") == "Far"
                   for c in ctrl._wda_direct_query.await_args_list)


class TestOtherReadsStayOnWda:
    async def test_a_filtered_read_without_the_lookup_flag_is_wdas(self, monkeypatch):
        """type_text's and clear_text's field lookups, and the sweep, name no
        flag and stay with WDA, as the user chose for phase 2a."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.wda_client._set_snapshot_depth = AsyncMock()

        await ctrl.get_ui_elements(PHONE, filter_label="General", snapshot_depth=12)
        await ctrl.get_ui_elements(PHONE, filter_label="General", use_cache=False)

        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_an_uncached_pushed_lookup_uses_the_title_rule(self, monkeypatch):
        """Not the exact filter applied while parsing, which a read that skips
        the cache otherwise uses."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        elements, _ = await ctrl.get_ui_elements(
            PHONE, filter_label="Wi-Fi", use_cache=False, lookup=True)
        assert [e.identifier for e in elements] == ["com.apple.settings.wifi"]

    async def test_a_wda_filtered_read_after_a_pushed_one_stays_exact(self, monkeypatch):
        """In the same request -- a sweep after a pushed lookup -- WDA's read is
        WDA's, matched exactly. Through the cached path, whose in-memory filter
        is the one a pushed read changes."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        await ctrl.get_element(label="Wi-Fi", udid=PHONE)              # pushed
        ctrl._invalidate_ui_cache(PHONE)
        ctrl.wda_client.describe_all = AsyncMock(return_value=WDA_ROW)

        elements, _ = await ctrl.get_ui_elements(PHONE, filter_label="Wi-Fi")

        assert elements == []
        ctrl.wda_client.describe_all.assert_awaited()

    async def test_a_wda_query_after_a_pushed_read_is_marked_as_wdas(self, monkeypatch):
        """Its own record of the reader, not the request's last one nor the
        shared per-device one a concurrent push read rewrites meanwhile."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_element(label="Wi-Fi", udid=PHONE)              # pushed
        general = UIElement(type="Button", label="General", identifier="general",
                            frame={"x": 20, "y": 660, "width": 374, "height": 52})

        async def query_while_a_push_lands(*a, **kw):
            ctrl._last_read_backend[PHONE] = CoreDeviceAxReader.TOOL_NAME
            return [general], 0.1

        ctrl._wda_direct_query = AsyncMock(side_effect=query_while_a_push_lands)
        ctrl.wda_client._set_snapshot_depth = AsyncMock()

        elements, _ = await ctrl.get_ui_elements(PHONE, filter_label="General",
                                                 snapshot_depth=12)

        assert elements == [general]
        ctrl._mark_on_screen.assert_awaited()

    async def test_the_reader_is_recorded_per_request(self, monkeypatch):
        """A WDA read that rewrites the shared per-device record mid-lookup must
        not make the lookup's pushed matches read as WDA's."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        pushed = parse_push(SETTINGS)[1]

        async def read_while_wda_answers(*a, **kw):
            ctrl._last_read_backend[PHONE] = ctrl.wda_client.TOOL_NAME
            return pushed

        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=read_while_wda_answers)

        result, _ = await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        assert result["matched_via"] == "merged_label"
        assert ctrl._ui_cache_backend[PHONE] == CoreDeviceAxReader.TOOL_NAME
