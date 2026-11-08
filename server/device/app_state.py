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
import plistlib
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from server.config import CONFIG_DIR
from server.models import AppStateNotFoundError, DeviceError, InvalidAppStatePathError

logger = logging.getLogger(__name__)

APP_STATES_DIR = CONFIG_DIR / "app-states"


# Keychain lives outside every app container. The basename varies by runtime
# (keychain-2.db, keychain-2-debug.db), so match the family rather than one name.
KEYCHAIN_GLOB = "keychain-2*.db*"


# ---------------------------------------------------------------------------
# Keychain capture
# ---------------------------------------------------------------------------


async def _simctl_data_path(udid: str) -> Path | None:
    """The device's data directory as simctl reports it, or None when simctl
    cannot say (it failed, or does not know the device)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "list", "devices", "-j",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        runtimes = json.loads(stdout)["devices"]
        for devices in runtimes.values():
            for device in devices:
                if device.get("udid") == udid and device.get("dataPath"):
                    return Path(device["dataPath"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


async def device_data_dir(udid: str) -> Path:
    """Where the simulator keeps its data, from simctl's `dataPath`.

    Not derived from HOME: a server whose HOME is not the user's -- the CLI
    live test, an agent trial, a VM run -- would look in the wrong place, and
    an app it then cannot find reads as "not installed" (#640). simctl answers
    for a booted or a shut-down device alike. Raises when it cannot say.
    """
    path = await _simctl_data_path(udid)
    if path is None:
        raise DeviceError(
            f"simctl could not say where simulator {udid[:8]} keeps its data",
            tool="simctl",
        )
    return path


async def _keychain_dir(udid: str) -> Path:
    return await device_data_dir(udid) / "Library" / "Keychains"


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


async def _save_keychain(udid: str, checkpoint: Path, src: Path) -> dict:
    """Copy the simulator keychain at `src` into the checkpoint. Device must be
    shut down. `src` is resolved by the caller before anything is touched."""
    await _require_shutdown(udid, "Saving a checkpoint with include_keychain=True")

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


def _checkpoint_keychain_files(checkpoint: Path) -> list[Path]:
    """The keychain files a checkpoint holds; raises if it holds none."""
    src = checkpoint / "keychain"
    files = sorted(src.glob(KEYCHAIN_GLOB))
    if not files:
        raise DeviceError(
            f"Checkpoint keychain directory {src} contains no keychain database",
            tool="simctl",
        )
    return files


async def _restore_keychain(udid: str, files: list[Path], dest: Path) -> dict:
    """Copy a checkpoint's keychain `files` to the device's `dest`. Device must
    be shut down. Both are resolved by the caller before any container is
    replaced, so a lookup that fails cannot leave a half-restored app (#640)."""
    await _require_shutdown(udid, "Restoring a checkpoint that contains a keychain")

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
    app_root = await device_data_dir(udid) / "Containers" / "Data" / "Application"
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


# ---------------------------------------------------------------------------
# App-group entitlements
# ---------------------------------------------------------------------------

APP_GROUPS_ENTITLEMENT = "com.apple.security.application-groups"

_MH_MAGIC = 0xFEEDFACE
_MH_MAGIC_64 = 0xFEEDFACF
_FAT_MAGIC = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_LC_SEGMENT = 0x1
_LC_SEGMENT_64 = 0x19


def _macho_section(data: bytes, base: int, segname: bytes, sectname: bytes) -> bytes | None:
    """The contents of one section of the thin Mach-O image at data[base:], or None.

    Raises ValueError for an image that is not a little-endian Mach-O, or whose
    load commands run past the end of the file.
    """
    magic = int.from_bytes(data[base:base + 4], "little")
    if magic == _MH_MAGIC_64:
        header_size, seg_cmd, seg_size, sect_size = 32, _LC_SEGMENT_64, 72, 80
    elif magic == _MH_MAGIC:
        header_size, seg_cmd, seg_size, sect_size = 28, _LC_SEGMENT, 56, 68
    else:
        raise ValueError(f"not a Mach-O image (magic {magic:#x})")
    wide = magic == _MH_MAGIC_64
    ncmds = int.from_bytes(data[base + 16:base + 20], "little")
    cursor = base + header_size
    for _ in range(ncmds):
        if cursor + 8 > len(data):
            raise ValueError("load commands run past the end of the file")
        cmd = int.from_bytes(data[cursor:cursor + 4], "little")
        cmdsize = int.from_bytes(data[cursor + 4:cursor + 8], "little")
        if cmdsize < 8 or cursor + cmdsize > len(data):
            raise ValueError("malformed load command")
        if cmd == seg_cmd and data[cursor + 8:cursor + 24].rstrip(b"\0") == segname:
            nsects = int.from_bytes(data[cursor + seg_size - 8:cursor + seg_size - 4], "little")
            if seg_size + nsects * sect_size > cmdsize:
                raise ValueError("segment's sections run past its load command")
            sect = cursor + seg_size
            for _ in range(nsects):
                if data[sect:sect + 16].rstrip(b"\0") == sectname:
                    if wide:
                        size = int.from_bytes(data[sect + 40:sect + 48], "little")
                        offset = int.from_bytes(data[sect + 48:sect + 52], "little")
                    else:
                        size = int.from_bytes(data[sect + 36:sect + 40], "little")
                        offset = int.from_bytes(data[sect + 40:sect + 44], "little")
                    start = base + offset
                    if start + size > len(data):
                        raise ValueError("section runs past the end of the file")
                    return data[start:start + size]
                sect += sect_size
        cursor += cmdsize
    return None


def macho_entitlements(binary: Path) -> dict | None:
    """Entitlements embedded in a Mach-O's __TEXT,__entitlements section.

    This is where Xcode puts a simulator build's entitlements: the ad-hoc
    signature it signs with carries none, so codesign reports an empty dict.
    Returns None when no slice has the section. A fat binary's slices are
    built from one entitlements file, so the first slice that has it wins.

    Raises ValueError for a file that is not a Mach-O, or a section that is not
    a property-list dictionary.
    """
    data = binary.read_bytes()
    magic = int.from_bytes(data[:4], "big")
    if magic in (_FAT_MAGIC, _FAT_MAGIC_64):
        count = int.from_bytes(data[4:8], "big")
        entry = 32 if magic == _FAT_MAGIC_64 else 20
        bases = []
        for i in range(count):
            at = 8 + i * entry
            if magic == _FAT_MAGIC_64:
                bases.append(int.from_bytes(data[at + 8:at + 16], "big"))
            else:
                bases.append(int.from_bytes(data[at + 8:at + 12], "big"))
    else:
        bases = [0]
    for base in bases:
        section = _macho_section(data, base, b"__TEXT", b"__entitlements")
        if section is not None:
            try:
                parsed = plistlib.loads(section.rstrip(b"\0"))
            except Exception as e:
                raise ValueError(f"__entitlements is not a property list: {e}") from e
            if not isinstance(parsed, dict):
                raise ValueError(f"__entitlements is a {type(parsed).__name__}, not a dictionary")
            return parsed
    return None


async def _signature_entitlements(path: Path) -> dict:
    """Entitlements in the code signature: where a device-signed build has them.

    An empty dict when the signature has none -- an Xcode simulator build's
    ad-hoc signature -- or when codesign confirms there is no signature at all,
    which a simulator installs but will not launch (measured, iOS 27.0): that
    is an answer, and the embedded section still counts (CodeRabbit). Raises
    ValueError for any other failure, which is "could not tell", never "no
    entitlements".
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "codesign", "-d", "--entitlements", "-", "--xml", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except OSError as e:
        raise ValueError(f"codesign could not run: {e}") from e
    message = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        if "code object is not signed at all" in message:
            return {}
        raise ValueError(f"codesign failed: {message}")
    if not stdout.strip():
        return {}
    try:
        parsed = plistlib.loads(stdout)
    except Exception as e:
        raise ValueError(f"codesign's entitlements are not a property list: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"codesign's entitlements are a {type(parsed).__name__}")
    return parsed


async def find_app_bundle_on_disk(udid: str, bundle_id: str) -> Path | None:
    """The installed .app for bundle_id, found by scanning bundle containers.

    Like find_data_container_on_disk, this works on a shut-down device, which
    a keychain checkpoint requires.
    """
    bundle_root = await device_data_dir(udid) / "Containers" / "Bundle" / "Application"
    if not bundle_root.exists():
        return None
    for container_dir in bundle_root.iterdir():
        if not container_dir.is_dir():
            continue
        if await _read_container_identifier(container_dir) == bundle_id:
            apps = sorted(container_dir.glob("*.app"))
            return apps[0] if apps else None
    return None


def _bundle_executables(app: Path) -> list[Path]:
    """The app's executable and each embedded extension's: an extension can be
    entitled to a group the app itself is not, and shares the app's data.

    Raises ValueError for any bundle whose executable cannot be found, so an
    unreadable extension cannot silently drop the groups only it names.
    """
    bundles = [app]
    for folder in ("PlugIns", "Extensions"):
        bundles += sorted((app / folder).glob("*.appex"))
    executables = []
    for bundle in bundles:
        try:
            with (bundle / "Info.plist").open("rb") as f:
                info = plistlib.load(f)
        except Exception as e:
            raise ValueError(f"{bundle.name}'s Info.plist cannot be read: {e}") from e
        name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
        if not isinstance(name, str) or not (bundle / name).is_file():
            raise ValueError(f"{bundle.name} has no executable")
        executables.append(bundle / name)
    return executables


async def entitled_app_groups(udid: str, bundle_id: str) -> set[str]:
    """The app-group identifiers bundle_id and its extensions are entitled to.

    Raises DeviceError when the app is not installed or an executable cannot be
    read. Callers must not fall back to every group on the device: a restore
    wipes each group it is given.
    """
    app = await find_app_bundle_on_disk(udid, bundle_id)
    if app is None:
        raise AppStateNotFoundError(
            f"{bundle_id} is not installed on {udid[:8]}, so its app groups are unknown",
            tool="simctl",
        )
    groups: set[str] = set()
    try:
        for executable in _bundle_executables(app):
            embedded = await asyncio.to_thread(macho_entitlements, executable)
            signed = await _signature_entitlements(executable.parent)
            # A simulator build carries its groups in the section, a device
            # build in the signature. Where both name them and disagree, which
            # one the system honours is not something to guess at: a group
            # taken from the wrong one is wiped on restore (CodeRabbit).
            lists = []
            for source in (embedded or {}, signed):
                if APP_GROUPS_ENTITLEMENT not in source:
                    continue
                value = source[APP_GROUPS_ENTITLEMENT]
                if not isinstance(value, list) or not all(isinstance(g, str) for g in value):
                    raise ValueError(f"{executable.name}'s {APP_GROUPS_ENTITLEMENT} is not "
                                     f"a list of strings")
                lists.append(set(value))
            if len(lists) == 2 and lists[0] != lists[1]:
                raise ValueError(
                    f"{executable.name}'s embedded and signed {APP_GROUPS_ENTITLEMENT} "
                    f"disagree ({sorted(lists[0])} vs {sorted(lists[1])})")
            for value in lists:
                groups.update(value)
    except (OSError, ValueError) as e:
        raise DeviceError(
            f"Could not read {bundle_id}'s entitlements, so its app groups are unknown: {e}",
            tool="quern",
        ) from e
    return groups


async def get_app_groups(udid: str, bundle_id: str) -> dict[str, Path]:
    """The app group containers bundle_id is entitled to, as {group_identifier: path}.

    Scans the device's Containers/Shared/AppGroup/ (under simctl's dataPath)
    and keeps the containers whose MCMMetadataIdentifier is in the app's (or an
    extension's) com.apple.security.application-groups entitlement. Every other
    app's groups live in the same directory, and a restore wipes each group it
    is given, so this never returns more than the app is entitled to.

    An entitled group with no container yet (the app has not used it) is absent.
    Raises DeviceError when the entitlements cannot be read; see
    entitled_app_groups.
    """
    _, groups = await _app_groups(udid, bundle_id)
    return groups


async def _app_groups(udid: str, bundle_id: str) -> tuple[set[str], dict[str, Path]]:
    """The groups bundle_id is entitled to, and those of them with a container."""
    app_group_root = await device_data_dir(udid) / "Containers" / "Shared" / "AppGroup"

    entitled = await entitled_app_groups(udid, bundle_id)
    if not entitled or not app_group_root.exists():
        return entitled, {}

    groups: dict[str, Path] = {}
    for container_dir in app_group_root.iterdir():
        if not container_dir.is_dir():
            continue
        identifier = await _read_container_identifier(container_dir)
        if identifier in entitled:
            groups[identifier] = container_dir

    return entitled, groups


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

    raise AppStateNotFoundError(
        f"Container {container!r} not found for {bundle_id}. "
        f"Available groups: {list(groups.keys())}",
        tool="simctl",
    )


# ---------------------------------------------------------------------------
# Checkpoint save / restore / list / delete
# ---------------------------------------------------------------------------


def checked_name(kind: str, value: str) -> str:
    """`value` if it is one plain path segment, else `InvalidAppStatePathError`.

    Bundle ids, labels and group ids become directory names under
    `APP_STATES_DIR`, and delete is an `rmtree` of the result. Unchecked,
    `bundle_id="../.."` with an existing label named a directory beside
    `~/.quern`, and `label=".."` named the whole store for that bundle. A
    leading dot is refused too: dot-names are how an in-progress save is
    hidden from `list_states`, so a checkpoint called `.x` was saved and then
    never listed.
    """
    if (
        not value
        or value.startswith(".")
        or any(c in value for c in ("/", "\\", "\0"))
    ):
        raise InvalidAppStatePathError(
            f"{kind} {value!r} is not usable as a name: it must be a single path "
            "segment, not empty, not starting with '.', with no '/', '\\' or NUL",
            tool="quern",
        )
    return value


def contained_path(base: Path, relative: str, kind: str = "plist_path") -> Path:
    """`base / relative`, refused if it resolves outside `base`.

    `relative` comes from the caller, so `../../..` or an absolute path would
    otherwise read or rewrite any plist on the Mac, not just the app's.
    """
    root = base.resolve()
    full = (base / relative).resolve()
    if not full.is_relative_to(root):
        raise InvalidAppStatePathError(
            f"{kind} {relative!r} leaves its container", tool="quern",
        )
    return full


def _checkpoint_dir(bundle_id: str, label: str) -> Path:
    return APP_STATES_DIR / checked_name("bundle_id", bundle_id) / checked_name("label", label)


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


async def _save_into(
    checkpoint: Path,
    udid: str,
    bundle_id: str,
    label: str,
    description: str,
    include_keychain: bool,
) -> dict:
    from server.device.live_plist import capture_preferences

    # Resolve before terminating: a lookup that fails here must leave the app
    # as it was, not killed for a checkpoint that was never taken (#640).
    data_path = await get_data_container(udid, bundle_id)
    groups = await get_app_groups(udid, bundle_id)
    keychain_src = await _keychain_dir(udid) if include_keychain else None

    await _terminate_app(udid, bundle_id)

    # Copy data container, then replace its preference files with what
    # cfprefsd holds: the files on disk trail the app by seconds.
    data_dest = checkpoint / "data-container"
    await _copy_container(data_path, data_dest)
    await capture_preferences(udid, data_path, data_dest, f"{bundle_id}.plist")

    # Copy app group containers
    groups_dest = checkpoint / "app-group"
    if groups:
        groups_dest.mkdir(exist_ok=True)
        for group_id, group_path in groups.items():
            dest = groups_dest / group_id
            await _copy_container(group_path, dest)
            await capture_preferences(udid, group_path, dest, f"{group_id}.plist")

    # Copy the keychain (outside every container — this is what carries the login)
    keychain_meta: dict = {"captured": False}
    if keychain_src is not None:
        keychain_meta = await _save_keychain(udid, checkpoint, keychain_src)

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
    return meta


async def save_state(
    udid: str,
    bundle_id: str,
    label: str,
    description: str = "",
    include_keychain: bool = False,
) -> dict:
    """Save a named checkpoint of the app's state.

    Terminates the app, copies its data container and the app group containers
    it is entitled to (see get_app_groups),
    then writes a .quern-meta.json metadata file.

    include_keychain also captures the simulator keychain, which is what makes a
    logged-in checkpoint restorable. It requires the device to be shut down; the
    precondition is checked before anything is written.

    Returns the metadata dict.
    """
    # Check before mutating anything, so a booted device fails cleanly.
    if include_keychain:
        await _require_shutdown(udid, "Saving a checkpoint with include_keychain=True")

    final = _checkpoint_dir(bundle_id, label)
    # Built beside the final directory and swapped in only once complete. The
    # old order deleted an existing checkpoint first, so a save that failed
    # part-way -- an app that is not installed is enough -- destroyed the
    # checkpoint it was replacing and left an empty directory in its place.
    # Fixed-length names, so a label near the filesystem's limit does not fail
    # here having worked before staging existed.
    token = uuid.uuid4().hex[:8]
    checkpoint = final.with_name(f".saving-{token}")
    displaced = final.with_name(f".replaced-{token}")
    try:
        checkpoint.mkdir(parents=True)
        meta = await _save_into(checkpoint, udid, bundle_id, label, description, include_keychain)
        # Move the old one aside rather than deleting it first, so there is no
        # moment at which neither copy exists.
        if final.exists():
            final.rename(displaced)
        checkpoint.rename(final)
    except OSError as e:
        if displaced.exists() and not final.exists():
            displaced.rename(final)
        raise DeviceError(f"Saving checkpoint {label!r} failed: {e}", tool="quern") from e
    finally:
        for leftover in (checkpoint, displaced):
            if leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)

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
        raise AppStateNotFoundError(
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

    from server.device.live_plist import preference_plists, push_preferences

    # Resolve every live path before wiping anything: a lookup that fails
    # after the data container was overwritten leaves a half-restored app.
    # Re-resolved rather than read from the metadata, as UUIDs rotate.
    data_src = checkpoint / "data-container"
    groups_src = checkpoint / "app-group"
    live_data = await get_data_container(udid, bundle_id) if data_src.exists() else None
    entitled: set[str] = set()
    live_groups: dict[str, Path] = {}
    if groups_src.exists():
        entitled, live_groups = await _app_groups(udid, bundle_id)
    keychain_files: list[Path] = []
    keychain_dest: Path | None = None
    if should_restore_keychain:
        keychain_files = _checkpoint_keychain_files(checkpoint)
        keychain_dest = await _keychain_dir(udid)

    await _terminate_app(udid, bundle_id)
    problems: list[str] = []

    if live_data is not None:
        prefs_before = [p.name for p in preference_plists(live_data)]
        # Wipe live container contents
        for child in live_data.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        # Copy checkpoint back
        await asyncio.to_thread(shutil.copytree, str(data_src), str(live_data), dirs_exist_ok=True)
        problems += await push_preferences(
            udid, live_data, prefs_before, f"{bundle_id}.plist",
        )

    if groups_src.exists():
        # Checkpoints saved before #628 hold every group on the device. Only
        # the app's own are put back; restoring the rest would roll back other
        # apps' data.
        not_entitled: list[str] = []
        missing: list[str] = []
        for group_dir in groups_src.iterdir():
            if not group_dir.is_dir():
                continue
            group_id = group_dir.name
            if group_id not in entitled:
                not_entitled.append(group_id)
                continue
            if group_id not in live_groups:
                missing.append(group_id)
                continue
            live_group_path = live_groups[group_id]
            group_prefs_before = [p.name for p in preference_plists(live_group_path)]
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
            problems += await push_preferences(
                udid, live_group_path, group_prefs_before, f"{group_id}.plist",
            )
        if not_entitled:
            logger.info(
                "Restore of %r skipped %d group(s) %s is not entitled to: %s",
                label, len(not_entitled), bundle_id, ", ".join(sorted(not_entitled)),
            )
            meta["groups_not_entitled"] = sorted(not_entitled)
        if missing:
            # The app's own data, not restored: say so where the caller looks.
            logger.warning(
                "Restore of %r could not put back %s's group(s) %s: no container on this "
                "device. Launch the app once so it creates them, then restore again.",
                label, bundle_id, ", ".join(sorted(missing)),
            )
            meta["groups_not_restored"] = {
                "groups": sorted(missing),
                "reason": (
                    "the app is entitled to these groups but they have no container on "
                    "this device yet; launch the app once so it creates them, then "
                    "restore again"
                ),
            }

    # Restore the keychain last: the container wipe above must not run after it.
    if should_restore_keychain:
        meta["keychain"] = await _restore_keychain(udid, keychain_files, keychain_dest)
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

    meta["preferences"] = {"synced": not problems}
    if problems:
        logger.warning("Restored %r, but cfprefsd disagrees: %s", label, problems)
        meta["preferences"].update({
            "detail": problems,
            "warning": (
                "the files were restored, but cfprefsd still serves other values for "
                "some preferences, so the app may not see the restored state"
            ),
        })

    logger.info("Restored app state %r for %s (udid=%s)", label, bundle_id, udid[:8])
    return meta


def list_states(bundle_id: str) -> list[dict]:
    """List all saved checkpoints for a bundle_id.

    Returns a list of metadata dicts sorted by captured_at (newest first).
    """
    bundle_dir = APP_STATES_DIR / checked_name("bundle_id", bundle_id)
    if not bundle_dir.exists():
        return []

    results = []
    for label_dir in bundle_dir.iterdir():
        # A dot-name is a save still being written (see `save_state`).
        if not label_dir.is_dir() or label_dir.name.startswith("."):
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
        raise AppStateNotFoundError(
            f"Checkpoint {label!r} not found for {bundle_id}",
            tool="simctl",
        )

    if container == "data":
        base = checkpoint / "data-container"
    else:
        base = checkpoint / "app-group" / checked_name("container", container)

    if not base.exists():
        raise AppStateNotFoundError(
            f"Container {container!r} not found in checkpoint {label!r}",
            tool="simctl",
        )

    full = contained_path(base, plist_path)
    if not full.exists():
        raise AppStateNotFoundError(
            f"Plist {plist_path!r} not found in checkpoint {label!r} container {container!r}",
            tool="simctl",
        )

    return full


def delete_state(bundle_id: str, label: str) -> None:
    """Delete a named checkpoint."""
    checkpoint = _checkpoint_dir(bundle_id, label)
    if not checkpoint.exists():
        raise AppStateNotFoundError(
            f"Checkpoint {label!r} not found for {bundle_id}",
            tool="simctl",
        )
    shutil.rmtree(checkpoint)
    logger.info("Deleted app state %r for %s", label, bundle_id)
