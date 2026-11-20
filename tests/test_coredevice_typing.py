"""Typing on a physical iPhone driven over CoreDevice, with no WDA (#664).

Keys go over a virtual HID keyboard, so the app sees real keystrokes; the phone
decodes them with its own keyboard and input method, which nothing reports, so
a named field is read back and set through the accessibility service when what
arrived is not what was meant. Measured on an iPhone 11 with a Japanese
keyboard: "Hello abc #@" arrived as "Helloあbc＃＠", and the set put it right.
"""

from __future__ import annotations

import plistlib
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.controller_ui import _keyboard_correction
from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import front_app, parse_tree, set_value, set_value_request
from server.models import DeviceError, DeviceType, UIElement
from tests.test_coredevice_ax import (  # noqa: F401  (phone_address is a fixture)
    SAFARI,
    SETTINGS,
    _as,
    _fake_lib,
    phone_address,
)

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
PROBE_FIELDS = (Path(__file__).resolve().parent / "fixtures" / "coredevice_ax"
                / "probe_text_fields.bin").read_bytes()
FRAME = {"x": 20.0, "y": 120.0, "width": 374.0, "height": 40.0}


def _field(value: str, *, type_: str = "TextField", identifier: str | None = "field_default"):
    return UIElement(type=type_, label="", identifier=identifier, value=value, frame=FRAME)


def _phone(monkeypatch, *, before: str, reads: list[str | None], secure: bool = False):
    """A phone the push serves, with a field holding `before` and the values
    read back after each step in `reads`."""
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    ctrl.wda_client._device_os_versions[PHONE] = "iOS 27.2.1"
    monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader",
                        lambda: "coredevice")
    monkeypatch.setattr("server.device.controller_ui.asyncio.sleep", AsyncMock())
    target = _field(before, type_="SecureTextField" if secure else "TextField")
    ctrl._find_text_field = AsyncMock(return_value=target)
    ctrl._read_field_value = AsyncMock(side_effect=reads)
    ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
    ctrl.coredevice_input.type_text = AsyncMock(return_value="coredevice")
    ctrl.wda_client.type_text = AsyncMock()
    setter = AsyncMock()
    monkeypatch.setattr("server.device.controller_ui.coredevice_ax.set_value", setter)
    return ctrl, setter


