"""Typing on a physical iPhone driven over CoreDevice, with no WDA (#664).

Keys go over a virtual HID keyboard, so the app sees real keystrokes; the phone
decodes them with its own keyboard and input method, which nothing reports, so
a named field is read back and set through the accessibility service when what
arrived is not what was meant. Measured on an iPhone 11 with a Japanese
keyboard: "Hello abc #@" arrived as "Helloあbc＃＠", and the set put it right.
"""

from __future__ import annotations

import plistlib
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.controller_ui import _keyboard_correction
from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import parse_tree, set_value, set_value_request
from server.models import DeviceError, DeviceType, UIElement
from tests.test_coredevice_ax import SETTINGS, _fake_lib, phone_address  # noqa: F401  (a fixture)

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
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
        """The set reads the push, which would run the crashing dump again (#669)."""
        ctrl, setter = _phone(monkeypatch, before="", reads=["あ"])
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}

        with pytest.raises(DeviceError):
            await ctrl.type_text("a", udid=PHONE, identifier="field_default")

        setter.assert_not_called()

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

        with pytest.raises(coredevice_ax.BareAppTree):
            await set_value(PHONE, lambda e: True, "x", timeout=5)
        assert not [c for c in state.control.calls if c[0] == "processDataFromHost:"]

    async def test_an_app_that_changed_under_the_read_is_refused(self, monkeypatch):
        lib, state = _fake_lib([])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        tree = parse_tree(SETTINGS)
        first = coredevice_ax.Tree("InitialDump", tree.elements, 711, tree.refs)
        newest_tree = coredevice_ax.Tree("AdditionalData", tree.elements, 99, tree.refs)

        async def newest(queue, held=None):
            return newest_tree, first, newest_tree
        monkeypatch.setattr(coredevice_ax, "_newest_tree", newest)

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
    what marks it. Unmarked, its dots read as the keyboard's change and the
    password was set through accessibility (found live on an iPhone 11)."""

    @pytest.mark.parametrize(("bundle", "traits", "expected"), [
        # The iPhone 11's password field, empty and then filled.
        ({coredevice_ax.ATTR_SECURE: False}, 0x2201040000, "AXSecureTextField"),
        ({coredevice_ax.ATTR_SECURE: True}, 0x2201240000, "AXSecureTextField"),
        # Its ordinary text fields.
        ({coredevice_ax.ATTR_SECURE: False}, 0x2200040000, ""),
        ({coredevice_ax.ATTR_SECURE: True}, 0, "AXSecureTextField"),
        ({}, coredevice_ax.TRAIT_SEARCH_FIELD, "AXSearchField"),
        ({coredevice_ax.ATTR_SECURE: True}, coredevice_ax.TRAIT_SEARCH_FIELD,
         "AXSecureTextField"),
    ])
    def test_the_subrole(self, bundle, traits, expected):
        assert coredevice_ax._role_description(bundle, traits) == expected

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
