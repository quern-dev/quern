"""Typing text no US key types by pasting it, over CoreDevice (#780).

Before this it went through WDA, which on an iOS 27 phone in no-WDA mode is
a refusal. Measured on an iPhone 11 (iOS 27.2.1): set on the clipboard over
CoreDevice's pasteboard service and pasted with Cmd+V on a HID keyboard,
"café 🎉 Ωμέγα" landed exactly in Settings' search, with no "Allow Paste"
prompt, and the app searched for it as for typing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device.ios import coredevice_input
from server.device.ios.coredevice_input import CoreDeviceInput
from tests.test_coredevice_input import PHONE, FakeLib


@pytest.fixture
def lib(monkeypatch):
    """The input tests' phone: a fake pymobiledevice3, no waits."""
    fake = FakeLib()
    monkeypatch.setattr(coredevice_input, "_pmd3", fake)
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(return_value=("fd00::1", 51000)))
    for wait in ("TAP_HOLD_S", "FIRST_REPORT_S", "KEY_HOLD_S"):
        monkeypatch.setattr(coredevice_input, wait, 0)
    return fake


@pytest.fixture
def wda():
    return SimpleNamespace(TOOL_NAME="wda", type_text=AsyncMock())

SAVED = [{"types": ["public.utf8-plain-text"],
          "data": {"public.utf8-plain-text": {"data": b"the user's copy"}}}]


class FakePasteboard:
    """CoreDevice's pasteboard service: what it held, and every call."""

    def __init__(self, held=SAVED):
        self.held = list(held)
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        board = self

        class Service:
            def __init__(self, rsd):
                pass

            async def connect(self):
                board._maybe_fail("connect")

            async def close(self):
                board.calls.append(("close",))

            async def get(self):
                board._maybe_fail("get")
                board.calls.append(("get",))
                return {"command": "PULL_REPLY", "pasteboard": {"items": list(board.held)}}

            async def set_text(self, text):
                board._maybe_fail("set_text")
                board.calls.append(("set_text", text))
                board.held = [{"text": text}]

            async def set(self, items):
                board._maybe_fail("set")
                board.calls.append(("set", list(items)))
                board.held = list(items)

        self.Service = Service

    def _maybe_fail(self, what):
        if what in self.fail:
            raise self.fail[what]


@pytest.fixture
def board(monkeypatch):
    b = FakePasteboard()
    monkeypatch.setattr(coredevice_input, "_pasteboard_service", lambda: b.Service)
    return b


async def test_text_no_us_key_types_is_pasted_and_the_clipboard_put_back(lib, wda, board):
    hid = CoreDeviceInput(wda)
    assert await hid.type_text(PHONE, "café 🎉") == "coredevice"

    wda.type_text.assert_not_called()
    assert [c[0] for c in board.calls] == ["get", "set_text", "set", "close"]
    assert board.calls[1] == ("set_text", "café 🎉")
    assert board.held == SAVED, "the user's clipboard was not put back"
    assert lib.keystrokes() == [(0xE3,), (0xE3, 0x19), ()], "not Cmd+V"
    assert hid.take_note(PHONE) is None


async def test_text_us_keys_type_is_still_keystrokes(lib, wda, board):
    """A paste is one change to the app; keystrokes are what WDA's typing was."""
    await CoreDeviceInput(wda).type_text(PHONE, "aA a")
    assert board.calls == []


@pytest.mark.parametrize("step", ["connect", "get"])
async def test_a_clipboard_that_cannot_be_saved_is_not_used(lib, wda, board, step):
    """Nothing pasted over what could not be put back: it goes the way of
    anything HID cannot send, with nothing on the phone changed."""
    board.fail[step] = OSError("pasteboard service refused")
    assert await CoreDeviceInput(wda).type_text(PHONE, "é") == "wda"
    assert not [c for c in board.calls if c[0] in ("set_text", "set")]
    assert lib.keystrokes() == []


async def test_an_unreadable_pull_is_not_an_empty_clipboard(lib, wda, board, monkeypatch):
    """A reply not of that shape could hide a clipboard: not overwritten."""
    async def odd(self):
        return {"command": "PULL_REPLY"}

    monkeypatch.setattr(board.Service, "get", odd)
    assert await CoreDeviceInput(wda).type_text(PHONE, "é") == "wda"
    assert not [c for c in board.calls if c[0] == "set_text"]


