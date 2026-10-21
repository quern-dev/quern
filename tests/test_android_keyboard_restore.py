"""The Android keyboard typing replaced is put back (#513).

uiautomator2's `send_keys` and `clear_text` select u2's own keyboard and
nothing selected the device's again, so a physical phone was left with a
keyboard that shows no keys. The keyboard is recorded before the first switch,
in the state directory, and restored when a session lets the device go, when
the server stops, and at the next start for a record a crashed process left.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from server.device.android import ime as ime_mod
from server.device.android.ime import ImeKeeper, is_u2_ime

GBOARD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
U2 = "com.github.uiautomator/.AdbKeyboard"
SERIAL = "emulator-5554"


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)
    return tmp_path


class FakeDevice:
    """A device whose `settings` and `ime` shell commands act on one value."""

    def __init__(self, ime: str = GBOARD, *, sticky: bool = False, broken: bool = False):
        self.ime = ime
        self.sticky = sticky        # ignores a switch, as a failed one would
        self.broken = broken
        self.calls: list[list[str]] = []

    def shell(self, cmd):
        self.calls.append(cmd)
        if self.broken:
            raise ConnectionError("device offline")
        if cmd[:3] == ["settings", "get", "secure"]:
            return SimpleNamespace(output=self.ime + "\n", exit_code=0)
        if cmd[:2] == ["ime", "set"] and not self.sticky:
            self.ime = cmd[2]
        return SimpleNamespace(output="", exit_code=0)


def _file(state_dir):
    path = state_dir / "android-ime.json"
    return json.loads(path.read_text()) if path.exists() else {}


class TestRemember:
    def test_the_keyboard_is_recorded_before_the_first_switch(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(GBOARD))
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_a_second_call_keeps_the_first_record(self, state_dir):
        """By the second call the device is on u2's keyboard; recording that
        would restore u2's keyboard."""
        keeper = ImeKeeper()
        keeper.remember(SERIAL, FakeDevice(GBOARD))
        keeper.remember(SERIAL, FakeDevice("com.example/.Other"))
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_a_device_already_on_u2s_keyboard_is_not_recorded(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(U2))
        assert _file(state_dir) == {}

    @pytest.mark.parametrize("value", ["", "null"])
    def test_no_keyboard_is_not_recorded(self, state_dir, value):
        ImeKeeper().remember(SERIAL, FakeDevice(value))
        assert _file(state_dir) == {}

    def test_a_failed_read_never_stops_the_typing(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(broken=True))
        assert _file(state_dir) == {}

    def test_the_record_survives_the_process(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(GBOARD))
        assert ImeKeeper().pending() == {SERIAL: GBOARD}

    def test_an_unreadable_file_reads_as_nothing_recorded(self, state_dir):
        (state_dir / "android-ime.json").write_text("{not json")
        assert ImeKeeper().pending() == {}

    def test_u2s_package_is_recognised(self):
        assert is_u2_ime(U2)
        assert is_u2_ime("com.github.uiautomator/.FastInputIME")
        assert not is_u2_ime(GBOARD)
        assert not is_u2_ime(None)


class TestRestore:
    def test_it_selects_the_recorded_keyboard_and_forgets_it(self, state_dir):
        keeper = ImeKeeper()
        keeper.remember(SERIAL, FakeDevice(GBOARD))
        device = FakeDevice(U2)
        action = keeper.restore(SERIAL, device)
        assert action == {"udid": SERIAL, "action": "keyboard_restored", "ime": GBOARD,
                          "ok": True}
        assert device.ime == GBOARD
        assert ["settings", "put", "secure", "default_input_method", GBOARD] in device.calls
        assert _file(state_dir) == {}

    def test_nothing_recorded_is_none_and_touches_nothing(self, state_dir):
        device = FakeDevice(U2)
        assert ImeKeeper().restore(SERIAL, device) is None
        assert device.calls == []

    def test_a_switch_that_did_not_hold_keeps_the_record(self, state_dir):
        """Checked by reading the setting back, not trusted from exit codes."""
        keeper = ImeKeeper()
        keeper.remember(SERIAL, FakeDevice(GBOARD))
        action = keeper.restore(SERIAL, FakeDevice(U2, sticky=True))
        assert action["ok"] is False and U2 in action["error"]
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_an_offline_device_keeps_the_record(self, state_dir):
        keeper = ImeKeeper()
        keeper.remember(SERIAL, FakeDevice(GBOARD))
        action = keeper.restore(SERIAL, FakeDevice(broken=True))
        assert action["ok"] is False
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_other_devices_records_are_kept(self, state_dir):
        keeper = ImeKeeper()
        keeper.remember(SERIAL, FakeDevice(GBOARD))
        keeper.remember("R5CT", FakeDevice("com.samsung/.Honeyboard"))
        keeper.restore(SERIAL, FakeDevice(U2))
        assert _file(state_dir) == {"R5CT": "com.samsung/.Honeyboard"}


def _backend(device):
    from server.device.android.u2_client import U2Backend

    backend = U2Backend()
    backend._connect = lambda serial: device
    return backend


