"""Persistent certificate state management.

Stores per-device cert installation state in ~/.quern/cert-state.json,
which persists across server restarts (unlike state.json which is deleted
on server stop).

Uses fcntl file locking matching the pattern in server/lifecycle/state.py.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from server.config import CONFIG_DIR

logger = logging.getLogger(__name__)

CERT_STATE_FILE = CONFIG_DIR / "cert-state.json"

# Only these fields should ever be written to cert-state.json.
# Computed fields (wifi_proxy_stale, active_wifi_network) and legacy flat
# proxy fields must not be stored — they are derived at read time.
_CANONICAL_FIELDS = frozenset({
    "name", "cert_installed", "fingerprint",
    "installed_at", "verified_at", "wifi_proxy_configs",
})


def read_cert_state() -> dict[str, dict]:
    """Read cert-state.json with shared file lock.

    Returns empty dict if file doesn't exist or contains invalid JSON.
    """
    if not CERT_STATE_FILE.exists():
        return {}

    try:
        fd = CERT_STATE_FILE.open("r")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            content = fd.read()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

        if not content.strip():
            return {}
        return _canonicalised(json.loads(content))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read cert state file: %s", e)
        return {}


def _newer_configs(first: dict, second: dict) -> dict:
    """Union two SSID maps, keeping the more recently recorded on a clash.

    By `set_at`, not by which record the file happened to list second. Both
    spellings of one device carry their own history, and file order says
    nothing about which was written later -- so taking the later entry could
    resurrect a proxy address the device had already moved away from, and the
    trace would then attribute its flows by a stale `client_ip`.

    An entry with no `set_at` loses to one that has it: knowing when beats not
    knowing. Between two undated entries there is nothing to choose, so the
    second stands.
    """
    merged = dict(first)
    for ssid, config in second.items():
        existing = merged.get(ssid)
        if existing is None:
            merged[ssid] = config
            continue
        if str(config.get("set_at") or "") >= str(existing.get("set_at") or ""):
            merged[ssid] = config
    return merged


def _canonicalised(state: dict[str, dict]) -> dict[str, dict]:
    """Keys as the rest of quern spells them.

    A physical device has two identifiers, and this file can hold either: the
    writer canonicalises what it is handed, but only once device discovery has
    run, and every file written before canonicalisation existed holds the raw
    hardware udid.

    Doing it here rather than in each reader is the point. There are eight, and
    the first attempt fixed one -- `ip_to_udid` -- which repaired trace lookups
    while `_verify_physical_device` still read by the canonical key, found
    nothing, and reported `proxy_not_configured` for a device whose proxy was
    configured. Every reader goes through this function; none of them should
    have to know.

    Unknown spellings pass through untouched, so simulators and anything
    recorded before its device was ever listed are unaffected.

    **Collisions are merged, not overwritten.** An old file can hold both
    spellings of one device -- one written before canonicalisation, one after
    -- and taking the later record wholesale drops the other's
    `wifi_proxy_configs`, which is exactly the data the trace needs to
    attribute that device's flows. A test caught this doing precisely that: a
    proxy config recorded while the alias map was cold vanished when a second
    record for the same device arrived.

    Scalar fields still take the later value, which is the newest thing known
    about the certificate. Only `wifi_proxy_configs` unions, keyed by SSID, and
    a repeated SSID takes the later one for the same reason.
    """
    from server.device.devicectl import canonical_device_id

    merged: dict[str, dict] = {}
    for udid, record in state.items():
        key = canonical_device_id(udid)
        if key not in merged:
            merged[key] = dict(record)
            continue
        configs = _newer_configs(
            merged[key].get("wifi_proxy_configs") or {},
            record.get("wifi_proxy_configs") or {},
        )
        merged[key] = {**merged[key], **record}
        if configs:
            merged[key]["wifi_proxy_configs"] = configs
    return merged


def read_cert_state_for_device(udid: str) -> dict | None:
    """Read cert state for a specific device.

    Returns None if no state exists for the device.
    """
    from server.device.devicectl import canonical_device_id

    # The *key* too, not only the state. `read_cert_state` canonicalises what
    # it returns, so a caller asking by the hardware udid looked for a key that
    # had just been rewritten to the other spelling and got None -- the same
    # miss this whole change exists to end, one layer down.
    state = read_cert_state()
    return state.get(canonical_device_id(udid))


def _write_cert_state(state: dict) -> None:
    """Write the full cert state dict to disk with exclusive lock."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = CERT_STATE_FILE.open("a+") if CERT_STATE_FILE.exists() else _create_and_open()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(state, indent=2))
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def update_cert_state(udid: str, cert_data: dict[str, Any]) -> None:
    """Update the named fields of one device's cert state, under an exclusive lock.

    Read-modify-write at two levels: other devices' entries are preserved, and
    so are *this* device's fields that `cert_data` does not mention. Passing
    `{"cert_installed": False}` changes that and nothing else.

    The second level was missing, and it lost data. `is_cert_installed` rebuilt
    the entry from the four fields it had just learned and wrote that over the
    whole record, so every verification erased `installed_at` -- and with every
    caller verifying since the cache was deleted, that happened almost
    immediately after any install. It took `wifi_proxy_configs` with it, which
    is a physical device's recorded proxy host and `client_ip`: the thing
    `_verify_physical_device` reads to find that device's traffic at all.

    To clear a field, name it: `{"fingerprint": None}` writes None. Only
    omission preserves. This is why callers should pass what they mean rather
    than a full `model_dump()`, which names every field including the ones it
    has no opinion about.

    Only canonical fields are written — computed fields like wifi_proxy_stale
    and active_wifi_network are stripped before saving.
    """
    cert_data = {k: v for k, v in cert_data.items() if k in _CANONICAL_FIELDS}

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Use a+ to create file if it doesn't exist
    fd = CERT_STATE_FILE.open("a+") if CERT_STATE_FILE.exists() else _create_and_open()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        content = fd.read()

        if content.strip():
            try:
                state = json.loads(content)
            except json.JSONDecodeError:
                state = {}
        else:
            state = {}

        # Filtered on the way in *and* out. `cert_data` is already stripped
        # above; without stripping what is on disk too, the merge preserves
        # legacy and computed fields forever -- where the old wholesale replace
        # quietly healed them on the next write. Flat `proxy_host`/`proxy_port`
        # are the bad case: `DeviceCertState` ignores extras, so they construct
        # cleanly, nothing raises, `strip_noncanonical_fields` never fires, and
        # they persist for good.
        existing = {
            k: v for k, v in (state.get(udid) or {}).items()
            if k in _CANONICAL_FIELDS
        }
        state[udid] = {**existing, **cert_data}

        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(state, indent=2))
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def strip_noncanonical_fields(udid: str) -> None:
    """Remove computed/legacy fields from a stored entry and re-save.

    Called when proxy_status detects a parse failure for a cert-state entry,
    indicating stale computed fields are stored that shouldn't be there.
    After this call the entry only contains canonical source-of-truth fields.
    """
    state = read_cert_state()
    if not state or udid not in state:
        return
    state[udid] = {k: v for k, v in state[udid].items() if k in _CANONICAL_FIELDS}
    _write_cert_state(state)


