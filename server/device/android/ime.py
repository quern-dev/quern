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

Not after every `type_text`: u2 selects its keyboard again on every call, so
each would switch twice -- measured, a switch costs ~300ms on an emulator.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: Every input method u2 installs lives in this package -- ours (the Quern
#: Driver APK) and the stock openatx one alike.
U2_IME_PACKAGE = "com.github.uiautomator/"

_FILE = "android-ime.json"


def is_u2_ime(ime: str | None) -> bool:
    return bool(ime) and ime.startswith(U2_IME_PACKAGE)


def _path() -> Path:
    # Read at call time: QUERN_STATE_DIR redirects CONFIG_DIR, and a path
    # fixed at import would outlive the redirect.
    from server import config

    return config.CONFIG_DIR / _FILE


class ImeKeeper:
    """Device serial -> the input method it had before quern replaced it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(_path().read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Could not read %s; keyboards to restore are unknown", _path(),
                           exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _save(self, records: dict[str, str]) -> None:
        path = _path()
        if not records:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)

    def pending(self) -> dict[str, str]:
        with self._lock:
            return self._load()

    def remember(self, serial: str, device) -> None:
        """Record the device's keyboard, unless one is recorded already.

        Before anything that selects u2's keyboard. Called from the worker
        thread that is about to type, with that call's u2 device. A device
        already on u2's keyboard with no record -- left there by a quern from
        before this record existed, or by other u2 tooling -- is not recorded:
        what it had before is unknown, and u2's keyboard is not it.

        Never raises: a keyboard that could not be recorded must not stop the
        typing it was recorded for.
        """
        with self._lock:
            try:
                records = self._load()
                if serial in records:
                    return
                current = device.shell(
                    ["settings", "get", "secure", "default_input_method"]).output.strip()
                if not current or current == "null" or is_u2_ime(current):
                    return
                records[serial] = current
                self._save(records)
            except Exception:  # noqa: BLE001 -- see the docstring
                logger.warning("Could not record %s's keyboard", serial, exc_info=True)

    def restore(self, serial: str, device) -> dict | None:
        """Select the recorded keyboard again, and forget it once that held.

        None when there is nothing recorded. A failure keeps the record, so a
        later restore tries again, and is reported rather than raised.
        """
        with self._lock:
            records = self._load()
            ime = records.get(serial)
            if ime is None:
                return None
            action = {"udid": serial, "action": "keyboard_restored", "ime": ime}
            try:
                device.shell(["ime", "enable", ime])
                device.shell(["ime", "set", ime])
                device.shell(["settings", "put", "secure", "default_input_method", ime])
                now = device.shell(
                    ["settings", "get", "secure", "default_input_method"]).output.strip()
                if now != ime:
                    return {**action, "ok": False,
                            "error": f"the device reports {now or 'nothing'} after the switch"}
            except Exception as e:  # noqa: BLE001 -- reported, never raised
                return {**action, "ok": False, "error": str(e)}
            records.pop(serial, None)
            try:
                self._save(records)
            except OSError as e:
                return {**action, "ok": True, "warning": f"could not update {_path()}: {e}"}
            return {**action, "ok": True}