class TestTypingIntoAField:
    async def test_text_that_arrives_is_left_as_typed(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="field_default", reads=["Hello abc #@"])

        result = await ctrl.type_text("Hello abc #@", udid=PHONE, identifier="field_default")

        ctrl.coredevice_input.type_text.assert_awaited_once_with(PHONE, "Hello abc #@")
        setter.assert_not_called()
        assert result == {"udid": PHONE, "verified": True, "input": "coredevice",
                          "value": "Hello abc #@"}
        ctrl.wda_client.type_text.assert_not_called()

    async def test_both_reads_are_the_push(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=["a"])
        await ctrl.type_text("a", udid=PHONE, identifier="field_default")
        assert ctrl._find_text_field.await_args.kwargs["whole_screen"] is True
        assert ctrl._read_field_value.await_args.kwargs["whole_screen"] is True

    async def test_text_the_keyboard_changed_is_set_and_says_so(self, monkeypatch):
        """The Japanese keyboard's own result, on an empty field whose
        placeholder read as its value."""
        ctrl, setter = _phone(monkeypatch, before="field_default",
                              reads=["Helloあbc＃＠", "Hello abc #@"])

        result = await ctrl.type_text("Hello abc #@", udid=PHONE, identifier="field_default")

        assert setter.await_args.args[2] == "Hello abc #@"
        assert result["value"] == "Hello abc #@"
        assert result["set_through_accessibility"] is True
        assert "editing callbacks" in result["note"]

    async def test_the_set_keeps_what_the_field_held(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="abc", reads=["abcXあ", "abcXa"])

        await ctrl.type_text("Xa", udid=PHONE, identifier="field_default")

        assert setter.await_args.args[2] == "abcXa"

    async def test_the_set_finds_the_field_by_every_name_it_has(self, monkeypatch):
        """An identifier a reusable cell's fields share is not enough on its own:
        the text would go to whichever came first (#682 review)."""
        ctrl, setter = _phone(monkeypatch, before="", reads=["あ", "a"])
        ctrl._find_text_field.return_value = UIElement(
            type="TextField", label="Last name", identifier="textField", value="", frame=FRAME)
        await ctrl.type_text("a", udid=PHONE, label="Last name")

        same_field = setter.await_args.args[1]
        assert same_field({"type": "TextField", "AXUniqueId": "textField", "AXLabel": "Last name"})
        assert not same_field({"type": "TextField", "AXUniqueId": "textField",
                               "AXLabel": "First name"})
        assert not same_field({"type": "Group", "AXUniqueId": "textField",
                               "AXLabel": "Last name"})

    async def test_a_field_is_not_found_by_a_frame_the_keyboard_may_move(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=["あ", "a"])
        ctrl._find_text_field.return_value = UIElement(
            type="TextField", label="Name", identifier=None, value="", frame=FRAME)
        await ctrl.type_text("a", udid=PHONE, label="Name")

        same_field = setter.await_args.args[1]
        assert same_field({"type": "TextField", "AXLabel": "Name",
                           "frame": {**FRAME, "y": 40.0}})

    async def test_text_no_us_key_types_is_set(self, monkeypatch):
        """A space is typed so the read-back shows where the field's text
        sits, and the whole text is then set."""
        ctrl, setter = _phone(monkeypatch, before="abc", reads=["abc ", "abc日本"])

        result = await ctrl.type_text("日本", udid=PHONE, identifier="field_default")

        ctrl.coredevice_input.type_text.assert_awaited_once_with(PHONE, " ")
        assert setter.await_args.args[2] == "abc日本"
        assert result["value"] == "abc日本"

    async def test_the_us_keys_of_mixed_text_are_typed(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=["caf", "café"])
        await ctrl.type_text("café", udid=PHONE, identifier="field_default")
        ctrl.coredevice_input.type_text.assert_awaited_once_with(PHONE, "caf")
        assert setter.await_args.args[2] == "café"

    async def test_a_password_field_is_never_set(self, monkeypatch):
        """Its dots could not confirm what was set."""
        ctrl, setter = _phone(monkeypatch, before="", reads=["••"], secure=True)

        with pytest.raises(DeviceError):
            await ctrl.type_text("abc", udid=PHONE, identifier="field_default")

        setter.assert_not_called()

    async def test_a_password_is_typed_whole_even_without_us_keys(self, monkeypatch):
        """Through WDA, by way of the HID backend's own fallback, rather than
        half typed and set."""
        ctrl, _ = _phone(monkeypatch, before="", reads=["••"], secure=True)
        await ctrl.type_text("é1", udid=PHONE, identifier="field_default")
        ctrl.coredevice_input.type_text.assert_awaited_once_with(PHONE, "é1")

    async def test_nothing_arriving_is_not_set(self, monkeypatch):
        """The tap may not have taken focus: where the text belongs is unknown."""
        ctrl, setter = _phone(monkeypatch, before="abc", reads=["abc"])

        with pytest.raises(DeviceError, match="did not change"):
            await ctrl.type_text("x", udid=PHONE, identifier="field_default")

        setter.assert_not_called()

    async def test_a_set_that_does_not_take_is_an_error(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=["あ", "あ"])

        with pytest.raises(DeviceError):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        setter.assert_awaited_once()

    async def test_an_unreadable_field_is_not_set(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=[None])
        with pytest.raises(DeviceError, match="could not read"):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")
        setter.assert_not_called()

    async def test_without_a_field_it_types_and_says_unverified(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=[])

        result = await ctrl.type_text("abc", udid=PHONE)

        assert result["verified"] is False and result["input"] == "coredevice"
        ctrl.coredevice_input.type_text.assert_awaited_once_with(PHONE, "abc")
        setter.assert_not_called()

    async def test_a_wda_depth_stays_with_wda(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        await ctrl.type_text("abc", udid=PHONE, snapshot_depth=12)
        ctrl.wda_client.type_text.assert_awaited_once_with(PHONE, "abc")
        ctrl.coredevice_input.type_text.assert_not_called()


class TestWhatIsNotSet:
    """Only what the phone's keyboard did is set: anything else is the app's or
    iOS's own change, which a set would overwrite and report as success (#682
    review)."""

    @pytest.mark.parametrize(("before", "after", "text"), [
        ("teh", "the cat", " cat"),           # autocorrect rewrote an earlier word
        ("(555) 12", "(555) 123-4", "34"),    # an input mask
        ("", "12", "12a"),                    # a validator dropped a character
    ])
    async def test_an_app_s_own_change_is_an_error(self, monkeypatch, before, after, text):
        ctrl, setter = _phone(monkeypatch, before=before, reads=[after])

        with pytest.raises(DeviceError):
            await ctrl.type_text(text, udid=PHONE, identifier="field_default")

        setter.assert_not_called()

    async def test_not_after_the_push_crashed_an_app(self, monkeypatch):
        """The set reads the push, which would run the crashing dump again
        (#669): such a device types into a named field through WDA, which
        inserts the text, and the answer says why."""
        ctrl, _ = _phone(monkeypatch, before="", reads=["a"])
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}
        ctrl.wda_client.tap = AsyncMock()

        result = await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        ctrl.wda_client.tap.assert_awaited_once()
        ctrl.wda_client.type_text.assert_awaited_once()
        ctrl.coredevice_input.tap.assert_not_called()
        ctrl.coredevice_input.type_text.assert_not_called()
        assert "input" not in result
        assert "QuernProbeScene" in result["note"] and "#669" in result["note"]

    async def test_a_crash_in_its_own_read_types_nothing(self, monkeypatch):
        """The field read fell back to WDA on the screen the crash left, so a
        tap there taps something else (#698 review)."""
        ctrl, setter = _phone(monkeypatch, before="", reads=["a"])
        target = ctrl._find_text_field.return_value

        async def crashing(*args, **kwargs):
            ctrl._record_push_crash(PHONE, "QuernProbeScene")
            return target
        ctrl._find_text_field = AsyncMock(side_effect=crashing)

        with pytest.raises(DeviceError, match="crashed QuernProbeScene.*Nothing was typed"):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        ctrl.coredevice_input.tap.assert_not_called()
        ctrl.coredevice_input.type_text.assert_not_called()
        ctrl.wda_client.type_text.assert_not_called()

    async def test_a_crash_reading_it_back_is_not_corrected_and_says_so(self, monkeypatch):
        """The correction's set reads the push again (#669); the failure names
        the crash, not the keyboard alone."""
        ctrl, setter = _phone(monkeypatch, before="", reads=[])

        async def crashing(*args, **kwargs):
            ctrl._record_push_crash(PHONE, "QuernProbeScene")
            return "あ"
        ctrl._read_field_value = AsyncMock(side_effect=crashing)

        with pytest.raises(DeviceError,
                           match="Reading the field back crashed QuernProbeScene") as err:
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        setter.assert_not_called()
        assert ".." not in str(err.value)

    @pytest.mark.parametrize("reads_as", ["a", "\u3042"])
    async def test_a_crash_reading_it_back_is_never_taken_for_the_text(self, monkeypatch, reads_as):
        """With no WDA the read back is the push's of what the crash left,
        where a field of the same name may hold the very text (CodeRabbit
        on #707): no verification either way."""
        ctrl, setter = _phone(monkeypatch, before="", reads=[])

        async def crashing(*args, **kwargs):
            ctrl._record_push_crash(PHONE, "QuernProbeScene")
            return reads_as
        ctrl._read_field_value = AsyncMock(side_effect=crashing)

        with pytest.raises(DeviceError, match="crashed QuernProbeScene"):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")
        setter.assert_not_called()

    async def test_a_crash_that_leaves_no_such_field_says_it_crashed(self, monkeypatch):
        """The likelier case: the read fell back to WDA on SpringBoard, where
        no field matches -- "no text field" alone would hide the crash (#698
        review)."""
        ctrl, _ = _phone(monkeypatch, before="", reads=["a"])

        async def crashing(*args, **kwargs):
            ctrl._record_push_crash(PHONE, "QuernProbeScene")
            raise DeviceError("No element found: no text field matching 'field_default'",
                              tool="wda")
        ctrl._find_text_field = AsyncMock(side_effect=crashing)

        with pytest.raises(DeviceError, match="crashed QuernProbeScene.*Nothing was typed"):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        ctrl.coredevice_input.tap.assert_not_called()

    async def test_a_field_that_is_not_there_is_still_not_there(self, monkeypatch):
        """With no crash, the lookup's own error stands."""
        ctrl, _ = _phone(monkeypatch, before="", reads=["a"])
        ctrl._find_text_field = AsyncMock(side_effect=DeviceError(
            "No element found: no text field matching 'field_default'", tool="wda"))

        with pytest.raises(DeviceError, match="no text field matching"):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

    async def test_a_crash_in_the_set_is_recorded(self, monkeypatch):
        """The set reads the push too: its crash puts the device on WDA, as
        every other read's does (#698 review)."""
        ctrl, setter = _phone(monkeypatch, before="", reads=["あ"])
        setter.side_effect = coredevice_ax.AppExitedDuringRead("QuernProbeScene", 123)

        with pytest.raises(DeviceError, match="crashed the app"):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        assert ctrl._push_crashed_apps[PHONE] == {"QuernProbeScene"}

    async def test_no_crash_note_when_a_depth_chose_wda(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=["a"])
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}
        ctrl.wda_client.tap = AsyncMock()

        result = await ctrl.type_text("a", udid=PHONE, identifier="field_default",
                                      snapshot_depth=30)

        assert "note" not in result

    async def test_a_failed_set_says_what_the_field_holds(self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=["Helloあ"])
        setter.side_effect = DeviceError("the field to set is not in it", tool="coredevice")

        with pytest.raises(DeviceError, match="holds 'Helloあ'.*failed"):
            await ctrl.type_text("Hello a", udid=PHONE, identifier="field_default")

    async def test_a_set_that_leaves_only_the_text_is_not_landed(self, monkeypatch):
        """`after == text` passes a typed field; after a set that asked for the
        old content too, it means the content was lost."""
        ctrl, _ = _phone(monkeypatch, before="abc", reads=["abcあ", "a"])

        with pytest.raises(DeviceError):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

    async def test_untargeted_keystrokes_say_nothing_checked_them(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        result = await ctrl.type_text("abc", udid=PHONE)
        assert "name the field" in result["note"]


class TestTheReadsTheyMake:
    """Both reads must reach the push: through WDA they would need a WDA the
    phone does not have."""

    @pytest.mark.parametrize("helper", ["_find_text_field", "_read_field_value"])
    async def test_whole_screen_reaches_the_read(self, helper):
        ctrl = DeviceController()
        ctrl.get_ui_elements = AsyncMock(return_value=([_field("x")], PHONE))
        if helper == "_find_text_field":
            await ctrl._find_text_field(PHONE, label=None, identifier="field_default",
                                        whole_screen=True)
        else:
            await ctrl._read_field_value(PHONE, _field("x"), None, "field_default",
                                         whole_screen=True)
        assert ctrl.get_ui_elements.await_args.kwargs["whole_screen"] is True


class TestClearing:
    """clear_text over CoreDevice (#664): found in the push, cleared with
    keystrokes over HID."""

    def _ctrl(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        field = _field("hello")
        ctrl.get_ui_elements = AsyncMock(return_value=([field], PHONE))
        ctrl.coredevice_input.select_all_and_delete = AsyncMock(return_value="coredevice")
        ctrl.wda_client.select_all_and_delete = AsyncMock()
        return ctrl

    async def test_it_reads_the_push_and_clears_over_hid(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch)

        await ctrl.clear_text(udid=PHONE, identifier="field_default")

        assert ctrl.get_ui_elements.await_args.kwargs["whole_screen"] is True
        ctrl.coredevice_input.select_all_and_delete.assert_awaited_once()
        ctrl.wda_client.select_all_and_delete.assert_not_called()

    async def test_a_crash_in_its_own_read_clears_nothing(self, monkeypatch):
        """Without a selector the first field of the screen the crash left
        would be emptied (#698 review)."""
        ctrl = self._ctrl(monkeypatch)
        found = ctrl.get_ui_elements.return_value

        async def crashing(*args, **kwargs):
            ctrl._record_push_crash(PHONE, "QuernProbeScene")
            return found
        ctrl.get_ui_elements = AsyncMock(side_effect=crashing)

        with pytest.raises(DeviceError, match="Nothing was cleared"):
            await ctrl.clear_text(udid=PHONE)

        ctrl.coredevice_input.select_all_and_delete.assert_not_called()
        ctrl.wda_client.select_all_and_delete.assert_not_called()

    async def test_a_wda_depth_stays_with_wda(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch)

        await ctrl.clear_text(udid=PHONE, identifier="field_default", snapshot_depth=12)

        assert ctrl.get_ui_elements.await_args.kwargs["whole_screen"] is False
        ctrl.wda_client.select_all_and_delete.assert_awaited_once()
        ctrl.coredevice_input.select_all_and_delete.assert_not_called()


class TestTheCorrection:
    @pytest.mark.parametrize(("before", "after", "keys", "text", "expected"), [
        # The keyboard's doing: set.
        ("field_default", "Helloあbc＃＠", "Hello abc #@", "Hello abc #@", "Hello abc #@"),
        ("Search", "Sうし", "Sushi", "Sushi", "Sushi"),      # a placeholder sharing a letter
        ("abc", "abcXあ", "Xa", "Xa", "abcXa"),
        ("", "あ", "a", "a", "a"),
        # Typed only in part, on purpose: set.
        ("abc", "abc ", " ", "日本", "abc日本"),
        ("field_default", " ", " ", "日本", "日本"),
        ("", "caf", "caf", "café", "café"),
        # Not the keyboard: not set.
        ("teh", "the cat", " cat", " cat", None),           # autocorrect
        ("(555) 12", "(555) 123-4", "34", "34", None),      # a mask
        ("", "12", "12a", "12a", None),                     # a validator
        ("abc", "abcX", "Y", "Y", None),                    # plain ASCII mismatch
        # Where the keys went is ambiguous: not set.
        ("ab", "aあab", "ac", "ac", None),
    ])
    def test_it(self, before, after, keys, text, expected):
        assert _keyboard_correction(before, after, keys, text) == expected


class TestTheRequest:
    def test_it_is_the_archive_apple_s_translator_writes(self):
        archive = plistlib.loads(set_value_request(1051, 15101179698132549287, "é 日本"))
        objects = archive["$objects"]
        request = objects[archive["$top"]["root"].data]
        assert request["requestType"] == 8 and request["attributeType"] == 53
        params = objects[request["parameters"].data]
        keys = [objects[u.data] for u in params["NS.keys"]]
        values = [objects[u.data] for u in params["NS.objects"]]
        assert dict(zip(keys, values)) == {"value": "é 日本"}
        translation = objects[request["translation"].data]
        assert translation["pid"] == 1051

    def test_the_object_id_is_an_object_not_inline(self):
        """Inline, the device accepts the request and does nothing (measured)."""
        archive = plistlib.loads(set_value_request(1051, 15101179698132549287, "x"))
        objects = archive["$objects"]
        translation = objects[objects[1]["translation"].data]
        assert isinstance(translation["objectID"], plistlib.UID)
        assert objects[translation["objectID"].data] == 15101179698132549287

    def test_every_element_of_a_real_push_has_its_address(self):
        tree = parse_tree(SETTINGS)
        assert len(tree.refs) == len(tree.elements)
        assert all(ref is not None for ref in tree.refs)
        assert len({ref for ref in tree.refs}) == len(tree.refs)   # one each


@pytest.mark.usefixtures("phone_address")
class TestSettingAValue:
    async def test_the_request_names_the_matching_element(self, monkeypatch):
        lib, state = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        tree = parse_tree(SETTINGS)
        index = next(i for i, e in enumerate(tree.elements)
                     if e["AXUniqueId"] == "com.apple.settings.general")

        await set_value(PHONE, lambda e: e["AXUniqueId"] == "com.apple.settings.general",
                        "x", timeout=5)

        sent = [args for sel, args in state.control.calls if sel == "processDataFromHost:"]
        assert sent == [(set_value_request(*tree.refs[index], "x"),)]
        assert state.provider_closed and state.rsd_closed

    async def test_no_matching_element_sends_nothing(self, monkeypatch):
        lib, state = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="not in"):
            await set_value(PHONE, lambda e: False, "x", timeout=5)

        assert not [c for c in state.control.calls if c[0] == "processDataFromHost:"]
        assert state.provider_closed and state.rsd_closed

    async def test_several_matching_elements_are_refused(self, monkeypatch):
        """Never the first taken: the text would go to whichever came first."""
        lib, state = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="elements .* match the field"):
            await set_value(PHONE, lambda e: True, "x", timeout=5)

        assert not [c for c in state.control.calls if c[0] == "processDataFromHost:"]

    async def test_a_bare_tree_is_refused(self, monkeypatch):
        lib, state = _fake_lib([])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        bare = coredevice_ax.Tree("AdditionalData", [{"type": "Application", "AXLabel": ""}],
                                  711, [(711, 1)])

        async def newest(queue, held=None):
            return bare, bare, bare
        monkeypatch.setattr(coredevice_ax, "_newest_tree", newest)
        # No pushes reach the queue here: not a silence to resubscribe over.
        monkeypatch.setattr(coredevice_ax, "_first_push", AsyncMock(return_value=True))

        with pytest.raises(coredevice_ax.BareAppTree):
            await set_value(PHONE, lambda e: True, "x", timeout=5)
        assert not [c for c in state.control.calls if c[0] == "processDataFromHost:"]

    async def test_an_app_that_changed_under_the_read_is_refused(self, monkeypatch):
        lib, state = _fake_lib([])
        state.processes = [711, 99]          # switched, not killed by the read
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        tree = parse_tree(SETTINGS)
        first = coredevice_ax.Tree("InitialDump", tree.elements, 711, tree.refs)
        newest_tree = coredevice_ax.Tree("AdditionalData", tree.elements, 99, tree.refs)

        async def newest(queue, held=None):
            return newest_tree, first, newest_tree
        monkeypatch.setattr(coredevice_ax, "_newest_tree", newest)
        # No pushes reach the queue here: not a silence to resubscribe over.
        monkeypatch.setattr(coredevice_ax, "_first_push", AsyncMock(return_value=True))

        with pytest.raises(DeviceError, match="changed"):
            await set_value(PHONE, lambda e: e["AXUniqueId"] == "com.apple.settings.general",
                            "x", timeout=5)
        assert not [c for c in state.control.calls if c[0] == "processDataFromHost:"]

    async def test_a_refused_connection_is_a_device_error(self, monkeypatch):
        lib, _ = _fake_lib([], connect_error=ConnectionRefusedError(61, "refused"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        with pytest.raises(DeviceError, match="failed"):
            await set_value(PHONE, lambda e: True, "x", timeout=5)


class TestPasswordFieldsFromThePush:
    """The push types a password field as a plain TextField; a trait bit is
    what marks it, on a field only. Unmarked, its dots read as the keyboard's change and the
    password was set through accessibility (found live on an iPhone 11)."""

    @pytest.mark.parametrize(("etype", "traits", "expected"), [
        # The iPhone 11's password field, empty and then filled.
        ("TextField", 0x2201040000, "AXSecureTextField"),
        ("TextField", 0x2201240000, "AXSecureTextField"),
        # Its ordinary text fields.
        ("TextField", 0x2200040000, ""),
        # The bit on anything but a field marks nothing.
        ("StaticText", 0x2201040000, ""),
        ("TextField", coredevice_ax.TRAIT_SEARCH_FIELD, "AXSearchField"),
    ])
    def test_the_subrole(self, etype, traits, expected):
        assert coredevice_ax._role_description(etype, traits) == expected

    def test_a_real_push_marks_its_password_field_and_nothing_else(self):
        """The probe app's text tab, captured on an iPhone 11, fields empty."""
        _, elements = coredevice_ax.parse_push(PROBE_FIELDS)
        secure = [e["AXUniqueId"] for e in elements
                  if e["role_description"] == "AXSecureTextField"]
        assert secure == ["field_secure"]

    @pytest.mark.parametrize("push", [SAFARI, SETTINGS])
    def test_page_text_and_settings_are_never_password_fields(self, push):
        """Safari's page text carries attribute 28, which once looked like the
        marker (#690 review)."""
        _, elements = coredevice_ax.parse_push(push)
        assert not [e for e in elements if e["role_description"] == "AXSecureTextField"]

    async def test_a_marked_password_field_is_checked_by_length_and_never_set(
            self, monkeypatch):
        ctrl, setter = _phone(monkeypatch, before="", reads=["•" * 6])
        ctrl._find_text_field.return_value = UIElement(
            type="TextField", role_description="AXSecureTextField", label="",
            identifier="field_secure", value="", frame=FRAME)

        result = await ctrl.type_text("pass12", udid=PHONE, identifier="field_secure")

        assert result["verified"] is True and "value" not in result
        setter.assert_not_called()

    async def test_dots_are_never_a_keyboard_change(self, monkeypatch):
        """Even on a field the push did not mark."""
        ctrl, setter = _phone(monkeypatch, before="field_secure", reads=["•" * 6])

        with pytest.raises(DeviceError):
            await ctrl.type_text("pass12", udid=PHONE, identifier="field_secure")

        setter.assert_not_called()
class TestLaunching:
    """launch_app over CoreDevice (#664): devicectl, confirmed from the push."""

    def _ctrl(self, monkeypatch, fronts):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        ctrl.devicectl.launch_app = AsyncMock(return_value=1583)
        ctrl.wda_client.launch_app = AsyncMock()
        ctrl.wda_client.app_state = AsyncMock()
        monkeypatch.setattr("server.device.ios.coredevice_ax.front_app",
                            AsyncMock(side_effect=fronts))
        monkeypatch.setattr("server.device.controller.asyncio.sleep", AsyncMock())
        return ctrl

    async def test_it_launches_with_devicectl_and_confirms_the_pid(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [(1583, "QuernProbeScene")])

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert info == {}
        ctrl.devicectl.launch_app.assert_awaited_once_with(
            PHONE, "com.quern.probe.scene", env={"QUERN_AUTOMATION": "YES"},
            terminate_existing=False)
        ctrl.wda_client.launch_app.assert_not_called()
        ctrl.wda_client.app_state.assert_not_called()

    async def test_a_daemon_restart_during_the_check_is_said_on_the_launch(
            self, monkeypatch):
        """The check is the read the restart rescued (#734): said on its
        answer, and not left for the next read to claim."""
        from server.device.ios import coredevice_ax

        async def front(*_a, **_k):
            coredevice_ax._NOTES.get().append("axauditd had stopped answering; restarted")
            return (1583, "QuernProbeScene")

        ctrl = self._ctrl(monkeypatch, [])
        monkeypatch.setattr("server.device.ios.coredevice_ax.front_app", front)

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert "restarted" in info.get("warning", "")

    async def test_a_daemon_restart_is_said_on_a_launch_that_then_fails(self, monkeypatch):
        """Said whichever way the check ends: here the app is not in front."""
        from server.device.ios import coredevice_ax

        async def front(*_a, **_k):
            coredevice_ax._NOTES.get().append("axauditd had stopped answering; restarted")
            return (37, "SpringBoard")

        ctrl = self._ctrl(monkeypatch, [])
        monkeypatch.setattr("server.device.ios.coredevice_ax.front_app", front)
        monkeypatch.setattr(ctrl, "_PUSH_FRONT_GRACE_S", 0.0)

        with pytest.raises(DeviceError, match="restarted"):
            await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

    async def test_the_check_is_remembered_as_a_read(self, monkeypatch):
        """A push read like any other: a crash it causes after it answers is
        looked for at the next read (#669 review)."""
        ctrl = self._ctrl(monkeypatch, [(37, " "), (1583, "QuernProbeScene")])

        await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        first = ctrl.coredevice_ax._front[PHONE][0]
        assert (first.pid, first.app) == (1583, "QuernProbeScene")
        # Dated from when its read began, so a crash under it is in the window.
        assert first.when <= time.time() and time.time() - first.when < 5

    async def test_env_restarts_the_app_so_it_applies(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [(1583, "QuernProbeScene")])

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE,
                                        env={"MODE": "test"})

        kwargs = ctrl.devicectl.launch_app.await_args.kwargs
        assert kwargs == {"env": {"QUERN_AUTOMATION": "YES", "MODE": "test"},
                          "terminate_existing": True}
        # devicectl does not say whether one was running.
        assert info == {"env_applied": True, "restarted": None}

    async def test_it_waits_for_the_app_to_come_to_the_front(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [(1, " "), (1583, "QuernProbeScene")])
        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)
        assert info == {}

    async def test_an_app_that_never_comes_up_is_an_error(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [(1, " ")] * 50)
        monkeypatch.setattr(ctrl, "_PUSH_FRONT_GRACE_S", 0)

        with pytest.raises(DeviceError, match="not in the foreground: the home screen"):
            await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

    async def test_an_unreadable_screen_is_no_verdict(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [DeviceError("no tunnel", tool="coredevice")] * 50)
        monkeypatch.setattr(ctrl, "_PUSH_FRONT_GRACE_S", 0)

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert info["launch_confirmed"] is None and "no tunnel" in info["launch_check_error"]

    async def test_no_pid_is_no_verdict(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [])
        ctrl.devicectl.launch_app.return_value = 0

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert info["launch_confirmed"] is None

    async def test_a_phone_not_on_the_push_launches_through_wda(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [])
        monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: "wda")
        ctrl.wda_client.app_state = AsyncMock(return_value=4)
        ctrl.wda_client.activate_app = AsyncMock()

        await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        ctrl.devicectl.launch_app.assert_not_called()
        ctrl.wda_client.activate_app.assert_awaited_once()


