"""App state checkpoint and plist inspection for simulator apps.

Checkpoints are stored in ~/.quern/app-states/<bundle_id>/<label>/.
Each checkpoint directory contains:
  .quern-meta.json         ← metadata (label, description, bundle_id, captured_at, udid)
  data-container/          ← copy of the app's data container
  app-group/<group-id>/    ← one subdir per app group (keyed by group identifier)
  keychain/                ← optional: simulator keychain database (see below)

Simulator only.

Keychain
--------
Auth tokens live in the simulator keychain, which sits *outside* every app container, at
<device>/data/Library/Keychains/. A checkpoint of containers alone therefore always
restores to a logged-out app, however it was captured — the single most common surprise
with this feature.

Passing include_keychain=True captures that directory too, which makes logged-in
checkpoints work. The device must be shut down for both save and restore: the keychain is
a WAL-mode SQLite database held open by securityd, so a copy taken while booted is torn,
and overwriting it beneath a running securityd does not take effect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from server.config import CONFIG_DIR
from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.app_state")

APP_STATES_DIR = CONFIG_DIR / "app-states"

SIM_DEVICES_ROOT = Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices"

# Keychain lives outside every app container. The basename varies by runtime
# (keychain-2.db, keychain-2-debug.db), so match the family rather than one name.
KEYCHAIN_GLOB = "keychain-2*.db*"


# ---------------------------------------------------------------------------
# Keychain capture
# ---------------------------------------------------------------------------


def _keychain_dir(udid: str) -> Path:
    return SIM_DEVICES_ROOT / udid / "data" / "Library" / "Keychains"


async def get_device_state(udid: str) -> str:
    """Return the simctl state for a device ("Booted", "Shutdown", ...) or "unknown"."""
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "list", "devices", "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return "unknown"
    try:
        listing = json.loads(stdout.decode())
    except json.JSONDecodeError:
        return "unknown"
    for devices in listing.get("devices", {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return device.get("state", "unknown")
    return "unknown"


async def _require_shutdown(udid: str, operation: str) -> None:
    """Raise unless the device is shut down.

    The keychain is a WAL-mode SQLite database held open by securityd. Copying it from a
    booted device yields a torn snapshot, and writing it beneath a running securityd is
    ignored — so both directions require the device to be off.
    """
    state = await get_device_state(udid)
    if state == "Shutdown":
        return
    raise DeviceError(
        f"{operation} needs the device shut down (it is currently {state!r}). "
        f"The simulator keychain is a WAL-mode SQLite database held open by securityd, so "
        f"copying it while booted produces a torn snapshot. "
        f"Run: xcrun simctl shutdown {udid}",
        tool="simctl",
    )


async def _save_keychain(udid: str, checkpoint: Path) -> dict:
    """Copy the simulator keychain into the checkpoint. Device must be shut down."""
    await _require_shutdown(udid, "Saving a checkpoint with include_keychain=True")

    src = _keychain_dir(udid)
    if not src.exists():
        raise DeviceError(
            f"No keychain directory for device {udid} at {src}",
            tool="simctl",
        )

    files = sorted(src.glob(KEYCHAIN_GLOB))
    if not files:
        raise DeviceError(
            f"No keychain database found in {src} (looked for {KEYCHAIN_GLOB})",
            tool="simctl",
        )

    dest = checkpoint / "keychain"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        await asyncio.to_thread(shutil.copy2, str(path), str(dest / path.name))

    logger.info("Captured keychain (%d files) for udid=%s", len(files), udid[:8])
    return {"captured": True, "files": [p.name for p in files]}


async def _restore_keychain(udid: str, checkpoint: Path) -> dict:
    """Copy a checkpoint's keychain back onto the device. Device must be shut down."""
    await _require_shutdown(udid, "Restoring a checkpoint that contains a keychain")

    src = checkpoint / "keychain"
    files = sorted(src.glob(KEYCHAIN_GLOB))
    if not files:
        raise DeviceError(
            f"Checkpoint keychain directory {src} contains no keychain database",
            tool="simctl",
        )

    dest = _keychain_dir(udid)
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        await asyncio.to_thread(shutil.copy2, str(path), str(dest / path.name))

    logger.info("Restored keychain (%d files) for udid=%s", len(files), udid[:8])
    return {"restored": True, "files": [p.name for p in files]}


