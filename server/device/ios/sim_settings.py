"""Simulator settings written to disk directly, instead of through the Settings app.

Driving the Settings app works, but it is slow, its steps differ by iOS
version, and it breaks whenever Apple moves a row. Every agent that needed
auto-correction or password AutoFill off was working that out for itself, so
the settings that matter for automation live in a catalog here
(sim_settings_catalog.json), each with where it is stored and how it was
verified.

Two kinds of storage, both inside the simulator's data directory:

- **Configuration-profile restrictions** in `UserSettings.plist`, the same
  mechanism an MDM profile uses. ManagedConfiguration recomputes its effective
  settings from this file at boot, so it is only read then.
- **Keyboard preferences** in `com.apple.keyboard.preferences.plist`, which the
  keyboard caches.

Both are therefore written with the simulator shut down, and a booted one is
rebooted to apply them. A reboot ends whatever app is running, so it happens
only when the caller says so: `reboot=False` on a booted simulator that needs a
change is a `RebootRequiredError`, not a silent restart in the middle of a test.
A setting that is already right, or a simulator that is shut down, needs no
reboot and never gets one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import plistlib
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    InvalidDeviceRequestError,
    RebootRequiredError,
)

CATALOG_PATH = Path(__file__).with_name("sim_settings_catalog.json")
#: Where CoreSimulator keeps each simulator's directory. A module attribute so
#: tests point it at a temporary directory.
DEVICES_DIR = Path.home() / "Library/Developer/CoreSimulator/Devices"

STATES = ("on", "off")


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


@dataclass
class _Write:
    file: str
    key_path: list[str]
    values: dict[str, Any]
    read: str | None
    absent: str | None


def _writes(entry: dict) -> list[_Write]:
    return [
        _Write(w["file"], w["key_path"], w["values"], w.get("read"), w.get("absent"))
        for w in entry["writes"]
    ]


def device_dir(udid: str) -> Path:
    return DEVICES_DIR / udid


def runtime_of(udid: str) -> str | None:
    """"iOS 26.5", from the simulator's device.plist, or None if unreadable."""
    try:
        raw = plistlib.loads((device_dir(udid) / "device.plist").read_bytes())["runtime"]
    except (OSError, KeyError, plistlib.InvalidFileException, ValueError):
        return None
    tail = str(raw).rsplit(".", 1)[-1]          # "iOS-26-5"
    platform, _, version = tail.partition("-")
    return f"{platform} {version.replace('-', '.')}" if version else tail


def _file_path(udid: str, alias: str) -> Path:
    return device_dir(udid) / catalog()["files"][alias]


def _load(path: Path) -> dict | None:
    """The plist's contents; {} when the file does not exist; None when it
    exists and cannot be read -- "could not ask" is not "nothing set"."""
    if not path.exists():
        return {}
    try:
        data = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dig(data: dict, key_path: list[str]):
    for key in key_path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _place(data: dict, key_path: list[str], value) -> None:
    for key in key_path[:-1]:
        child = data.get(key)
        if not isinstance(child, dict):
            child = data[key] = {}
        data = child
    data[key_path[-1]] = value


def _state_of(write: _Write, stored) -> str:
    """Which state a stored value means: "on", "off", or "unknown"."""
    if stored is None:
        return write.absent or "unknown"
    for state, value in write.values.items():
        if write.read:
            if isinstance(stored, dict) and isinstance(value, dict) \
                    and stored.get(write.read) == value.get(write.read):
                return state
        elif stored == value:
            return state
    return "unknown"


def read_state(udid: str, name: str) -> str | None:
    """The setting's state: "on" or "off" when every key it covers agrees,
    "mixed" when they do not, "unknown" for a value the catalog does not
    recognise, or None when a file could not be read.

    Every key, not only the first: the restriction can say off while the
    hardware-keyboard switch -- the one quern's own typing goes through --
    says on, and reading only the restriction reported that as already off.
    A key that is absent agrees with whatever the rest say, unless it is the
    first, whose `absent` is what a fresh simulator means.
    """
    seen: set[str] = set()
    for i, write in enumerate(_writes(_entry(name))):
        data = _load(_file_path(udid, write.file))
        if data is None:
            return None
        stored = _dig(data, write.key_path)
        if stored is None and i > 0:
            continue
        seen.add(_state_of(write, stored))
    if "unknown" in seen:
        return "unknown"
    if len(seen) > 1:
        return "mixed"
    return seen.pop() if seen else "unknown"


def _entry(name: str) -> dict:
    settings = catalog()["settings"]
    if name not in settings:
        raise InvalidDeviceRequestError(
            f"no simulator setting named {name!r}; known: {', '.join(sorted(settings))}",
            tool="quern",
        )
    return settings[name]


def verified_runtimes(entry: dict) -> tuple[list[str], list[str]]:
    """(runtimes verified by effect, runtimes where only the storage is known)."""
    full = [v["runtime"] for v in entry.get("verified", []) if not v.get("storage_only")]
    storage = [v["runtime"] for v in entry.get("verified", []) if v.get("storage_only")]
    return full, storage


def describe(udid: str) -> dict:
    """Every catalog setting's state on this simulator."""
    runtime = runtime_of(udid)
    out = {}
    for name, entry in catalog()["settings"].items():
        full, storage = verified_runtimes(entry)
        out[name] = {
            "title": entry["title"],
            "state": read_state(udid, name),
            "verified_on": full,
            "storage_known_on": storage,
            "verified_here": runtime in full,
        }
    return {"udid": udid, "runtime": runtime, "settings": out}


