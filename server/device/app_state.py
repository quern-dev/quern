"""App state checkpoint and plist inspection for simulator apps.

Checkpoints are stored in ~/.quern/app-states/<bundle_id>/<label>/.
Each checkpoint directory contains:
  .quern-meta.json         ← metadata (label, description, bundle_id, captured_at, udid)
  data-container/          ← copy of the app's data container
  app-group/<group-id>/    ← one subdir per app group (keyed by group identifier)

Simulator only. Keychain is out of scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.config import CONFIG_DIR
from server.device.plist import read_plist, set_plist_value, remove_plist_key
from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.app_state")

APP_STATES_DIR = CONFIG_DIR / "app-states"


# ---------------------------------------------------------------------------
# Container discovery
# ---------------------------------------------------------------------------


async def get_data_container(udid: str, bundle_id: str) -> Path:
    """Return the path to the app's data container via simctl."""
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "get_app_container", udid, bundle_id, "data",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DeviceError(
            f"Could not get data container for {bundle_id}: {stderr.decode().strip()}",
            tool="simctl",
        )
    path = Path(stdout.decode().strip())
    if not path.exists():
        raise DeviceError(
            f"Data container path does not exist: {path}",
            tool="simctl",
        )
    return path


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
) -> dict:
    """Save a named checkpoint of the app's state.

    Terminates the app, copies data container and all app group containers,
    then writes a .quern-meta.json metadata file.

    Returns the metadata dict.
    """
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

    # Write metadata
    captured_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "label": label,
        "description": description,
        "bundle_id": bundle_id,
        "captured_at": captured_at,
        "udid": udid,
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
) -> dict:
    """Restore a named checkpoint.

    Terminates the app, wipes each live container, then copies the checkpoint
    contents back using re-resolved live paths (not the paths stored in metadata).

    Returns the metadata dict.
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
