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
import threading
import time
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
    """Android as measured on an emulator: `ime set` changes the method in use
    (`dumpsys input_method`), and exits 255 for one that is not installed;
    `settings put` changes only the setting, which can then say anything."""

    def __init__(self, ime: str = GBOARD, *, installed=(GBOARD, U2), broken=False,
                 hang: float = 0.0):
        self.current = ime
        self.setting = ime
        self.installed = set(installed)
        self.broken = broken
        self.hang = hang
        self.calls: list[list[str]] = []
        self.timeouts: list = []

    def shell(self, cmd, timeout=None):
        self.calls.append(cmd)
        self.timeouts.append(timeout)
        if self.hang:
            time.sleep(self.hang)
        if self.broken:
            raise ConnectionError("device offline")
        if cmd == ["dumpsys", "input_method"]:
            return SimpleNamespace(output=f"  mCurMethodId={self.current}\n  other=1\n",
                                   exit_code=0)
        if cmd[:3] == ["settings", "get", "secure"]:
            return SimpleNamespace(output=self.setting + "\n", exit_code=0)
        if cmd[:3] == ["settings", "put", "secure"]:
            self.setting = cmd[4]
            return SimpleNamespace(output="", exit_code=0)
        if cmd[:2] == ["ime", "set"]:
            if cmd[2] not in self.installed:
                return SimpleNamespace(output=f"Unknown input method {cmd[2]} cannot be "
                                              "selected for user #0\n", exit_code=255)
            self.current = cmd[2]
            return SimpleNamespace(output=f"Input method {cmd[2]} selected\n", exit_code=0)
        return SimpleNamespace(output="", exit_code=0)


def _file(state_dir):
    path = state_dir / "android-ime.json"
    return json.loads(path.read_text()) if path.exists() else {}


class TestRemember:
    def test_the_keyboard_is_recorded_before_the_first_switch(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(GBOARD))
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_it_reads_the_method_in_use_not_the_setting(self, state_dir):
        device = FakeDevice(GBOARD)
        device.setting = "com.example/.Stale"
        ImeKeeper().remember(SERIAL, device)
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

    def test_no_keyboard_is_not_recorded(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice("null"))
        assert _file(state_dir) == {}

    def test_a_failed_read_never_stops_the_typing(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(broken=True))
        assert _file(state_dir) == {}

    def test_every_shell_call_is_bounded(self, state_dir):
        """u2's default is 60s, and a hung adb holds the device's lock."""
        device = FakeDevice(GBOARD)
        keeper = ImeKeeper()
        keeper.remember(SERIAL, device)
        device.current = U2
        keeper.restore(SERIAL, device)
        assert device.timeouts and all(t == ime_mod.SHELL_TIMEOUT_S for t in device.timeouts)

    def test_the_record_survives_the_process(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(GBOARD))
        assert ImeKeeper().pending() == {SERIAL: GBOARD}

    def test_an_unreadable_file_is_never_overwritten(self, state_dir):
        """Other devices' records are in it."""
        (state_dir / "android-ime.json").write_text("{not json")
        keeper = ImeKeeper()
        assert keeper.pending() == {}
        keeper.remember(SERIAL, FakeDevice(GBOARD))
        assert (state_dir / "android-ime.json").read_text() == "{not json"

    def test_no_temporary_file_is_left(self, state_dir):
        ImeKeeper().remember(SERIAL, FakeDevice(GBOARD))
        assert [p.name for p in state_dir.iterdir()] == ["android-ime.json"]

    def test_u2s_package_is_recognised(self):
        assert is_u2_ime(U2)
        assert is_u2_ime("com.github.uiautomator/.FastInputIME")
        assert not is_u2_ime(GBOARD)
        assert not is_u2_ime(None)


def _recorded(device=None):
    keeper = ImeKeeper()
    keeper.remember(SERIAL, device or FakeDevice(GBOARD))
    return keeper


