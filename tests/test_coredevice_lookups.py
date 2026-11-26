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

    def test_prefiltering_keeps_both_kinds_of_candidate(self):
        """A read narrowed before the caller's own match keeps exact and title
        candidates alike: the related-type prefilter keeps an exact Cell that
        the final rule then drops, and the row must still be there for it."""
        cell = ROWS[0].model_copy(update={"label": "Wi-Fi", "type": "Cell",
                                           "identifier": "cell"})
        kept = find_element([*ROWS, cell], label="Wi-Fi", element_type="Button",
                            merged_labels=True, prefilter=True)
        assert {e.identifier for e in kept} >= {"cell", "com.apple.settings.wifi"}
        final = find_element(kept, label="Wi-Fi", element_type="Button", merged_labels=True)
        assert [e.identifier for e in final] == ["com.apple.settings.wifi"]

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

    @pytest.mark.usefixtures("wda_fallback_on")
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


class TestAPushThatCannotBeRead:
    """A lookup whose push read fails takes the WDA path any lookup takes,
    including its deeper look on a miss (CodeRabbit on #672)."""

    @pytest.mark.usefixtures("wda_fallback_on")
    async def test_wait_for_element_looks_deeper_through_wda(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("no tunnel", tool="coredevice"))
        deep = UIElement(type="Button", label="Deep", identifier="deep",
                         frame={"x": 20, "y": 400, "width": 374, "height": 52})
        ctrl._deeper_query = AsyncMock(return_value=([deep], 0.2))

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.EXISTS, label="Deep", timeout=1, udid=PHONE)

        assert result["matched"] is True and result["element"]["identifier"] == "deep"
        assert ctrl._wda_direct_query.await_args_list            # WDA's filtered query
        ctrl._deeper_query.assert_awaited()

    @pytest.mark.usefixtures("wda_fallback_on")
    async def test_tap_element_looks_deeper_through_wda(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("no tunnel", tool="coredevice"))
        ctrl._deeper_query = AsyncMock(return_value=([], 0.2))
        ctrl._identify_for_miss = AsyncMock(return_value={})

        await ctrl.tap_element(label="Deep", udid=PHONE, scroll_to_find=False)

        ctrl._deeper_query.assert_awaited()


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

    @pytest.mark.usefixtures("wda_fallback_on")
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
        # One read: the push reader answers a settled screen, so there is no
        # second read to see whether the row moved.
        ctrl.coredevice_ax.describe_all.assert_awaited_once()

    @staticmethod
    def _hit(label, x, y, w, h, kind="StaticText", identifier=None):
        return {"type": kind, "AXLabel": label, "AXUniqueId": identifier,
                "frame": {"x": x, "y": y, "width": w, "height": h}}

    def _covered(self, monkeypatch, hit):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
        ctrl.wda_client.tap = AsyncMock()
        ctrl.coredevice_ax.describe_point = AsyncMock(return_value=hit)
        return ctrl

    async def test_a_covered_row_is_refused_with_what_covers_it(self, monkeypatch):
        """The phone's hit-test answers with the deepest element: an overlay's
        title, inside the target's frame, is not the target (#664)."""
        wifi = next(e for e in parse_push(SETTINGS)[1]
                    if e["AXUniqueId"] == "com.apple.settings.wifi")
        f = wifi["frame"]
        ctrl = self._covered(monkeypatch, self._hit(
            "Overlay", f["x"] + 40, f["y"] + 4, 60, f["height"] - 8))
        # The overlay is on the pushed screen too, smaller than the row, as the
        # probe's is: its title is the overlay's, not the row's.
        overlay = {"type": "Button", "AXLabel": "Overlay", "AXUniqueId": "overlay",
                   "AXValue": None, "enabled": True, "role": "AXButton",
                   "role_description": "", "help": None, "custom_actions": [],
                   "frame": {"x": f["x"] + 30, "y": f["y"] + 2,
                             "width": f["width"] - 60, "height": f["height"] - 4}}
        ctrl.coredevice_ax.describe_all = AsyncMock(
            return_value=[*parse_push(SETTINGS)[1], overlay])

        result = await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

        assert result["status"] == "obstructed", result
        assert result["covered_by"]["label"] == "Overlay"
        ctrl.coredevice_input.tap.assert_not_called()
        ctrl.wda_client.tap.assert_not_called()

    async def test_its_own_text_or_detail_is_the_target(self, monkeypatch):
        """A pushed row's label is all its texts: its title and its detail
        both say part of it."""
        wifi = next(e for e in parse_push(SETTINGS)[1]
                    if e["AXUniqueId"] == "com.apple.settings.wifi")
        f = wifi["frame"]
        for text in ("Wi-Fi", wifi["AXLabel"].split(", ")[-1]):
            ctrl = self._covered(monkeypatch, self._hit(
                text, f["x"] + 40, f["y"] + 4, 60, f["height"] - 8))

            result = await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

            assert result["status"] == "ok", (text, result)
            ctrl.coredevice_input.tap.assert_awaited_once()

    async def test_a_text_of_the_rows_own_that_its_label_lacks_still_taps(self, monkeypatch):
        """Whose a hit is comes from the pushed screen the lookup cached: a
        title the label does not carry is still the row's own (CodeRabbit on
        #705)."""
        wifi = next(e for e in parse_push(SETTINGS)[1]
                    if e["AXUniqueId"] == "com.apple.settings.wifi")
        f = wifi["frame"]
        ctrl = self._covered(monkeypatch, self._hit(
            "Networks", f["x"] + 40, f["y"] + 4, 60, f["height"] - 8))

        result = await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

        assert result["status"] == "ok", result
        ctrl.coredevice_input.tap.assert_awaited_once()

    async def test_a_hit_test_that_cannot_tell_taps(self, monkeypatch):
        ctrl = self._covered(monkeypatch, None)

        result = await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

        assert result["status"] == "ok", result
        ctrl.coredevice_ax.describe_point.assert_awaited_once()

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

    async def test_a_pushed_miss_sweeps_through_the_push(self, monkeypatch):
        """The sweep reads and swipes over CoreDevice too (#678), so the pushed
        miss is its own kind of read and is not taken again."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl._identify_for_miss = AsyncMock(return_value={})
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)

        await ctrl.tap_element(label="Nowhere", udid=PHONE, scroll_to_find=True)

        kwargs = ctrl._ios_scroll_to_element.await_args.kwargs
        assert kwargs["pushed"] is True
        assert kwargs["target_known_absent"] is True

    async def test_after_a_pushed_sweep_nothing_asks_wda(self, monkeypatch):
        """The sweep read through the push too (#678): what it found is the
        push's settled read, so no WDA re-check or reachability query follows
        -- on a phone with no WDA either would fail or start one."""
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
        ctrl._wda_direct_query.assert_not_called()
        ctrl.coredevice_input.tap.assert_awaited_once()


class TestPushedSweeps:
    """#678 and its review: what decides a sweep is pushed, and what it keeps."""

    async def test_a_row_the_sweep_found_by_its_title_is_tapped(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl._identify_for_miss = AsyncMock(return_value={})
        ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
        row = UIElement(type="Button", label="Far, Detail", identifier=None,
                        frame={"x": 20, "y": 400, "width": 374, "height": 52})
        ctrl._ios_scroll_to_element = AsyncMock(return_value=row)

        result = await ctrl.tap_element(label="Far", udid=PHONE, scroll_to_find=True)

        assert result["status"] == "ok", result
        assert "merged_label" in result.get("matched_via", "")

    @pytest.mark.parametrize(("depth", "pushed"), [(None, True), (12, False)])
    async def test_scroll_to_element_sweeps_the_push_unless_given_a_depth(
            self, monkeypatch, depth, pushed):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)

        result = await ctrl.scroll_to_element(udid=PHONE, label="Far", snapshot_depth=depth)

        assert ctrl._ios_scroll_to_element.await_args.kwargs["pushed"] is pushed
        if pushed:
            # Not "read through WDA at snapshot_depth": the sweep read the push.
            assert "tree the device pushes" in result["detail"]
            assert "snapshot_depth" not in result

    @pytest.mark.usefixtures("wda_fallback_on")
    async def test_a_device_the_push_crashed_an_app_on_sweeps_through_wda(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        _wda_depth_spies(ctrl)
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)

        await ctrl.scroll_to_element(udid=PHONE, label="Far")

        assert ctrl._ios_scroll_to_element.await_args.kwargs["pushed"] is False

    async def test_scroll_into_view_swipes_over_hid_and_looks_up_in_the_push(
            self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        low = UIElement(type="Button", label="Far, Detail", identifier=None,
                        frame={"x": 20, "y": 860, "width": 374, "height": 52})
        high = low.model_copy(update={"frame": {**low.frame, "y": 700}})
        reads = []

        async def get_ui_elements(*a, **k):
            reads.append(k)
            return ([low] if len(reads) == 1 else [high]), PHONE
        ctrl.get_ui_elements = get_ui_elements
        ctrl.coredevice_input.swipe = AsyncMock(return_value="coredevice")
        ctrl.wda_client.swipe = AsyncMock()

        found = await ctrl._scroll_element_into_view(
            PHONE, "Far", None, None, None, None, 896, 414, pushed=True)

        assert found is not None and found.frame["y"] == 700
        ctrl.coredevice_input.swipe.assert_awaited_once()
        ctrl.wda_client.swipe.assert_not_called()
        assert all(r.get("lookup") is True for r in reads)
        # 100 pt at the HID sweep speed, held at the end.
        args, kwargs = ctrl.coredevice_input.swipe.await_args
        assert args[5] == pytest.approx(100 / 250) and kwargs["hold"] == 0.5


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
        remember = ctrl._remember_screen

        def wda_answers_meanwhile(udid, raw):
            # After the read has named its reader, before the lookup reads its
            # answer: where a concurrent WDA read's record lands.
            remember(udid, raw)
            ctrl._last_read_backend[PHONE] = ctrl.wda_client.TOOL_NAME

        ctrl._remember_screen = wda_answers_meanwhile

        result, _ = await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        assert result["matched_via"] == "merged_label"
        assert ctrl._ui_cache_backend[PHONE] == CoreDeviceAxReader.TOOL_NAME
