"""Plist reading, editing and diffing, all through plistlib.

No `plutil`: it addresses keys by key path, which makes every dotted key
unreachable, and it does not exist on Linux.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import plistlib
import shutil
import uuid
from pathlib import Path
from typing import Any

from server.models import AppStateNotFoundError, DeviceError


def _make_json_safe(obj: Any) -> Any:
    """Recursively convert plist types that aren't JSON-serializable.

    - bytes → lowercase hex string  (e.g. NSData blobs, binary tokens)
    - datetime → ISO 8601 string
    - Everything else passes through unchanged.
    """
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    return obj


async def read_plist(path: Path) -> dict:
    """Read a plist file and return its contents as a JSON-safe dict.

    Uses Python's plistlib (handles XML, binary, and all plist types).
    NSData blobs are returned as hex strings; NSDate as ISO 8601.
    """
    def _read() -> dict:
        with open(path, "rb") as f:
            return plistlib.load(f)

    try:
        raw = await asyncio.to_thread(_read)
    except Exception as e:
        raise DeviceError(
            f"plistlib read failed for {path}: {e}",
            tool="plistlib",
        )
    return _make_json_safe(raw)


def _plist_type_for(value: Any) -> Any:
    """The value as plistlib should store it: numbers and booleans as
    themselves (plistlib writes `True` as `<true/>`, not as the int it also
    is), anything else as its string form."""
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _load_for_edit(path: Path) -> tuple[dict, plistlib.PlistFormat]:
    raw = path.read_bytes()
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist00") else plistlib.FMT_XML
    data = plistlib.loads(raw)
    if not isinstance(data, dict):
        raise DeviceError(
            f"{path} holds a {type(data).__name__}, not a dictionary; "
            "only top-level keys can be edited",
            tool="plistlib",
        )
    return data, fmt


def _write_atomically(path: Path, data: dict, fmt: plistlib.PlistFormat) -> None:
    """Replace `path` in one step, keeping its format and permissions.

    A reader -- cfprefsd, or the app -- must never see a half-written file, so
    the new contents go to a sibling and are renamed over the original.
    """
    tmp = path.with_name(f".{path.name}.quern-{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(plistlib.dumps(data, fmt=fmt))
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _edit(path: Path, change) -> None:
    try:
        data, fmt = _load_for_edit(path)
        change(data)
        _write_atomically(path, data, fmt)
    except DeviceError:
        raise
    except Exception as e:
        # Broad on purpose. plistlib raises far more than its documented
        # InvalidFileException -- `ExpatError` for broken XML, `AttributeError`
        # for a bad <date>, OverflowError for a big int -- and each one that
        # escaped here became an undetailed 500.
        raise DeviceError(f"editing {path} failed: {type(e).__name__}: {e}", tool="plistlib") from e


async def set_plist_values(path: Path, values: dict[str, Any]) -> None:
    """Set top-level keys in a plist file, all in one write.

    Keys are taken literally. This used to shell out to `plutil -replace`,
    which reads its argument as a *key path* -- so `probe.greeting` meant key
    `greeting` inside a dictionary called `probe`, and every reverse-DNS key
    (the ordinary way to name a preference) failed with "Key path not found",
    or was written into a nested dictionary that happened to match.

    All or nothing: either every key is written or the file is untouched.

    Type inference: bool -> <true/>/<false/>, int -> <integer>, float -> <real>,
    everything else -> <string>.
    """
    converted = {key: _plist_type_for(value) for key, value in values.items()}
    await asyncio.to_thread(_edit, path, lambda data: data.update(converted))


async def set_plist_value(path: Path, key: str, value: Any) -> None:
    """Set one top-level key. See `set_plist_values`."""
    await set_plist_values(path, {key: value})


async def remove_plist_key(path: Path, key: str) -> None:
    """Remove a top-level key, taken literally.

    Raises `AppStateNotFoundError` when the key is not there, rather than
    reporting a removal that did nothing.
    """
    def _remove(data: dict) -> None:
        if key not in data:
            raise AppStateNotFoundError(f"Key {key!r} not found in {path.name}", tool="plistlib")
        del data[key]

    await asyncio.to_thread(_edit, path, _remove)


def diff_plists(old: dict, new: dict) -> dict:
    """Compare two plist dicts and return added/removed/changed keys.

    Returns {"added": {...}, "removed": {...}, "changed": {...}}.
    Changed entries have the form {key: {"old": ..., "new": ...}}.
    """
    old_keys = set(old.keys())
    new_keys = set(new.keys())

    added = {k: new[k] for k in sorted(new_keys - old_keys)}
    removed = {k: old[k] for k in sorted(old_keys - new_keys)}
    changed = {}
    for k in sorted(old_keys & new_keys):
        if old[k] != new[k]:
            changed[k] = {"old": old[k], "new": new[k]}

    return {"added": added, "removed": removed, "changed": changed}
