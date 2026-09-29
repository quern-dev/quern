"""A record of each build quern makes, so a crash can be matched to it (#326).

`build_and_install` builds every scheme into one DerivedData directory, and the
next build overwrites it. So the binary a phone is running, and the object files
its debug information lives in, are gone by the time its crash is read. A
record keeps, per build: what was built (bundle id, version, configuration,
platform), where, and each binary's UUID, which is how a crash report names the
binaries it ran.

For a device build it also keeps dSYMs of the app's own code, made straight
after the build while DerivedData still matches it. A Debug build has no dSYM
of its own (`DEBUG_INFORMATION_FORMAT=dwarf`): its DWARF stays in the object
files, which the next build replaces. Measured on Geocaching: about 6 s and
185 MB per build, and `atos` then resolves a phone's crash to
`AppDelegate.swift:13`. A simulator build gets the record alone, because macOS
already writes file and line into a simulator's crash report.

Records live in `~/.quern/build-records/<build id>/`: `record.json` and
`dSYMs/`. Each is assembled under `<build id>.partial/` and renamed into place
only when complete, so a record that exists is whole.
"""

from __future__ import annotations

import asyncio
import logging
import plistlib
import shutil
import uuid as uuid_mod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.config import CONFIG_DIR
from server.device import macho
from server.models import BuildBinary, BuildRecord

logger = logging.getLogger(__name__)

RECORDS_DIR = CONFIG_DIR / "build-records"
#: dSYMs are kept for this many of the newest device builds of each scheme --
#: about 1.8 GB for Geocaching. An older record stays, marked as expired.
KEEP_DSYMS_PER_SCHEME = 10
#: Records themselves are a few KB, kept as long as crash copies are.
RECORD_RETENTION_DAYS = 30
DSYMUTIL_TIMEOUT = 300  # s; Geocaching's 117 MB .debug.dylib takes 5.4
_PARTIAL = ".partial"

