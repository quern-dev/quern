"""The Android keyboard quern replaced, and putting it back (#513).

Typing goes through uiautomator2's `send_keys`, and clearing through its
`clear_text`. Both select u2's own input method first (`ime set`, and
`settings put secure default_input_method`), and nothing selected the
device's keyboard again afterwards. On an emulator that is harmless. On a
physical phone the owner's keyboard stayed replaced after quern was done, by
one that shows no keys.

So the keyboard a device had is recorded the first time quern is about to
replace it. The record is kept in the state directory, so a crash does not lose
it. It is put back when quern is done with the device: when the session
holding it ends, when the server stops, and, for a record an earlier process
left behind, when the server next starts.

Not after every `type_text`: each would then switch twice, since u2 selects its
keyboard again whenever another one is current -- measured, a switch costs
~300ms on an emulator.

Two rules keep a restore from losing the keyboard it exists to keep:

- **One device lock spans the record and the typing, and the restore.** Taken
  apart, a restore could land between a call's record check and its
  `send_keys`, which then switched to u2's keyboard with the record already
  gone -- and with u2's keyboard current, nothing would ever record again.
- **No device I/O under a lock the event loop takes.** `pending()` reads the
  file only. A hung adb shell holds one device's lock in a worker thread, and
  nothing else waits on it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: Every input method u2 installs lives in this package -- ours (the Quern
#: Driver APK) and the stock openatx one alike.
U2_IME_PACKAGE = "com.github.uiautomator/"

#: Seconds one shell command on the device may take. u2's default is 60, and a
#: restore runs four.
SHELL_TIMEOUT_S = 10

_FILE = "android-ime.json"
_CURRENT_RE = re.compile(r"mCurMethodId=(\S+)")


def is_u2_ime(ime: str | None) -> bool:
    return bool(ime) and ime.startswith(U2_IME_PACKAGE)


def _path() -> Path:
    # Read at call time: QUERN_STATE_DIR redirects CONFIG_DIR, and a path
    # fixed at import would outlive the redirect.
    from server import config

    return config.CONFIG_DIR / _FILE


class _Unreadable(Exception):
    """The record file exists and cannot be read: not the same as empty."""


def _shell(device, cmd: list[str]):
    return device.shell(cmd, timeout=SHELL_TIMEOUT_S)


def current_ime(device) -> str | None:
    """The input method the device is actually using, from the input-method
    service -- not the setting, which a `settings put` makes say anything."""
    out = _shell(device, ["dumpsys", "input_method"]).output
    m = _CURRENT_RE.search(out or "")
    return m.group(1) if m and m.group(1) != "null" else None


class ImeKeeper:
    """Device serial -> the input method it had before quern replaced it."""

    def __init__(self) -> None:
        self._file_lock = threading.Lock()      # held for file I/O only
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def lock_for(self, serial: str) -> threading.Lock:
        """The lock a caller holds across `remember` and the u2 call after it,
        and that `restore` holds across the switch."""
        with self._locks_lock:
            return self._locks.setdefault(serial, threading.Lock())

    # -- the file ------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(_path().read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise _Unreadable(str(e)) from e
        if not isinstance(data, dict):
            raise _Unreadable(f"expected an object, found {type(data).__name__}")
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _save(self, records: dict[str, str]) -> None:
        path = _path()
        if not records:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{_FILE}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(records, indent=2, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _update(self, change) -> None:
        """Read, change, write, under the file lock. Raises _Unreadable rather
        than overwrite a file it could not read -- other devices' records are
        in it."""
        with self._file_lock:
            records = self._load()
            change(records)
            self._save(records)

    def pending(self) -> dict[str, str]:
        """Every record. No device I/O and no device lock, so the event loop
        may call it. An unreadable file reads as nothing to do, and says so."""
        with self._file_lock:
            try:
                return self._load()
            except _Unreadable as e:
                logger.warning("Could not read %s (%s); keyboards to restore are unknown",
                               _path(), e)
                return {}

    # -- the device ----------------------------------------------------------

    def remember(self, serial: str, device) -> None:
        """Record the device's keyboard, unless one is recorded already.

        Call with `lock_for(serial)` held, from the worker thread about to
        type, and keep holding it through the u2 call. A device already on
        u2's keyboard with no record -- left there by a quern from before this
        record existed, or by other u2 tooling -- is not recorded: what it had
        before is unknown, and u2's keyboard is not it.

        Never raises: a keyboard that could not be recorded must not stop the
        typing it was recorded for. It does leave that device unrepairable, so
        the warning says what it costs.
        """
        try:
            if serial in self.pending():
                return
            current = current_ime(device)
            if not current or is_u2_ime(current):
                return
            self._update(lambda records: records.setdefault(serial, current))
        except Exception:  # noqa: BLE001 -- see the docstring
            logger.warning("Could not record %s's keyboard; quern will not be able to "
                           "put it back after typing", serial, exc_info=True)

    def restore(self, serial: str, device) -> dict | None:
        """Select the recorded keyboard again, and forget it once that held.

        Call with `lock_for(serial)` held. None when there is nothing recorded.
        A failure keeps the record, so a later restore tries again, and is
        reported rather than raised. A device no longer on u2's keyboard --
        changed by hand, or a different phone behind a reused serial -- is
        left as it is and its record dropped.
        """
        ime = self.pending().get(serial)
        if ime is None:
            return None
        action = {"udid": serial, "action": "keyboard_restored", "ime": ime}
        try:
            now = current_ime(device)
            if not is_u2_ime(now):
                self._update(lambda records: records.pop(serial, None))
                return {**action, "action": "keyboard_left", "ok": True,
                        "detail": f"the device is on {now or 'no keyboard'}, not quern's; "
                                  "left as it is"}
            _shell(device, ["ime", "enable", ime])
            chosen = _shell(device, ["ime", "set", ime])
            if chosen.exit_code != 0:
                said = (chosen.output or "").strip()
                return {**action, "ok": False,
                        "error": said or f"ime set exited {chosen.exit_code}"}
            _shell(device, ["settings", "put", "secure", "default_input_method", ime])
            now = current_ime(device)
            if now != ime:
                return {**action, "ok": False,
                        "error": f"the device is on {now or 'no keyboard'} after the switch"}
        except Exception as e:  # noqa: BLE001 -- reported, never raised
            return {**action, "ok": False, "error": str(e) or type(e).__name__}
        try:
            self._update(lambda records: records.pop(serial, None))
        except (OSError, _Unreadable) as e:
            return {**action, "ok": True, "warning": f"could not update {_path()}: {e}"}
        return {**action, "ok": True}