def record_device_proxy_config(
    udid: str, ssid: str, proxy_host: str, port: int, client_ip: str | None = None
) -> None:
    """Record the Wi-Fi proxy config for a specific network on a physical device."""
    existing = read_cert_state_for_device(udid) or {}
    configs: dict[str, Any] = existing.get("wifi_proxy_configs") or {}
    configs[ssid] = {
        "proxy_host": proxy_host,
        "proxy_port": port,
        "client_ip": client_ip,
        "set_at": datetime.now(UTC).isoformat(),
    }
    existing["wifi_proxy_configs"] = configs
    update_cert_state(udid, existing)


def forget_device_proxy_configs(udid: str) -> list[str]:
    """Drop every recorded Wi-Fi proxy config for a device, naming what went.

    Android's `global http_proxy` is one setting for the whole device, not one
    per network, so clearing it invalidates every SSID recorded here at once.
    Leaving the records behind would be the failure this file keeps producing:
    a stored config that reads as current while the device is no longer
    routed anywhere.

    Clears **every spelling** of the device, not just the canonical one, and
    reports what a subsequent read can no longer see rather than what it set
    out to remove. Both halves were wrong first time round, and they hid each
    other. `read_cert_state_for_device` returns the canonicalised *merge* of
    every spelling, while `update_cert_state` writes to the one raw key it is
    handed; so zeroing the canonical entry left the other spelling's configs
    on disk, and `_canonicalised` unioned them straight back on the next read.
    Meanwhile the return value was computed from the read *before* the write,
    so it asserted a removal that the very next read contradicted -- a claim
    that could not fail, which is the shape that keeps getting through here.
    """
    from server.device.devicectl import canonical_device_id

    canonical = canonical_device_id(udid)
    before = set((read_cert_state_for_device(udid) or {}).get(
        "wifi_proxy_configs"
    ) or {})
    if not before:
        return []

    # Raw keys, deliberately: this needs the spellings as they sit on disk,
    # which is exactly what the canonicalised read hides.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = CERT_STATE_FILE.open("a+") if CERT_STATE_FILE.exists() else _create_and_open()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        content = fd.read()
        try:
            state = json.loads(content) if content.strip() else {}
        except json.JSONDecodeError:
            state = {}

        for key, entry in state.items():
            if canonical_device_id(key) == canonical and isinstance(entry, dict):
                entry["wifi_proxy_configs"] = {}

        fd.seek(0)
        fd.truncate()
        json.dump(state, fd, indent=2)
        fd.flush()
        os.fsync(fd.fileno())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

    # What actually went, read back through the same merge a caller would use.
    after = set((read_cert_state_for_device(udid) or {}).get(
        "wifi_proxy_configs"
    ) or {})
    return sorted(before - after)


def _create_and_open():
    """Create cert state file and return file handle opened for read/write."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CERT_STATE_FILE.touch()
    return CERT_STATE_FILE.open("a+")
