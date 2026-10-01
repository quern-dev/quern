"""Reading and editing a plist inside a live app container.

Preference files -- `Library/Preferences/*.plist` in a container -- are not
the truth on a booted simulator. cfprefsd caches every app's defaults, hands
them to the app from that cache, and writes them to disk on its own schedule,
measured at 3s to more than 15s behind the app. So on a booted simulator they
are read and written *through* cfprefsd, with `defaults` run inside the
simulator:

- a read sees what the app has written, including what is not on disk yet
  (measured: the file said 1, the app and `defaults export` said 3);
- a write updates the cache the app reads from, so even a running app sees it
  on its next read, and the file follows.

The first fix for the stale cache restarted cfprefsd around every read and
write instead. That made the files current, but restarting the daemon under a
running app intermittently swallowed the app's own in-flight writes -- about
once per full conformance run -- so it is kept only for save and restore,
where the app has already been terminated.

Every other plist, and a preference file on a simulator that is shut down (no
cfprefsd, nothing cached), is read and edited as a file.
"""

from __future__ import annotations

import asyncio
import plistlib
from pathlib import Path
from typing import Any

from server.device.app_state import get_device_state
from server.device.plist import (
    _make_json_safe,
    read_plist,
    remove_plist_key,
    set_plist_values,
)
from server.models import AppStateNotFoundError, DeviceError

_DEFAULTS_TIMEOUT = 20.0


def _preferences_domain(container: Path, full_path: Path) -> str | None:
    """The cfprefsd domain for `full_path`, or None if it is not a preference file.

    The domain is the path without `.plist`; `defaults` addresses a container's
    preferences that way, not by bundle id, which would name the simulator's
    own global domain instead of the app's.
    """
    prefs = (container / "Library" / "Preferences").resolve()
    if full_path.parent == prefs and full_path.suffix == ".plist":
        return str(full_path.with_suffix(""))
    return None


async def _through_cfprefsd(udid: str, container: Path, full_path: Path) -> str | None:
    """The domain to use, when this file is cfprefsd's to serve right now."""
    domain = _preferences_domain(container, full_path)
    if domain is None:
        return None
    # Shut down means no cfprefsd and nothing cached: the file is the truth.
    # Anything else, "unknown" included, goes through cfprefsd, where a failure
    # is reported rather than silently reading a possibly stale file.
    if await get_device_state(udid) == "Shutdown":
        return None
    return domain


async def _defaults(udid: str, *args: str) -> bytes:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "spawn", udid, "defaults", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULTS_TIMEOUT)
    except (OSError, TimeoutError) as e:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        detail = f"timed out after {_DEFAULTS_TIMEOUT:.0f}s" if isinstance(e, TimeoutError) else e
        raise DeviceError(f"defaults {args[0]} failed: {detail}", tool="defaults") from e
    if proc.returncode != 0:
        raise DeviceError(
            f"defaults {args[0]} failed: {stderr.decode(errors='replace').strip()}",
            tool="defaults",
        )
    return stdout


async def _export(udid: str, domain: str) -> dict:
    raw = await _defaults(udid, "export", domain, "-")
    try:
        data = plistlib.loads(raw)
    except Exception as e:
        raise DeviceError(
            f"defaults export returned an unreadable plist: {e}", tool="defaults",
        ) from e
    if not isinstance(data, dict):
        raise DeviceError("defaults export did not return a dictionary", tool="defaults")
    return data


def _defaults_type(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return "-bool", "true" if value else "false"
    if isinstance(value, int):
        return "-int", str(value)
    if isinstance(value, float):
        return "-float", repr(value)
    return "-string", str(value)


def _same(a: Any, b: Any) -> bool:
    # `1 == True` in Python; a bool written must read back as a bool.
    return a == b and type(a) is type(b)


async def read_live_plist(udid: str, container: Path, full_path: Path) -> dict:
    """The plist's contents as the app would see them, JSON-safe."""
    domain = await _through_cfprefsd(udid, container, full_path)
    if domain is None:
        if not full_path.exists():
            raise AppStateNotFoundError(f"Plist not found: {full_path.name}", tool="quern")
        return await read_plist(full_path)
    data = await _export(udid, domain)
    # An unknown domain exports as an empty dictionary. Not-found only when
    # there is nothing in cfprefsd *and* nothing on disk, so an app whose first
    # write has not been flushed yet still reads.
    if not data and not full_path.exists():
        raise AppStateNotFoundError(f"Plist not found: {full_path.name}", tool="quern")
    return _make_json_safe(data)


async def set_live_plist_values(
    udid: str, container: Path, full_path: Path, values: dict[str, Any],
) -> None:
    """Set top-level keys, literally, and confirm they read back.

    Through cfprefsd a preference file need not exist yet: setting a flag
    before the app's first launch is an ordinary thing to want.
    """
    domain = await _through_cfprefsd(udid, container, full_path)
    if domain is None:
        if not full_path.exists():
            raise AppStateNotFoundError(f"Plist not found: {full_path.name}", tool="quern")
        await set_plist_values(full_path, values)
        return
    written: list[str] = []
    try:
        for key, value in values.items():
            flag, text = _defaults_type(value)
            await _defaults(udid, "write", domain, key, flag, text)
            written.append(key)
    except DeviceError as e:
        raise DeviceError(
            f"{e} -- after writing {written or 'no keys'} of {list(values)}", tool=e.tool,
        ) from e
    # Read back through the same channel. A write that `defaults` accepted and
    # cfprefsd did not keep is exactly the shape of failure this module exists
    # to stop reporting as success.
    stored = await _export(udid, domain)
    wrong = {k: stored.get(k) for k, v in values.items() if not _same(stored.get(k), v)}
    if wrong:
        raise DeviceError(f"values did not read back as written: {wrong}", tool="defaults")


async def remove_live_plist_key(udid: str, container: Path, full_path: Path, key: str) -> None:
    """Remove a top-level key, literally; not-found if it is not there."""
    domain = await _through_cfprefsd(udid, container, full_path)
    if domain is None:
        if not full_path.exists():
            raise AppStateNotFoundError(f"Plist not found: {full_path.name}", tool="quern")
        await remove_plist_key(full_path, key)
        return
    if key not in await _export(udid, domain):
        raise AppStateNotFoundError(f"Key {key!r} not found in {full_path.name}", tool="quern")
    await _defaults(udid, "delete", domain, key)
    if key in await _export(udid, domain):
        raise DeviceError(f"{key!r} was still present after deleting it", tool="defaults")