class TestRestore:
    def test_it_selects_the_recorded_keyboard_and_forgets_it(self, state_dir):
        keeper = _recorded()
        device = FakeDevice(U2)
        action = keeper.restore(SERIAL, device)
        assert action == {"udid": SERIAL, "action": "keyboard_restored", "ime": GBOARD,
                          "ok": True}
        assert device.current == GBOARD and device.setting == GBOARD
        assert _file(state_dir) == {}

    def test_nothing_recorded_is_none_and_touches_nothing(self, state_dir):
        device = FakeDevice(U2)
        assert ImeKeeper().restore(SERIAL, device) is None
        assert device.calls == []

    def test_a_keyboard_that_is_gone_keeps_the_record(self, state_dir):
        """Uninstalled since: `ime set` refuses, and the setting written after
        it would read back as if it had worked (review)."""
        keeper = _recorded()
        device = FakeDevice(U2, installed=(U2,))
        action = keeper.restore(SERIAL, device)
        assert action["ok"] is False and "Unknown input method" in action["error"]
        assert device.current == U2
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_a_switch_the_service_did_not_take_keeps_the_record(self, state_dir):
        keeper = _recorded()
        device = FakeDevice(U2)
        real = device.shell

        def ignore_set(cmd, timeout=None):
            if cmd[:2] == ["ime", "set"]:
                device.calls.append(cmd)
                return SimpleNamespace(output="", exit_code=0)   # says yes, does nothing
            return real(cmd, timeout)
        device.shell = ignore_set
        action = keeper.restore(SERIAL, device)
        assert action["ok"] is False and U2 in action["error"]
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_a_keyboard_changed_by_hand_is_left_alone(self, state_dir):
        """Or a different phone behind a reused STF serial: not on quern's
        keyboard, so there is nothing of quern's to undo."""
        keeper = _recorded()
        device = FakeDevice("com.touchtype.swiftkey/.KeyboardService",
                            installed=(GBOARD, U2, "com.touchtype.swiftkey/.KeyboardService"))
        action = keeper.restore(SERIAL, device)
        assert action["action"] == "keyboard_left" and action["ok"] is True
        assert device.current == "com.touchtype.swiftkey/.KeyboardService"
        assert not any(c[:2] == ["ime", "set"] for c in device.calls)
        assert _file(state_dir) == {}

    def test_a_keyboard_the_device_cannot_name_keeps_the_record(self, state_dir):
        """Just after a boot the input-method service is not up: no answer is
        not a hand change, and dropping the record lost the keyboard for good."""
        keeper = _recorded()
        device = FakeDevice(U2)
        device.current = "null"
        action = keeper.restore(SERIAL, device)
        assert action["ok"] is False and "did not say" in action["error"]
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_an_offline_device_keeps_the_record(self, state_dir):
        keeper = _recorded()
        action = keeper.restore(SERIAL, FakeDevice(broken=True))
        assert action["ok"] is False
        assert _file(state_dir) == {SERIAL: GBOARD}

    def test_other_devices_records_are_kept(self, state_dir):
        keeper = _recorded()
        keeper.remember("R5CT", FakeDevice("com.samsung/.Honeyboard"))
        keeper.restore(SERIAL, FakeDevice(U2))
        assert _file(state_dir) == {"R5CT": "com.samsung/.Honeyboard"}


def _backend(device):
    from server.device.android.u2_client import U2Backend

    backend = U2Backend()
    backend._connect = lambda serial: device
    return backend


def _finished(backend) -> threading.Event:
    """Set when the backend's restore worker returns, however it returns."""
    done = threading.Event()
    real = backend.ime.restore

    def restore(*a, **k):
        try:
            return real(*a, **k)
        finally:
            done.set()
    backend.ime.restore = restore
    return done