class TestDevicectlLaunch:
    async def test_env_and_restart_reach_the_command(self):
        from server.device.ios.devicectl import DevicectlBackend
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(
            return_value=('{"result": {"process": {"processIdentifier": 1583}}}', ""))

        pid = await backend.launch_app(PHONE, "com.x", env={"A": "1"}, terminate_existing=True)

        assert pid == 1583
        args = backend._run_devicectl.await_args.args
        assert args[args.index("--environment-variables") + 1] == '{"A": "1"}'
        assert "--terminate-existing" in args and args[-1] == "com.x"

    async def test_no_options_by_default(self):
        from server.device.ios.devicectl import DevicectlBackend
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("{}", ""))
        await backend.launch_app(PHONE, "com.x")
        args = backend._run_devicectl.await_args.args
        assert "--environment-variables" not in args and "--terminate-existing" not in args


@pytest.mark.usefixtures("phone_address")
class TestTheAppInFront:
    async def test_its_pid_and_name(self, monkeypatch):
        lib, state = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        assert await front_app(PHONE, timeout=5) == (617, "Settings")
        assert state.provider_closed and state.rsd_closed

    async def test_an_app_the_read_killed_is_said(self, monkeypatch):
        """As read_tree says it (#669), so the caller stops reading the push."""
        lib, state = _fake_lib([_as("InitialDump", SETTINGS), SETTINGS,
                                _as("InitialDump", SAFARI), SAFARI])
        state.processes = [618]                       # Settings is gone
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(coredevice_ax.AppExitedDuringRead):
            await front_app(PHONE, timeout=5)

    async def test_a_set_that_times_out_says_it_may_have_happened(self, monkeypatch):
        lib, _ = _fake_lib([])                        # no tree ever comes
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="may or may not have been set"):
            await set_value(PHONE, lambda e: True, "x", timeout=0.3)


