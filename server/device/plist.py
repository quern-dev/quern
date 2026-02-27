"""Thin subprocess wrapper around plutil for plist read/write, plus plistlib for reading."""

from __future__ import annotations

import asyncio
import datetime
import plistlib
from pathlib import Path
from typing import Any

from server.models import DeviceError


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
            tool="plutil",
        )
    return _make_json_safe(raw)


async def set_plist_value(path: Path, key: str, value: Any) -> None:
    """Set a key in a plist file using plutil.

    Type inference: bool → -bool, int → -integer, float → -float, everything else → -string.

    Uses: plutil -replace <key> -<type> <value> <path>
    """
    if isinstance(value, bool):
        type_flag = "-bool"
        str_value = "true" if value else "false"
    elif isinstance(value, int):
        type_flag = "-integer"
        str_value = str(value)
    elif isinstance(value, float):
        type_flag = "-float"
        str_value = str(value)
    else:
        type_flag = "-string"
        str_value = str(value)

    proc = await asyncio.create_subprocess_exec(
        "plutil", "-replace", key, type_flag, str_value, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DeviceError(
            f"plutil set failed for {path} key {key!r}: {stderr.decode().strip()}",
            tool="plutil",
        )


async def remove_plist_key(path: Path, key: str) -> None:
    """Remove a key from a plist file using plutil.

    Uses: plutil -remove <key> <path>
    """
    proc = await asyncio.create_subprocess_exec(
        "plutil", "-remove", key, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DeviceError(
            f"plutil remove failed for {path} key {key!r}: {stderr.decode().strip()}",
            tool="plutil",
        )