class TestU2Backend:
    async def test_typing_records_the_keyboard_before_send_keys(self, state_dir):
        device = FakeDevice(GBOARD)
        seen = []

        def send_keys(text):
            seen.append(_file(state_dir))
            device.ime = U2           # what u2 does
        device.send_keys = send_keys
        await _backend(device).type_text(SERIAL, "hi")
        assert seen == [{SERIAL: GBOARD}], "recorded before u2 switched"

    async def test_clearing_records_the_keyboard_before_clear_text(self, state_dir,
                                                                   monkeypatch):
        device = FakeDevice(GBOARD)
        device.click = MagicMock()
        seen = []

        def clear_text():
            seen.append(_file(state_dir))
            device.ime = U2
        device.clear_text = clear_text
        monkeypatch.setattr("server.device.android.u2_client._focused_text_and_hint",
                            lambda d: (None, None))
        await _backend(device).select_all_and_delete(SERIAL, 10, 20)
        assert seen == [{SERIAL: GBOARD}]

    async def test_restore_connects_only_when_something_was_replaced(self, state_dir):
        def connect(serial):
            raise AssertionError("connected with nothing to restore")
        from server.device.android.u2_client import U2Backend

        backend = U2Backend()
        backend._connect = connect
        assert await backend.restore_keyboard(SERIAL) is None
        assert await backend.restore_keyboards() == []

    async def test_restore_keyboards_puts_back_every_record(self, state_dir):
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        device.ime = U2
        actions = await backend.restore_keyboards()
        assert [a["ok"] for a in actions] == [True]
        assert device.ime == GBOARD

    async def test_a_device_that_hangs_is_given_up_on_and_kept(self, state_dir,
                                                               monkeypatch):
        import time

        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        backend.RESTORE_TIMEOUT_S = 0.05
        backend._connect = lambda serial: time.sleep(0.5) or device
        action = await backend.restore_keyboard(SERIAL)
        assert action["ok"] is False and action["error"] == "TimeoutError"
        assert _file(state_dir) == {SERIAL: GBOARD}


class TestSessionRelease:
    def _app(self, u2):
        from server.session_registry import SessionRegistry

        registry = SessionRegistry(grace_seconds=180)
        controller = SimpleNamespace(u2=u2, shutdown=None)
        return SimpleNamespace(state=SimpleNamespace(sessions=registry,
                                                     device_controller=controller)), registry

    def _u2(self):
        u2 = MagicMock()

        async def restore(udid):
            u2.restored.append(udid)
            return {"udid": udid, "action": "keyboard_restored", "ime": GBOARD, "ok": True}
        u2.restored = []
        u2.restore_keyboard = restore
        return u2

    def _release(self, app):
        from server.api.sessions import run_release_policies

        return asyncio.run(run_release_policies(app))

    def test_a_released_device_gets_its_keyboard_back(self):
        u2 = self._u2()
        app, registry = self._app(u2)
        registry.open("claude-a")
        registry.reserve("claude-a", [SERIAL])
        registry.release("claude-a")
        actions = self._release(app)
        assert u2.restored == [SERIAL]
        assert any(a["action"] == "keyboard_restored" for a in actions)

    def test_a_device_reserved_again_meanwhile_keeps_u2s_keyboard(self):
        u2 = self._u2()
        app, registry = self._app(u2)
        registry.open("claude-a")
        registry.reserve("claude-a", [SERIAL])
        registry.unreserve("claude-a", [SERIAL])
        registry.open("claude-b")
        registry.reserve("claude-b", [SERIAL])
        self._release(app)
        assert u2.restored == []

    def test_a_failed_restore_is_reported_not_raised(self):
        u2 = MagicMock()

        async def restore(udid):
            raise RuntimeError("adb gone")
        u2.restore_keyboard = restore
        app, registry = self._app(u2)
        registry.open("claude-a")
        registry.reserve("claude-a", [SERIAL])
        registry.release("claude-a")
        actions = self._release(app)
        assert [a for a in actions if a["action"] == "keyboard_restored"][0]["ok"] is False


def test_the_record_lives_in_the_state_directory(state_dir):
    assert ime_mod._path() == state_dir / "android-ime.json"


async def test_the_server_puts_keyboards_back_at_startup_and_at_shutdown(monkeypatch):
    """Pinned against the real lifespan: a record a crashed process left is
    repaired at startup, and the server's own records at shutdown."""
    from server.config import ServerConfig
    from server.device.android.u2_client import U2Backend
    from server.lifecycle import network_monitor, update_check
    from server.main import create_app, lifespan

    async def _never(*a, **k):
        return None

    monkeypatch.setattr(network_monitor, "update_network_state", lambda *a: None)
    monkeypatch.setattr(network_monitor, "network_monitor_loop", _never)
    monkeypatch.setattr(update_check, "periodic_update_check", _never)
    calls = []

    async def restore_keyboards(self):
        calls.append("restore")
        return []
    monkeypatch.setattr(U2Backend, "restore_keyboards", restore_keyboards)
    app = create_app(config=ServerConfig(api_key="k"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    async with lifespan(app):
        await asyncio.wait_for(app.state._warmup_task, 60)
        assert calls == ["restore"], "at startup"
    assert calls == ["restore", "restore"], "and at shutdown"