#: `(argv) -> (exit code, stderr)`. Injected by tests, which never run Xcode.
Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def _run(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=DSYMUTIL_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, err.decode(errors="replace")


async def record_build(
    app_path: Path, *, project_path: str, scheme: str, configuration: str,
    platform: str, root: Path | None = None, run: Runner | None = None,
    now: datetime | None = None,
) -> BuildRecord:
    """Record the build of `app_path` and return the record.

    Never raises for anything a build can leave behind: a binary that cannot be
    read, a dSYM that cannot be made, a directory that cannot be written. Each
    is said on the record instead, because a build that installed must not be
    reported as failed over its symbols -- and its symbols failing must not be
    reported as nothing at all.
    """
    root = root or RECORDS_DIR
    run = run or _run            # looked up now, so a test's patch applies
    created = now or datetime.now(UTC)
    build_id = f"{created:%Y%m%d-%H%M%S}-{platform}-{uuid_mod.uuid4().hex[:6]}"
    record = BuildRecord(
        build_id=build_id, created_at=created, project_path=project_path,
        scheme=scheme, configuration=configuration, platform=platform,
        app_path=str(app_path),
    )
    # Blocking file work -- a walk of the bundle, a 117 MB mmap -- kept off the
    # event loop that is serving the installs meanwhile.
    await asyncio.to_thread(_read_info, app_path, record)
    record.binaries = await asyncio.to_thread(_binaries, app_path)

    partial, final = root / (build_id + _PARTIAL), root / build_id
    try:
        partial.mkdir(parents=True)
        if platform == "iphoneos":
            # Written under `.partial`, recorded under the name it will have.
            await _keep_dsyms(app_path, record, partial / "dSYMs", final / "dSYMs", run)
        (partial / "record.json").write_text(record.model_dump_json(indent=2))
        partial.rename(final)
    except OSError as e:
        shutil.rmtree(partial, ignore_errors=True)
        for b in record.binaries:
            b.dsym = ""
        record.error = f"the record could not be written to {root}: {e}"
        logger.warning("Build record %s not written: %s", build_id, e)
    return record


def save(record: BuildRecord, root: Path | None = None) -> None:
    """Rewrite a record that is already on disk (after its installs, say)."""
    path = (root or RECORDS_DIR) / record.build_id / "record.json"
    if not path.parent.is_dir():
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(record.model_dump_json(indent=2))
    tmp.replace(path)


def _read_info(app_path: Path, record: BuildRecord) -> None:
    try:
        with open(app_path / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError) as e:
        record.notes.append(f"Info.plist could not be read: {e}")
        return
    record.bundle_id = str(info.get("CFBundleIdentifier") or "")
    record.version = str(info.get("CFBundleShortVersionString") or "")
    record.build_number = str(info.get("CFBundleVersion") or "")


def _binaries(app_path: Path) -> list[BuildBinary]:
    out = []
    for path in sorted(app_path.rglob("*")):
        if path.is_symlink() or not path.is_file() or not macho.is_macho(path):
            continue
        m = macho.read(path)
        if m is None:
            continue
        out.append(BuildBinary(
            path=str(path.relative_to(app_path)), uuids=m.uuids,
            built_here=_objects_here(m),
        ))
    return out


def _objects_here(m: macho.MachO) -> bool:
    """Whether this build compiled the binary: its debug map names object
    files that exist on this Mac. A vendored framework's map names its
    vendor's build machine."""
    for s in m.slices:
        for obj in s.debug_objects:
            # An archive member is written `lib.a(member.o)`.
            if Path(obj.split("(", 1)[0]).exists():
                return True
    return False


async def _keep_dsyms(
    app_path: Path, record: BuildRecord, out: Path, recorded_as: Path, run: Runner,
) -> None:
    existing = await asyncio.to_thread(_existing_dsyms, app_path.parent)
    copied: dict[Path, str] = {}
    for binary in record.binaries:
        uuids = set(binary.uuids.values())
        have = next((d for d, ids in existing.items() if uuids and uuids <= ids), None)
        # A dSYM Xcode made (dwarf-with-dsym, or one a vendored framework
        # ships) is exact when its UUIDs match, and `dsymutil` could not do
        # better. One can cover several binaries -- `MyApp.app.dSYM` holds the
        # app and its .debug.dylib -- so it is copied once, not once each.
        name = have.name if have is not None else binary.path.replace("/", "__") + ".dSYM"
        target = out / name
        try:
            if have is not None and have in copied:
                binary.dsym = copied[have]
                continue
            if have is not None:
                out.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copytree, have, target)
                copied[have] = str(recorded_as / name)
            elif binary.built_here:
                out.mkdir(parents=True, exist_ok=True)
                code, err = await run(
                    ["xcrun", "dsymutil", str(app_path / binary.path), "-o", str(target)])
                if code != 0:
                    raise RuntimeError(f"dsymutil exited {code}: {_first_line(err)}")
                warnings = [w for w in err.splitlines() if "warning" in w.lower()]
                if warnings:
                    # Most likely an object file replaced mid-read by another
                    # build of the scheme: the dSYM is kept, and said to be
                    # incomplete rather than presented as whole.
                    binary.dsym_warnings = warnings[:3]
            else:
                continue
        except (OSError, RuntimeError, TimeoutError) as e:
            shutil.rmtree(target, ignore_errors=True)
            binary.dsym_error = str(e) or type(e).__name__
            continue
        binary.dsym = str(recorded_as / name)


def _existing_dsyms(products: Path) -> dict[Path, set[str]]:
    """The products directory's dSYMs, by the UUIDs they cover."""
    found: dict[Path, set[str]] = {}
    for dsym in products.glob("*.dSYM"):
        ids: set[str] = set()
        for dwarf in (dsym / "Contents" / "Resources" / "DWARF").glob("*"):
            m = macho.read(dwarf)
            if m is not None:
                ids |= set(m.uuids.values())
        if ids:
            found[dsym] = ids
    return found