class TestU2Backend:
    async def test_typing_records_the_keyboard_before_send_keys(self, state_dir):
        device = FakeDevice(GBOARD)
        seen = []

        def send_keys(text):
            seen.append(_file(state_dir))
            device.current = U2           # what u2 does
        device.send_keys = send_keys
        await _backend(device).type_text(SERIAL, "hi")
        assert seen == [{SERIAL: GBOARD}], "recorded before u2 switched"

    async def test_the_device_lock_is_held_through_send_keys(self, state_dir):
        """A restore between the record and the switch lost the record, and
        with u2's keyboard current nothing recorded again (review)."""
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        held = []
        device.send_keys = lambda text: held.append(
            not backend.ime.lock_for(SERIAL).acquire(blocking=False))
        await backend.type_text(SERIAL, "hi")
        assert held == [True]

    async def test_clearing_records_the_keyboard_before_clear_text(self, state_dir,
                                                                   monkeypatch):
        device = FakeDevice(GBOARD)
        device.click = MagicMock()
        backend = _backend(device)
        seen = []

        def clear_text():
            seen.append((_file(state_dir),
                         not backend.ime.lock_for(SERIAL).acquire(blocking=False)))
            device.current = U2
        device.clear_text = clear_text
        monkeypatch.setattr("server.device.android.u2_client._focused_text_and_hint",
                            lambda d: (None, None))
        await backend.select_all_and_delete(SERIAL, 10, 20)
        assert seen == [({SERIAL: GBOARD}, True)]

    async def test_restore_connects_only_when_something_was_replaced(self, state_dir):
        def connect(serial):
            raise AssertionError("connected with nothing to restore")
        from server.device.android.u2_client import U2Backend

        backend = U2Backend()
        backend._connect = connect
        assert await backend.restore_keyboard(SERIAL) is None
        assert await backend.restore_keyboards() == []

    async def test_restore_keyboards_puts_back_every_record(self, state_dir, monkeypatch):
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        device.current = U2

        async def online():
            return {SERIAL}
        monkeypatch.setattr(backend, "_online_serials", online)
        actions = await backend.restore_keyboards()
        assert [a["ok"] for a in actions] == [True]
        assert device.current == GBOARD

    async def test_a_device_adb_cannot_see_is_skipped_and_kept(self, state_dir, monkeypatch):
        """Waiting on an unplugged phone spent the shutdown's budget (review)."""
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        backend._connect = lambda serial: (_ for _ in ()).throw(AssertionError("connected"))

        async def online():
            return {"some-other-device"}
        monkeypatch.setattr(backend, "_online_serials", online)
        actions = await backend.restore_keyboards()
        assert [(a["ok"], a["error"]) for a in actions] == [
            (False, "not connected; kept for the next start")]
        assert _file(state_dir) == {SERIAL: GBOARD}

    async def test_a_device_that_hangs_is_given_up_on_and_kept(self, state_dir):
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        backend.RESTORE_TIMEOUT_S = 0.05
        backend._connect = lambda serial: time.sleep(0.5) or device
        done = _finished(backend)
        action = await backend.restore_keyboard(SERIAL)
        assert action["ok"] is False and action["error"] == "TimeoutError"
        assert _file(state_dir) == {SERIAL: GBOARD}
        # The worker outlives the await; let it end inside this test, or its
        # late write lands in another test's state dir (CodeRabbit).
        assert done.wait(10), "the worker never finished"

    async def test_a_hung_shell_never_blocks_the_event_loop(self, state_dir):
        """The first version took its lock on the loop while a worker held it
        across device I/O: one hung shell stalled every request (review)."""
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        device.current = U2
        backend.RESTORE_TIMEOUT_S = 0.2
        device.hang = 0.3                 # every shell call now hangs past the bound
        done = _finished(backend)
        gaps, last = [], time.monotonic()

        async def ticker():
            nonlocal last
            for _ in range(15):
                await asyncio.sleep(0.02)
                gaps.append(time.monotonic() - last)
                last = time.monotonic()
        tick = asyncio.create_task(ticker())
        first = await backend.restore_keyboard(SERIAL)
        second = await backend.restore_keyboard(SERIAL)   # its lock is still held
        await tick
        assert first["ok"] is False and second["ok"] is False
        assert max(gaps) < 0.15, f"the loop stalled for {max(gaps):.2f}s"
        assert done.wait(10), "the worker never finished"

    async def test_a_restore_waits_for_typing_in_progress(self, state_dir):
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        device.current = U2
        lock = backend.ime.lock_for(SERIAL)
        lock.acquire()
        threading.Timer(0.1, lock.release).start()   # the typing finishes
        action = await backend.restore_keyboard(SERIAL)
        assert action["ok"] is True and device.current == GBOARD

    async def test_a_device_busy_past_the_bound_is_reported_not_waited_on(self, state_dir):
        device = FakeDevice(GBOARD)
        backend = _backend(device)
        backend.ime.remember(SERIAL, device)
        backend.RESTORE_TIMEOUT_S = 0.2
        lock = backend.ime.lock_for(SERIAL)
        lock.acquire()                    # typing that never finishes
        try:
            action = await backend.restore_keyboard(SERIAL)
        finally:
            lock.release()
        assert action["error"] == "the device stayed busy"
        assert _file(state_dir) == {SERIAL: GBOARD}

    async def test_online_serials_reads_adb_devices(self, monkeypatch):
        from server.device.android.u2_client import U2Backend

        class Proc:
            returncode = 0

            async def communicate(self):
                return (b"List of devices attached\nemulator-5554\tdevice\n"
                        b"10.0.0.5:5555\toffline\nR5CT\tunauthorized\n\n", b"")

        async def exec_(*a, **k):
            return Proc()
        monkeypatch.setattr("server.device.android.adb._find_sdk_tool", lambda n: "/x/adb")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
        assert await U2Backend()._online_serials() == {"emulator-5554"}

    async def test_no_adb_means_try_everything(self, monkeypatch):
        from server.device.android.u2_client import U2Backend

        monkeypatch.setattr("server.device.android.adb._find_sdk_tool", lambda n: None)
        assert await U2Backend()._online_serials() is None