def checkpoint_has_keychain(bundle_id: str, label: str) -> bool:
    """True if the named checkpoint carries a keychain snapshot."""
    keychain = _checkpoint_dir(bundle_id, label) / "keychain"
    return keychain.is_dir() and any(keychain.glob(KEYCHAIN_GLOB))


# ---------------------------------------------------------------------------
# Container discovery
# ---------------------------------------------------------------------------


async def _read_container_identifier(container_dir: Path) -> str | None:
    """Read MCMMetadataIdentifier from a container's metadata plist, or None."""
    metadata_path = container_dir / ".com.apple.mobile_container_manager.metadata.plist"
    if not metadata_path.exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "plutil", "-convert", "json", "-o", "-", "--", str(metadata_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return json.loads(stdout.decode()).get("MCMMetadataIdentifier")
    except Exception:
        logger.debug("Failed to read metadata for %s", container_dir, exc_info=True)
        return None


async def find_data_container_on_disk(udid: str, bundle_id: str) -> Path | None:
    """Locate the app's data container by scanning the device directory.

    simctl get_app_container only works on a *booted* device ("Unable to lookup in current
    state: Shutdown"), but keychain capture requires the device to be shut down. Scanning
    the metadata plists works in either state, which is what lets a single call save or
    restore containers and keychain together.
    """
    app_root = SIM_DEVICES_ROOT / udid / "data" / "Containers" / "Data" / "Application"
    if not app_root.exists():
        return None
    for container_dir in app_root.iterdir():
        if not container_dir.is_dir():
            continue
        if await _read_container_identifier(container_dir) == bundle_id:
            return container_dir
    return None


async def get_data_container(udid: str, bundle_id: str) -> Path:
    """Return the path to the app's data container.

    Uses simctl when the device is booted, and falls back to a filesystem scan otherwise
    (or when simctl fails), so this works against a shut-down device too.
    """
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "get_app_container", udid, bundle_id, "data",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0:
        path = Path(stdout.decode().strip())
        if path.exists():
            return path

    on_disk = await find_data_container_on_disk(udid, bundle_id)
    if on_disk is not None:
        return on_disk

    detail = stderr.decode().strip() or "container path did not exist"
    raise DeviceError(
        f"Could not get data container for {bundle_id}: {detail}",
        tool="simctl",
    )


async def get_app_groups(udid: str, bundle_id: str) -> dict[str, Path]:
    """Discover app group containers for bundle_id on the given simulator.

    Scans ~/Library/Developer/CoreSimulator/Devices/<udid>/data/Containers/Shared/AppGroup/
    and reads each .com.apple.mobile_container_manager.metadata.plist to find groups
    whose MCMMetadataIdentifier starts with "group." and are associated with bundle_id
    (checked via MCMMetadataIdentifier containing bundle components).

    Returns {group_identifier: path} for all matching groups.

    Note: we match ALL group. containers since there's no reliable way to enumerate
    only the groups for a specific bundle without entitlement data. We return them all
    and let the caller filter by known group IDs.
    """
    sim_devices_root = Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices"
    app_group_root = sim_devices_root / udid / "data" / "Containers" / "Shared" / "AppGroup"

    if not app_group_root.exists():
        return {}

    groups: dict[str, Path] = {}
    metadata_plist_name = ".com.apple.mobile_container_manager.metadata.plist"

    for container_dir in app_group_root.iterdir():
        if not container_dir.is_dir():
            continue
        metadata_path = container_dir / metadata_plist_name
        if not metadata_path.exists():
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                "plutil", "-convert", "json", "-o", "-", "--", str(metadata_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                continue
            meta = json.loads(stdout.decode())
            identifier = meta.get("MCMMetadataIdentifier", "")
            if identifier.startswith("group."):
                groups[identifier] = container_dir
        except Exception:
            logger.debug("Failed to read metadata for %s", container_dir, exc_info=True)

    return groups


async def resolve_container(udid: str, bundle_id: str, container: str) -> Path:
    """Resolve a container name to a live filesystem path.

    container can be:
    - "data" → main data container via simctl
    - "group.<id>" or any group identifier → matched from app groups
    """
    if container == "data":
        return await get_data_container(udid, bundle_id)

    groups = await get_app_groups(udid, bundle_id)
    if container in groups:
        return groups[container]

    raise DeviceError(
        f"Container {container!r} not found for {bundle_id}. "
        f"Available groups: {list(groups.keys())}",
        tool="simctl",
    )


# ---------------------------------------------------------------------------
# Checkpoint save / restore / list / delete
# ---------------------------------------------------------------------------


def _checkpoint_dir(bundle_id: str, label: str) -> Path:
    return APP_STATES_DIR / bundle_id / label


async def _terminate_app(udid: str, bundle_id: str) -> None:
    """Terminate the app, swallowing DeviceError if it's not running."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "terminate", udid, bundle_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        # Ignore non-zero exit: app may not be running
    except Exception:
        pass


async def _copy_container(src: Path, dest: Path) -> None:
    """Copy a container directory to dest, recreating it fresh."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copytree, str(src), str(dest), dirs_exist_ok=True)


async def save_state(
    udid: str,
    bundle_id: str,
    label: str,
    description: str = "",
    include_keychain: bool = False,
) -> dict:
    """Save a named checkpoint of the app's state.

    Terminates the app, copies data container and all app group containers,
    then writes a .quern-meta.json metadata file.

    include_keychain also captures the simulator keychain, which is what makes a
    logged-in checkpoint restorable. It requires the device to be shut down; the
    precondition is checked before anything is written.

    Returns the metadata dict.
    """
    # Check before mutating anything, so a booted device fails cleanly.
    if include_keychain:
        await _require_shutdown(udid, "Saving a checkpoint with include_keychain=True")

    checkpoint = _checkpoint_dir(bundle_id, label)
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    checkpoint.mkdir(parents=True, exist_ok=True)

    # Terminate app before copying
    await _terminate_app(udid, bundle_id)

    # Copy data container
    data_path = await get_data_container(udid, bundle_id)
    data_dest = checkpoint / "data-container"
    await _copy_container(data_path, data_dest)

    # Copy app group containers
    groups = await get_app_groups(udid, bundle_id)
    groups_dest = checkpoint / "app-group"
    if groups:
        groups_dest.mkdir(exist_ok=True)
        for group_id, group_path in groups.items():
            dest = groups_dest / group_id
            await _copy_container(group_path, dest)

    # Copy the keychain (outside every container — this is what carries the login)
    keychain_meta: dict = {"captured": False}
    if include_keychain:
        keychain_meta = await _save_keychain(udid, checkpoint)

    # Write metadata
    captured_at = datetime.now(UTC).isoformat()
    meta = {
        "label": label,
        "description": description,
        "bundle_id": bundle_id,
        "captured_at": captured_at,
        "udid": udid,
        "keychain": keychain_meta,
        "containers": {
            "data": str(data_path),
            "groups": {gid: str(p) for gid, p in groups.items()},
        },
    }
    (checkpoint / ".quern-meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    logger.info("Saved app state %r for %s (udid=%s)", label, bundle_id, udid[:8])
    return meta


async def restore_state(
    udid: str,
    bundle_id: str,
    label: str,
    include_keychain: bool | None = None,
) -> dict:
    """Restore a named checkpoint.

    Terminates the app, wipes each live container, then copies the checkpoint
    contents back using re-resolved live paths (not the paths stored in metadata).

    include_keychain:
      None  (default) — restore the keychain if the checkpoint has one. Requires the
                        device to be shut down; raises before touching anything if not,
                        rather than silently producing a logged-out app.
      True            — require a keychain in the checkpoint and restore it.
      False           — skip the keychain even if present. The app will start logged out.

    Returns the metadata dict, with a "keychain" key describing what was restored.
    """
    checkpoint = _checkpoint_dir(bundle_id, label)
    if not checkpoint.exists():
        raise DeviceError(
            f"Checkpoint {label!r} not found for {bundle_id}",
            tool="simctl",
        )

    meta_path = checkpoint / ".quern-meta.json"
    if not meta_path.exists():
        raise DeviceError(
            f"Checkpoint {label!r} has no metadata file (.quern-meta.json)",
            tool="simctl",
        )
    meta = json.loads(meta_path.read_text())

    has_keychain = checkpoint_has_keychain(bundle_id, label)
    if include_keychain is True and not has_keychain:
        raise DeviceError(
            f"Checkpoint {label!r} has no keychain snapshot, so the login cannot be "
            f"restored. Re-save it with include_keychain=True while the device is shut down.",
            tool="simctl",
        )
    should_restore_keychain = has_keychain and include_keychain is not False

    # Check the precondition before wiping any container, so a booted device fails
    # cleanly instead of leaving a half-restored app.
    if should_restore_keychain:
        await _require_shutdown(udid, "Restoring a checkpoint that contains a keychain")

    # Terminate app before restoring
    await _terminate_app(udid, bundle_id)

    # Restore data container — re-resolve live path (UUID may have rotated)
    data_src = checkpoint / "data-container"
    if data_src.exists():
        live_data = await get_data_container(udid, bundle_id)
        # Wipe live container contents
        for child in live_data.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        # Copy checkpoint back
        await asyncio.to_thread(shutil.copytree, str(data_src), str(live_data), dirs_exist_ok=True)

    # Restore app group containers — re-resolve live paths
    groups_src = checkpoint / "app-group"
    if groups_src.exists():
        live_groups = await get_app_groups(udid, bundle_id)
        for group_dir in groups_src.iterdir():
            if not group_dir.is_dir():
                continue
            group_id = group_dir.name
            if group_id not in live_groups:
                logger.warning("Group %r not found in live simulator, skipping restore", group_id)
                continue
            live_group_path = live_groups[group_id]
            # Wipe live group contents
            for child in live_group_path.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            # Copy checkpoint back
            await asyncio.to_thread(
                shutil.copytree, str(group_dir), str(live_group_path), dirs_exist_ok=True,
            )

    # Restore the keychain last: the container wipe above must not run after it.
    if should_restore_keychain:
        meta["keychain"] = await _restore_keychain(udid, checkpoint)
    else:
        meta["keychain"] = {"restored": False}
        if has_keychain:
            meta["keychain"]["reason"] = "include_keychain=False"
            logger.info("Skipped keychain restore for %r by request", label)
        else:
            meta["keychain"]["reason"] = "checkpoint has no keychain snapshot"
            logger.warning(
                "Checkpoint %r has no keychain snapshot — the app will start logged out. "
                "Re-save with include_keychain=True (device shut down) to capture the login.",
                label,
            )

    logger.info("Restored app state %r for %s (udid=%s)", label, bundle_id, udid[:8])
    return meta


def list_states(bundle_id: str) -> list[dict]:
    """List all saved checkpoints for a bundle_id.

    Returns a list of metadata dicts sorted by captured_at (newest first).
    """
    bundle_dir = APP_STATES_DIR / bundle_id
    if not bundle_dir.exists():
        return []

    results = []
    for label_dir in bundle_dir.iterdir():
        if not label_dir.is_dir():
            continue
        meta_path = label_dir / ".quern-meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            results.append(meta)
        except Exception:
            logger.debug("Failed to read metadata for %s", label_dir, exc_info=True)

    results.sort(key=lambda m: m.get("captured_at", ""), reverse=True)
    return results


def get_checkpoint_plist_path(
    bundle_id: str,
    label: str,
    container: str,
    plist_path: str,
) -> Path:
    """Resolve the path to a plist file inside a saved checkpoint.

    container: "data" for the data container, or a group ID like "group.com.example"
    plist_path: relative path within the container (e.g. "Library/Preferences/com.example.plist")
    """
    checkpoint = _checkpoint_dir(bundle_id, label)
    if not checkpoint.exists():
        raise DeviceError(
            f"Checkpoint {label!r} not found for {bundle_id}",
            tool="simctl",
        )

    if container == "data":
        base = checkpoint / "data-container"
    else:
        base = checkpoint / "app-group" / container

    if not base.exists():
        raise DeviceError(
            f"Container {container!r} not found in checkpoint {label!r}",
            tool="simctl",
        )

    full = base / plist_path
    if not full.exists():
        raise DeviceError(
            f"Plist {plist_path!r} not found in checkpoint {label!r} container {container!r}",
            tool="simctl",
        )

    return full


def delete_state(bundle_id: str, label: str) -> None:
    """Delete a named checkpoint."""
    checkpoint = _checkpoint_dir(bundle_id, label)
    if not checkpoint.exists():
        raise DeviceError(
            f"Checkpoint {label!r} not found for {bundle_id}",
            tool="simctl",
        )
    shutil.rmtree(checkpoint)
    logger.info("Deleted app state %r for %s", label, bundle_id)