def _first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "no output")


def load_all(root: Path | None = None) -> list[BuildRecord]:
    """Every complete record, newest first. An unreadable one is skipped."""
    root = root or RECORDS_DIR
    records = []
    for path in root.glob("*/record.json"):
        if path.parent.name.endswith(_PARTIAL):
            continue
        try:
            records.append(BuildRecord.model_validate_json(path.read_text()))
        except (OSError, ValueError) as e:
            logger.warning("Skipping unreadable build record %s: %s", path, e)
    return sorted(records, key=lambda r: r.created_at, reverse=True)


def prune(root: Path | None = None, *, now: datetime | None = None,
          keep: int = KEEP_DSYMS_PER_SCHEME,
          max_age_days: int = RECORD_RETENTION_DAYS) -> list[str]:
    """Apply retention; return what was removed, one line each.

    dSYMs beyond the newest `keep` device builds of a scheme go, and their
    record says so. Records older than `max_age_days` go entirely. A `.partial`
    directory older than an hour is a record whose build died mid-way.
    """
    root = root or RECORDS_DIR
    now = now or datetime.now(UTC)
    removed: list[str] = []
    for partial in root.glob("*" + _PARTIAL):
        try:
            if now.timestamp() - partial.stat().st_mtime > 3600:
                shutil.rmtree(partial)
                removed.append(f"{partial.name} (unfinished)")
        except OSError as e:
            logger.warning("Could not remove %s: %s", partial, e)

    cutoff = now - timedelta(days=max_age_days)
    kept_per_scheme: dict[tuple[str, str], int] = {}
    for record in load_all(root):
        directory = root / record.build_id
        if max_age_days > 0 and record.created_at < cutoff:
            try:
                shutil.rmtree(directory)
                removed.append(f"{record.build_id} (older than {max_age_days} days)")
            except OSError as e:
                logger.warning("Could not remove %s: %s", directory, e)
            continue
        if record.platform != "iphoneos" or not any(b.dsym for b in record.binaries):
            continue
        key = (record.project_path, record.scheme)
        kept_per_scheme[key] = kept_per_scheme.get(key, 0) + 1
        if kept_per_scheme[key] <= keep:
            continue
        try:
            shutil.rmtree(directory / "dSYMs")
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not remove dSYMs of %s: %s", record.build_id, e)
            continue
        for b in record.binaries:
            b.dsym = ""
        record.dsyms_expired = True
        try:
            save(record, root)
        except OSError as e:
            logger.warning("Could not update %s: %s", record.build_id, e)
        removed.append(f"{record.build_id} dSYMs (beyond the newest {keep} of {record.scheme})")
    return removed


def summary_line(record: BuildRecord) -> str:
    """One line for the build summary an MCP caller reads."""
    what = " ".join(x for x in (record.bundle_id, record.version,
                                f"({record.build_number})" if record.build_number else "") if x)
    head = f"Recorded build {record.build_id}: {what or 'unknown app'}, {record.configuration}"
    if record.error:
        return f"{head} -- {record.error}."
    if record.platform != "iphoneos":
        return f"{head}, {len(record.binaries)} binaries' UUIDs."
    kept = [b for b in record.binaries if b.dsym]
    failed = [b for b in record.binaries if b.dsym_error]
    parts = [f"{head}; symbols kept for {len(kept)} binar{'y' if len(kept) == 1 else 'ies'}"]
    if failed:
        parts.append(f"not for {', '.join(b.path for b in failed)} ({failed[0].dsym_error})")
    if any(b.dsym_warnings for b in kept):
        parts.append("some may be incomplete (dsymutil warned)")
    if not kept and not failed:
        parts.append("none of its binaries were built from source here")
    return "; ".join(parts) + "."