class TestTheLaunchCheck:
    """#689 review: the check follows #669's crash rule, is no verdict when its
    newest look failed, and is bounded by its grace."""

    def _ctrl(self, monkeypatch, fronts):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        ctrl.devicectl.launch_app = AsyncMock(return_value=1583)
        self.front = AsyncMock(side_effect=fronts)
        monkeypatch.setattr("server.device.ios.coredevice_ax.front_app", self.front)
        monkeypatch.setattr("server.device.controller.asyncio.sleep", AsyncMock())
        return ctrl

    async def test_a_device_the_push_crashed_an_app_on_is_not_read(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [])
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert info["launch_confirmed"] is None and "#669" in info["launch_check_error"]
        self.front.assert_not_called()

    async def test_a_check_that_crashed_the_app_is_recorded(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch,
                          [coredevice_ax.AppExitedDuringRead("QuernProbeScene", 1583)])

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert info["launch_confirmed"] is None
        assert "QuernProbeScene" in ctrl._push_crashed_apps[PHONE]

    async def test_a_stale_mismatch_then_a_failed_read_is_no_verdict(self, monkeypatch):
        """The home screen at the first look, then a stalled read (#684): the
        app may have come up since, so it is not called failed."""
        ctrl = self._ctrl(monkeypatch, [(1, " "), DeviceError("timed out", tool="coredevice")])
        monkeypatch.setattr("server.device.controller.time.monotonic",
                            _clock([0.0, 0.0, 3.0, 3.0, 10.0]))

        _, info = await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert info["launch_confirmed"] is None and "timed out" in info["launch_check_error"]

    async def test_a_read_with_the_grace_nearly_spent_still_gets_the_floor(
            self, monkeypatch):
        """Room for a silent subscription's fresh one to deliver (#684)."""
        ctrl = self._ctrl(monkeypatch, [(1583, "QuernProbeScene")])
        monkeypatch.setattr("server.device.controller.time.monotonic",
                            _clock([0.0, 5.9]))

        await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert self.front.await_args.kwargs["timeout"] == pytest.approx(ctrl._PUSH_READ_MIN_S)

    async def test_each_read_is_bounded_by_what_is_left(self, monkeypatch):
        ctrl = self._ctrl(monkeypatch, [(1583, "QuernProbeScene")])
        monkeypatch.setattr(ctrl, "_PUSH_READ_MIN_S", 1.0)    # the floor below the grace
        monkeypatch.setattr("server.device.controller.time.monotonic", _clock([100.0]))

        await ctrl.launch_app("com.quern.probe.scene", udid=PHONE)

        assert self.front.await_args.kwargs["timeout"] == pytest.approx(ctrl._PUSH_FRONT_GRACE_S)