async def test_an_empty_clipboard_is_put_back_empty(lib, wda, monkeypatch):
    empty = FakePasteboard(held=[])
    monkeypatch.setattr(coredevice_input, "_pasteboard_service", lambda: empty.Service)
    await CoreDeviceInput(wda).type_text(PHONE, "é")
    assert ("set", []) in empty.calls and empty.held == []


async def test_a_clipboard_not_put_back_is_said_not_raised(lib, wda, board):
    """The text was typed: raising would read as not typed, and invite a second."""
    board.fail["set"] = OSError("gone")
    hid = CoreDeviceInput(wda)
    assert await hid.type_text(PHONE, "é") == "coredevice"
    note = hid.take_note(PHONE)
    assert "could not be put back" in note and "holds the typed text" in note
    assert hid.take_note(PHONE) is None, "a note is said once"


async def test_a_failed_paste_still_puts_the_clipboard_back(lib, wda, board):
    lib.fail_key = (2, OSError("keyboard gone"))    # the Cmd+V report itself
    with pytest.raises(Exception):
        await CoreDeviceInput(wda).type_text(PHONE, "é")
    assert board.held == SAVED
    wda.type_text.assert_not_called()   # a key may have reached it: never re-sent


async def test_the_untargeted_answer_says_it_was_pasted(monkeypatch):
    from tests.test_no_implicit_wda import PHONE as P27
    from tests.test_no_implicit_wda import _failing_phone

    ctrl, _start = _failing_phone(monkeypatch)
    ctrl.coredevice_input.type_text = AsyncMock(return_value="coredevice")
    ctrl.coredevice_input.take_note = lambda udid: None
    result = await ctrl.type_text("café", udid=P27)
    assert result["input"] == "coredevice" and result["note"].startswith("Pasted")

    ascii_only = await ctrl.type_text("cafe", udid=P27)
    assert ascii_only["note"].startswith("Typed as keystrokes")


def test_this_pymobiledevice3_has_the_pasteboard_service():
    assert isinstance(coredevice_input._pasteboard_service(), type)


def test_the_pull_reply_shapes(self=None):
    items = coredevice_input._pasteboard_items
    assert items({"pasteboard": {"items": SAVED}}) == SAVED
    assert items({"pasteboard": {"items": []}}) == []
    assert items({"items": SAVED}) == SAVED
    assert items({"pasteboard": {}}) is None
    assert items(SimpleNamespace()) is None


# MARK: - a clipboard not put back always reaches the answer (CodeRabbit on #784)


async def test_a_paste_that_fails_still_says_the_clipboard_was_not_put_back(lib, wda, board):
    """The keyboard failed before Cmd+V, so the text went through WDA -- and
    the clipboard, not put back, held it with nothing to say so."""
    lib.fail["keyboard"] = OSError("no keyboard")
    board.fail["set"] = OSError("gone")
    hid = CoreDeviceInput(wda)
    assert await hid.type_text(PHONE, "é") == "wda"
    assert "holds the typed text" in (hid.take_note(PHONE) or "")


async def test_the_untargeted_answer_never_says_put_back_when_it_was_not(monkeypatch):
    from tests.test_no_implicit_wda import PHONE as P27
    from tests.test_no_implicit_wda import _failing_phone

    ctrl, _start = _failing_phone(monkeypatch)
    ctrl.coredevice_input.type_text = AsyncMock(return_value="coredevice")
    warning = "The phone's clipboard could not be put back (x): it now holds the typed text."
    ctrl.coredevice_input.take_note = lambda udid: warning
    note = (await ctrl.type_text("café", udid=P27))["note"]
    assert "put back." not in note.split(warning)[0], "said put back, then the opposite"
    assert note.endswith(warning)

    ctrl.coredevice_input.type_text = AsyncMock(return_value="wda")
    assert (await ctrl.type_text("café", udid=P27))["note"] == warning


async def test_a_password_left_on_the_clipboard_is_said_on_a_named_field(monkeypatch):
    """The whole password is pasted into a secure field; a clipboard not put
    back holds it, and the named answer said nothing. Said now -- without the
    text."""
    from tests.test_coredevice_typing import PHONE as TP
    from tests.test_coredevice_typing import _phone

    ctrl, _ = _phone(monkeypatch, before="", reads=["••"], secure=True)
    warning = "The phone's clipboard could not be put back (x): it now holds the typed text."
    ctrl.coredevice_input.take_note = lambda udid: warning
    result = await ctrl.type_text("é1", udid=TP, identifier="field_default")
    assert warning in result["note"]
    assert "é1" not in str(result)