class TestIdleRestore:
    """A device nobody reserved kept u2's keyboard until the server stopped;
    it now goes back after IDLE_RESTORE_S without typing."""

    def _typed(self, state_dir, monkeypatch, online=(SERIAL,)):
        device = FakeDevice(GBOARD)
        device.send_keys = lambda text: setattr(device, "current", U2)
        backend = _backend(device)

        async def serials():
            return set(online)
        monkeypatch.setattr(backend, "_online_serials", serials)
        return backend, device

    async def test_a_device_typed_on_recently_keeps_u2s_keyboard(self, state_dir,
                                                                monkeypatch):
        backend, device = self._typed(state_dir, monkeypatch)
        await backend.type_text(SERIAL, "hi")
        assert await backend.restore_idle_keyboards() == []
        assert device.current == U2

    async def test_after_the_idle_time_it_is_put_back(self, state_dir, monkeypatch):
        backend, device = self._typed(state_dir, monkeypatch)
        await backend.type_text(SERIAL, "hi")
        later = time.monotonic() + backend.IDLE_RESTORE_S
        actions = await backend.restore_idle_keyboards(now=later)
        assert [a["ok"] for a in actions] == [True]
        assert device.current == GBOARD and _file(state_dir) == {}

    async def test_just_short_of_the_idle_time_it_is_not(self, state_dir, monkeypatch):
        backend, device = self._typed(state_dir, monkeypatch)
        await backend.type_text(SERIAL, "hi")
        soon = time.monotonic() + backend.IDLE_RESTORE_S - 5
        assert await backend.restore_idle_keyboards(now=soon) == []

    async def test_the_idle_time_is_three_minutes(self):
        from server.device.android.u2_client import U2Backend

        assert U2Backend.IDLE_RESTORE_S == 180.0

    async def test_typing_that_failed_still_counts_as_typing(self, state_dir, monkeypatch):
        """u2 may have switched before it failed."""
        backend, device = self._typed(state_dir, monkeypatch)

        def fail(text):
            device.current = U2
            raise RuntimeError("broadcast failed")
        device.send_keys = fail
        with pytest.raises(Exception):
            await backend.type_text(SERIAL, "hi")
        assert await backend.restore_idle_keyboards() == []

    async def test_a_record_from_an_earlier_process_counts_as_idle(self, state_dir,
                                                                  monkeypatch):
        """A startup repair that could not reach the device: the sweep retries."""
        backend, device = self._typed(state_dir, monkeypatch)
        ImeKeeper().remember(SERIAL, FakeDevice(GBOARD))
        device.current = U2
        actions = await backend.restore_idle_keyboards()
        assert [a["ok"] for a in actions] == [True]

    async def test_a_device_adb_cannot_see_is_left_for_later(self, state_dir, monkeypatch):
        backend, device = self._typed(state_dir, monkeypatch, online=())
        await backend.type_text(SERIAL, "hi")
        later = time.monotonic() + backend.IDLE_RESTORE_S
        assert await backend.restore_idle_keyboards(now=later) == []
        assert _file(state_dir) == {SERIAL: GBOARD}

    async def test_a_failed_restore_waits_another_idle_period(self, state_dir, monkeypatch):
        """An uninstalled keyboard fails every time; retrying every sweep was
        ~120 warnings an hour (CodeRabbit)."""
        backend, device = self._typed(state_dir, monkeypatch)
        await backend.type_text(SERIAL, "hi")
        device.installed = {U2}                   # the recorded keyboard is gone
        later = time.monotonic() + backend.IDLE_RESTORE_S
        first = await backend.restore_idle_keyboards(now=later)
        assert [a["ok"] for a in first] == [False]
        assert await backend.restore_idle_keyboards(now=later + 30) == [], "no retry yet"

    async def test_typing_while_the_restore_waits_for_the_lock_cancels_it(
            self, state_dir, monkeypatch):
        """Judged idle, then typed on before the restore got the device: it
        is not idle any more, and the new stamp survives (CodeRabbit)."""
        backend, device = self._typed(state_dir, monkeypatch)
        await backend.type_text(SERIAL, "hi")
        later = time.monotonic() + backend.IDLE_RESTORE_S
        lock = backend.ime.lock_for(SERIAL)
        lock.acquire()                              # typing in progress...

        def finish_typing():
            backend._last_typed[SERIAL] = later     # ...stamps as it ends
            lock.release()
        threading.Timer(0.1, finish_typing).start()
        actions = await backend.restore_idle_keyboards(now=later)
        assert actions == []
        assert device.current == U2 and _file(state_dir) == {SERIAL: GBOARD}
        assert backend._last_typed[SERIAL] == later

    async def test_typing_after_a_restore_records_again(self, state_dir, monkeypatch):
        backend, device = self._typed(state_dir, monkeypatch)
        await backend.type_text(SERIAL, "hi")
        await backend.restore_idle_keyboards(now=time.monotonic() + backend.IDLE_RESTORE_S)
        await backend.type_text(SERIAL, "again")
        assert _file(state_dir) == {SERIAL: GBOARD} and device.current == U2


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

    def test_the_device_stays_unavailable_until_its_keyboard_is_back(self):
        """Freed first, a new session could reserve it and have its keyboard
        switched under it (CodeRabbit)."""
        from server.session_registry import SessionConflict

        u2 = MagicMock()
        app, registry = self._app(u2)
        seen = []

        async def restore(udid):
            registry.open("claude-b", principal="key:other")   # not the owner,
            try:                                               # who may always
                registry.reserve("claude-b", [udid])
                seen.append("reserved during the restore")
            except SessionConflict:
                seen.append("refused")
            return {"udid": udid, "action": "keyboard_restored", "ime": GBOARD, "ok": True}
        u2.restore_keyboard = restore
        # A client key's release: the registry holds a device back from other
        # keys only while a client key's clean-up runs (the owner's frees it).
        registry.open("claude-a", principal="key:a")
        registry.reserve("claude-a", [SERIAL])
        registry.release("claude-a")
        self._release(app)
        assert seen == ["refused"]
        registry.reserve("claude-b", [SERIAL])      # free once it is done

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
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "keyboard-sweep" in names, "the idle sweep runs"
    assert calls == ["restore", "restore"], "and at shutdown"


def test_the_log_says_which_keyboards_were_left_alone(caplog):
    """A keyboard changed by hand was logged as "Put back" (live test)."""
    import logging

    from server.main import _log_keyboards

    with caplog.at_level(logging.INFO, logger="server.main"):
        _log_keyboards([{"udid": SERIAL, "action": "keyboard_left", "ime": GBOARD, "ok": True,
                         "detail": "the device is on X, not quern's; left as it is"}],
                       "at shutdown")
    assert "Left emulator-5554's keyboard as it is" in caplog.text
    assert "Put back" not in caplog.text