def _clock(times: list[float]):
    """A monotonic clock that reads `times` in turn, then holds the last."""
    left = list(times)

    def now() -> float:
        return left.pop(0) if len(left) > 1 else left[0]
    return now



def test_a_launch_read_has_room_for_a_fresh_subscription():
    """At 4 s, a launch whose first subscription stalled was cut off just
    before the fresh connection's tree came (#684, live on an iPhone 11)."""
    from server.device.controller import DeviceController
    assert DeviceController._PUSH_READ_MIN_S >= coredevice_ax.FIRST_PUSH_S + 2.0


class TestTerminating:
    """terminate_app over CoreDevice (#664): devicectl, by the running process."""

    async def test_an_opted_in_phone_terminates_through_devicectl(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        ctrl.devicectl.terminate_app = AsyncMock(return_value=True)
        ctrl.wda_client.terminate_app = AsyncMock()

        _, was_running = await ctrl.terminate_app("com.quern.probe.scene", udid=PHONE)

        assert was_running is True
        ctrl.devicectl.terminate_app.assert_awaited_once_with(PHONE, "com.quern.probe.scene")
        ctrl.wda_client.terminate_app.assert_not_called()

    async def test_a_phone_not_on_the_push_terminates_through_wda(self, monkeypatch):
        ctrl, _ = _phone(monkeypatch, before="", reads=[])
        monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: "wda")
        ctrl.devicectl.terminate_app = AsyncMock()
        ctrl.wda_client.terminate_app = AsyncMock(return_value=True)

        await ctrl.terminate_app("com.quern.probe.scene", udid=PHONE)

        ctrl.wda_client.terminate_app.assert_awaited_once()
        ctrl.devicectl.terminate_app.assert_not_called()


async def test_after_a_push_crash_untargeted_typing_stays_on_hid(monkeypatch):
    """Untargeted typing reads nothing, so a crash gives it no reason to
    start WDA -- which, on a phone with none, it cannot (#698 review)."""
    ctrl, _ = _phone(monkeypatch, before="", reads=[])
    ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}

    result = await ctrl.type_text("abc", udid=PHONE)

    ctrl.coredevice_input.type_text.assert_awaited_once_with(PHONE, "abc")
    ctrl.wda_client.type_text.assert_not_called()
    assert result["input"] == "coredevice"