def _write_plist(path: Path, data: dict) -> None:
    """Binary, as the system writes them, and atomically: a reader -- the
    simulator booting -- must never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _files_for(name: str) -> dict[str, list[_Write]]:
    by_file: dict[str, list[_Write]] = {}
    for w in _writes(_entry(name)):
        by_file.setdefault(w.file, []).append(w)
    return by_file


def check_writable(udid: str, name: str) -> None:
    """Refuse now anything `write_state` would refuse, so it is refused before
    a booted simulator is shut down rather than after."""
    for alias in _files_for(name):
        path = _file_path(udid, alias)
        if alias == "restrictions" and not path.exists():
            # Created at first boot. Writing a fresh one would replace a file
            # whose other contents the system expects, so refuse instead.
            raise InvalidDeviceRequestError(
                f"{path.name} does not exist yet: boot this simulator once so iOS "
                f"creates it, then set {name}",
                tool="quern",
            )
        if _load(path) is None:
            raise DeviceError(f"cannot read {path}; not changing {name}", tool="quern")


def write_state(udid: str, name: str, state: str) -> None:
    """Write every key the setting covers. The simulator must be shut down.

    The files are read again here rather than reusing what `check_writable`
    read while the simulator was up: the keyboard caches its preferences and
    writes them late, so a copy taken before the shutdown can be older than
    the file is after it.
    """
    check_writable(udid, name)
    for alias, group in _files_for(name).items():
        path = _file_path(udid, alias)
        data = _load(path)
        if data is None:
            raise DeviceError(f"cannot read {path}; not changing {name}", tool="quern")
        for w in group:
            _place(data, w.key_path, w.values[state])
        _write_plist(path, data)


#: One change at a time per simulator. Two calls racing would each see the
#: other's shutdown or boot in progress -- measured by the review: the second
#: wrote while the first was shutting down and reported no reboot.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(udid: str) -> asyncio.Lock:
    return _locks.setdefault(udid, asyncio.Lock())


async def set_setting(
    simctl,
    udid: str,
    name: str,
    state: str,
    *,
    reboot: bool,
    device_state: Callable[[str], Awaitable[str]],
    after_boot: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Set one setting. Returns what happened: changed, rebooted, and whether
    this runtime is one the entry was verified on.

    `device_state` returns simctl's own state string ("Booted", "Shutdown",
    "Shutting Down"...). Only the first two are acted on: a simulator part way
    through either transition is still running, and writing to it then is the
    write-under-a-running-system this module exists to avoid. `after_boot`
    runs once a reboot has finished, for what a fresh boot needs -- the
    controller passes its input-service repair.
    """
    if state not in STATES:
        raise InvalidDeviceRequestError(
            f"state must be 'on' or 'off', not {state!r}", tool="quern",
        )
    entry = _entry(name)
    if not device_dir(udid).is_dir():
        raise InvalidDeviceRequestError(f"no simulator directory for {udid}", tool="quern")
    runtime = runtime_of(udid)
    if runtime is None or not runtime.startswith("iOS "):
        raise DeviceOperationUnsupportedError(
            f"simulator settings are catalogued for iOS simulators only; {udid[:8]} runs "
            f"{runtime or 'an unreadable runtime'}",
            tool="quern",
        )
    full, storage = verified_runtimes(entry)
    result: dict = {"udid": udid, "name": name, "state": state, "runtime": runtime,
                    "changed": False, "rebooted": False, "verified_here": runtime in full}
    if runtime not in full:
        result["warning"] = (
            f"{name} is verified on {', '.join(full) or 'no runtime'}"
            + (f" (storage only on {', '.join(storage)})" if storage else "")
            + f", not {runtime}; check the effect before relying on it."
        )

    async with _lock_for(udid):
        current = read_state(udid, name)
        if current == state:
            return result

        sim_state = await device_state(udid)
        if sim_state == "unknown":
            # Not "shut down": a simulator whose state could not be read may
            # be running, and writing under it is what this refuses.
            raise DeviceError(
                f"could not read the state of simulator {udid[:8]}; not changing {name}",
                tool="simctl",
            )
        if sim_state not in ("Booted", "Shutdown"):
            raise InvalidDeviceRequestError(
                f"simulator {udid[:8]} is {sim_state!r}; set {name} once it has finished "
                "booting or shutting down",
                tool="simctl",
            )
        booted = sim_state == "Booted"
        if booted and not reboot:
            raise RebootRequiredError(
                f"{entry['title']} is {current or 'unreadable'} and changing it needs this "
                f"simulator rebooted, which ends whatever app is running. Pass reboot: true "
                f"to allow it, or set it while the simulator is shut down.",
                tool="quern",
            )
        # Everything that can be refused is refused here, while the simulator
        # is still up: found after the shutdown, it left a booted simulator
        # shut down with nothing written.
        check_writable(udid, name)

        if booted:
            await simctl.shutdown(udid)
        written = False
        try:
            write_state(udid, name, state)
            written = True
        finally:
            if booted:
                # Booted again whatever happened above, so a failed write does
                # not leave a simulator down that the caller had running.
                try:
                    await simctl.boot(udid)
                    await simctl.wait_until_booted(udid)
                except DeviceError as e:
                    done = (f"{name} was written as {state}" if written
                            else f"{name} was not changed")
                    raise DeviceError(
                        f"{done}, but the simulator did not come back up: {e}", tool=e.tool,
                    ) from e
                result["rebooted"] = True
                if after_boot is not None:
                    await after_boot(udid)
        result["changed"] = True

        now = read_state(udid, name)
        if now != state:
            # The write went somewhere nothing reads, or the boot rewrote it.
            raise DeviceError(
                f"wrote {name} as {state}, but it reads back as {now or 'unreadable'}",
                tool="quern",
            )
        return result
