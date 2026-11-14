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

from server.device.controller_ui import PUSHED_MISS_DETAIL
from server.device.ios.coredevice_ax import CoreDeviceAxReader
from server.device.ui_elements import find_element, parse_elements
from server.models import DeviceError, WaitCondition
from tests.test_coredevice_ax import PHONE, SETTINGS, _phone_controller, parse_push

ROWS = parse_elements(parse_push(SETTINGS)[1])


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
        the miss is not called the push's."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("no tunnel", tool="coredevice"))
        ctrl.wda_client.describe_all = AsyncMock(return_value=parse_push(SETTINGS)[1])

        with pytest.raises(DeviceError) as caught:
            await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        ctrl.wda_client.describe_all.assert_awaited()
        assert "tree the device pushes" not in str(caught.value)

    async def test_a_pushed_screen_read_answers_the_next_lookup(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        # Its own task, as its own request would be: what it records about
        # its reader must not be what makes the lookup's answer right.
        await asyncio.create_task(ctrl.get_ui_elements(PHONE, whole_screen=True))

        result, _ = await ctrl.get_element(label="Wi-Fi", udid=PHONE)

        assert result["matched_via"] == "merged_label"
        ctrl.coredevice_ax.describe_all.assert_awaited_once()


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

    async def test_a_never_found_element_names_the_push(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._identify_for_miss = AsyncMock(return_value={})

        result, _ = await ctrl.wait_for_element(
            condition=WaitCondition.EXISTS, label="Nowhere", timeout=0.3,
            interval=0.1, udid=PHONE)

        assert result["matched"] is False
        assert "tree the device pushes" in result["depth_note"]
        assert "snapshot_depth" not in result


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

    async def test_through_wda_the_label_is_exact(self, monkeypatch):
        """Not opted in: the same lookup is WDA's, matched exactly."""
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)
        ctrl.wda_client._set_snapshot_depth = AsyncMock()

        await ctrl.tap_element(label="Wi-Fi", udid=PHONE, scroll_to_find=False)

        ctrl.coredevice_ax.describe_all.assert_not_called()
        ctrl.wda_client.find_elements_by_query.assert_awaited()


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
