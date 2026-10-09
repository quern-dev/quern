"""A record of each build quern makes, so a crash can be matched to it (#326).

`build_and_install` builds each project's scheme into one DerivedData directory
per platform, and the next build overwrites it. So the binary a phone is
running, and the object files its debug information lives in, are gone by the
time its crash is read. A
record keeps, per build: what was built (bundle id, version, configuration,
platform), where, and each binary's UUID, which is how a crash report names the
binaries it ran.

For a device build it also keeps dSYMs, straight after the build while
DerivedData still matches it. Where Xcode made one (`dwarf-with-dsym`, or a
vendored framework's) and its UUIDs match, it is copied: the production app
measured here does, and recording takes 0.4 s and about 200 MB. Where it did not
(`DEBUG_INFORMATION_FORMAT=dwarf`, the Debug default), the DWARF is still in
the object files the next build replaces, and `dsymutil` makes one: about 6 s
and 185 MB for a 117 MB .debug.dylib. Either way `atos` then
resolves a phone's crash to `AppDelegate.swift:13`. A simulator build gets the
record alone, because macOS already writes file and line into a simulator's
crash report.

Records live in `~/.quern/build-records/<build id>/`: `record.json` and
`dSYMs/`. Each is assembled under `<build id>.partial/` and renamed into place
only when complete, so a record that exists is whole.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import re
import shutil
import uuid as uuid_mod
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.builds import elf, macho
from server.config import CONFIG_DIR
from server.models import BuildBinary, BuildRecord

logger = logging.getLogger(__name__)

RECORDS_DIR = CONFIG_DIR / "build-records"
#: dSYMs are kept for this many of the newest device builds of each scheme --
#: about 1.8 GB for the production app measured. An older record stays, marked as expired.
KEEP_DSYMS_PER_SCHEME = 10
#: Records themselves are a few KB, kept as long as crash copies are.
RECORD_RETENTION_DAYS = 30
DSYMUTIL_TIMEOUT = 300  # s; a 117 MB .debug.dylib took 5.4
_PARTIAL = ".partial"

#: A `.partial` untouched this long is a recording that died. Generous: a
#: recording can run several `dsymutil`s of up to DSYMUTIL_TIMEOUT each, and
#: only the directory's own mtime is looked at.
STALE_PARTIAL_SECONDS = 6 * 3600

#: `(argv) -> (exit code, stderr)`. Injected by tests, which never run Xcode.
Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def _run(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=DSYMUTIL_TIMEOUT)
    except BaseException:
        # A timeout, or the request cancelled (a server restart mid-build):
        # either way the child is not left running on its own.
        if proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())
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
    except asyncio.CancelledError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(partial, ignore_errors=True)
        for b in record.binaries:
            b.dsym = b.dwarf = ""
        record.error = f"the record could not be written to {root}: {e}"
        logger.warning("Build record %s not written: %s", build_id, e)
    return record


class AndroidBuildNotFound(ValueError):
    """The variant has no APK output to record, said with what there is."""


#: `# pg_map_id: e1ad14241ecb…` in mapping.txt's header.
_MAP_ID = re.compile(r"^# pg_map_id: ([0-9a-f]+)\s*$")


async def record_android_build(
    module_dir: Path, variant: str, *, root: Path | None = None, now: datetime | None = None,
    just_built: bool = False,
) -> BuildRecord:
    """Record a Gradle build of `variant` in `module_dir` (the app module).

    quern does not run Gradle (#347), so the agent builds and then records:
    the APK's package and version from AGP's `output-metadata.json`, R8's
    `mapping.txt` with its `pg_map_id` when the variant is minified, and the
    unstripped native libraries from `merged_native_libs`, each by the BuildId
    a tombstone names it by. All copied, because the next build overwrites
    them. Raises `AndroidBuildNotFound` when the variant has no APK output;
    anything a build can leave behind short of that is said on the record.
    """
    root = root or RECORDS_DIR
    created = now or datetime.now(UTC)
    metadata = await asyncio.to_thread(_android_metadata, module_dir, variant)
    elements = metadata["elements"]
    element = elements[0]
    build_id = f"{created:%Y%m%d-%H%M%S}-android-{uuid_mod.uuid4().hex[:6]}"
    record = BuildRecord(
        build_id=build_id, created_at=created, project_path=str(module_dir), scheme=variant,
        configuration=variant, platform="android",
        app_path=str(metadata["_dir"] / str(element.get("outputFile") or "")),
        bundle_id=str(metadata.get("applicationId") or ""),
        version=str(element.get("versionName") or ""),
        build_number=str(element.get("versionCode") or ""),
        version_codes=sorted({str(e["versionCode"]) for e in elements if e.get("versionCode")}),
    )
    # `just_built`: `build_and_install` ran Gradle a moment ago, which rebuilt
    # what the source changed and left the rest up to date -- so an APK from
    # last week is this source's APK, not a stale one.
    stale = None if just_built else _stale_outputs(Path(record.app_path), created)
    if stale:
        record.notes.append(stale)
    partial, final = root / (build_id + _PARTIAL), root / build_id
    try:
        partial.mkdir(parents=True)
        await asyncio.to_thread(_keep_android_symbols, module_dir, variant, record,
                                partial / "symbols", final / "symbols")
        (partial / "record.json").write_text(record.model_dump_json(indent=2))
        partial.rename(final)
    except asyncio.CancelledError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(partial, ignore_errors=True)
        for b in record.binaries:
            b.dsym = b.dwarf = ""
        record.mapping = record.mapping_id = ""
        record.error = f"the record could not be written to {root}: {e}"
        logger.warning("Build record %s not written: %s", build_id, e)
    return record


#: Outputs older than this, when recorded, are said to be. Recording is meant to
#: follow the build it records; an agent recording whatever `build/` held gets
#: the build from weeks ago, and its mapping retraces to lines that have moved.
STALE_OUTPUTS = timedelta(hours=1)


def _stale_outputs(apk: Path, now: datetime) -> str:
    try:
        built = datetime.fromtimestamp(apk.stat().st_mtime, UTC)
    except OSError:
        return f"the APK its output-metadata.json names, {apk.name}, is not there"
    age = now - built
    if age <= STALE_OUTPUTS:
        return ""
    hours = round(age.total_seconds() / 3600)
    ago = f"{age.days} days" if age.days >= 2 else f"{hours} hour{'' if hours == 1 else 's'}"
    return (f"these outputs were built {built:%Y-%m-%d %H:%M} UTC, {ago} before they were "
            f"recorded: if the source has changed since, build again and record that, "
            f"or its lines will not match the source")


def _android_metadata(module_dir: Path, variant: str, *, after_build: bool = False) -> dict:
    """The variant's output-metadata.json, with `_dir` set to its directory.

    `after_build`: quern has just run the build, so "build the variant first"
    is the wrong advice and the module is the likelier mistake.
    """
    outputs = module_dir / "build" / "outputs" / "apk"
    seen = []
    for path in sorted(outputs.rglob("output-metadata.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("variantName") or "")
        seen.append(name)
        # Gradle matches task names case-insensitively, so `stagingdebug`
        # builds stagingDebug; the outputs must be found the same way.
        if name.lower() == variant.lower():
            elements = data.get("elements")
            if (not isinstance(elements, list) or not elements
                    or not all(isinstance(e, dict) for e in elements)):
                raise AndroidBuildNotFound(
                    f"{path} names variant {variant!r} but lists no APKs in the form AGP "
                    f"writes: build the variant again")
            data["_dir"] = path.parent
            return data
    if not outputs.is_dir():
        if after_build:
            raise AndroidBuildNotFound(
                f"Gradle reported success and wrote no APK outputs under {outputs}: is "
                f"{module_dir.name} the application module? Pass module= naming the one "
                f"that applies com.android.application")
        raise AndroidBuildNotFound(
            f"no APK outputs under {outputs}: build the variant first, and pass the app "
            f"module's directory (the one with build.gradle), not the project root")
    # `debug` on a flavoured project assembles every flavour's debug variant,
    # none of which is called `debug`: name the ones it did build.
    matches = sorted({n for n in seen if n.lower().endswith(variant.lower())})
    if matches:
        raise AndroidBuildNotFound(
            f"{variant!r} is not one variant here but every flavour's: pass one of "
            f"{', '.join(matches)}")
    raise AndroidBuildNotFound(
        f"no APK output for variant {variant!r} under {outputs}; built variants: "
        f"{', '.join(sorted(set(seen))) or 'none'}")


def _keep_android_symbols(
    module_dir: Path, variant: str, record: BuildRecord, out: Path, recorded_as: Path,
) -> None:
    mapping = module_dir / "build" / "outputs" / "mapping" / variant / "mapping.txt"
    built_with = _apk_map_id(Path(record.app_path))
    record.minified = (built_with != NOT_MINIFIED) if built_with else None
    if mapping.is_file() and built_with == NOT_MINIFIED:
        # Minify since turned off leaves the last minified build's mapping in
        # place, and it would rewrite this build's real names into wrong ones.
        record.notes.append("the APK was not minified (D8 built it), so the mapping.txt left "
                            "from an earlier minified build was not kept")
    elif mapping.is_file():
        mapping_id = _map_id(mapping)
        if built_with and mapping_id and built_with != mapping_id:
            # A mapping.txt left from another build -- minify since turned off,
            # or a build that failed after R8 -- retraces to plausible, wrong
            # names. Kept, it would be used without a word.
            record.notes.append(
                f"mapping.txt ({mapping_id[:12]}) is not the one the APK was built with "
                f"({built_with[:12]}), so it was not kept: build the variant again")
        else:
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mapping, out / "mapping.txt")
            record.mapping = str(recorded_as / "mapping.txt")
            record.mapping_id = mapping_id
            if not mapping_id:
                record.notes.append("mapping.txt carries no pg_map_id; matched by version only")
    elif built_with and built_with != NOT_MINIFIED:
        record.notes.append(f"the APK was minified by R8 ({built_with[:12]}) but there is no "
                            f"mapping.txt for {variant}, so its Java frames cannot be retraced")
    libs = module_dir / "build" / "intermediates" / "merged_native_libs" / variant
    for so in sorted(libs.rglob("*.so")):
        found = elf.read(so)
        if found is None:
            continue
        rel = Path("lib") / so.parent.name / so.name
        ids = {found.abi: found.build_id} if found.build_id else {}
        binary = BuildBinary(path=str(rel), uuids=ids)
        if found.build_id:
            # Stored by BuildId: two outputs of one name (a stale directory
            # from an older AGP beside the current one) would otherwise
            # overwrite each other, the record naming one and holding the other.
            kept = Path("lib") / so.parent.name / found.build_id / so.name
            (out / kept).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(so, out / kept)
            binary.dsym = binary.dwarf = str(recorded_as / kept)
        else:
            binary.dsym_error = "no BuildId, so no crash can be matched to it"
        record.binaries.append(binary)


#: R8's marker in every dex it writes: `~~R8{…"pg-map-id":"e1ad1424…"…}`. D8,
#: which builds an unminified variant, writes `~~D8{…}` with no map id.
_DEX_MAP_ID = re.compile(rb'~~R8\{[^}]*"pg-map-id":"([0-9a-f]+)"')
_DEX_D8 = re.compile(rb"~~D8\{")
_DEX_NAME = re.compile(r"^classes(\d*)\.dex$")
NOT_MINIFIED = "d8"


def _apk_map_id(apk: Path) -> str:
    """The pg_map_id R8 stamped into the APK's code; NOT_MINIFIED when D8 built
    it; "" if it carries no marker or cannot be read -- then the mapping is
    taken on trust, as before."""
    try:
        with zipfile.ZipFile(apk) as z:
            # Every dex normally carries it, but not all do (7 of 26 in a real
            # debug APK), so the first that does answers.
            names = sorted((n for n in z.namelist() if _DEX_NAME.match(n)),
                           key=lambda n: int(_DEX_NAME.match(n).group(1) or 1))
            for name in names:
                dex = z.read(name)
                m = _DEX_MAP_ID.search(dex)
                if m:
                    return m.group(1).decode()
                if _DEX_D8.search(dex):
                    return NOT_MINIFIED
    # A corrupt APK raises whatever its compressor does (zlib.error, EOFError,
    # a RuntimeError for an encrypted entry), and a record must not fail on it.
    except (OSError, KeyError, EOFError, RuntimeError, ValueError, zipfile.BadZipFile,
            zlib.error):
        return ""
    return ""


def _map_id(mapping: Path) -> str:
    with open(mapping, errors="replace") as f:
        for i, line in enumerate(f):
            m = _MAP_ID.match(line)
            if m:
                return m.group(1)
            if i > 50 or not line.startswith("#"):
                return ""
    return ""


def save(record: BuildRecord, root: Path | None = None) -> None:
    """Rewrite a record that is already on disk (after its installs, say)."""
    _write(record, (root or RECORDS_DIR) / record.build_id)


def _write(record: BuildRecord, directory: Path) -> None:
    path = directory / "record.json"
    if not directory.is_dir():
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(record.model_dump_json(indent=2))
    tmp.replace(path)


def _read_info(app_path: Path, record: BuildRecord) -> None:
    try:
        with open(app_path / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except Exception as e:  # noqa: BLE001 -- a truncated XML plist raises
        # expat's ExpatError, which is none of OSError, ValueError or
        # InvalidFileException; losing the whole record over it is the defect.
        record.notes.append(f"Info.plist could not be read: {type(e).__name__}: {e}")
        return
    if not isinstance(info, dict):
        record.notes.append("Info.plist could not be read: it is not a dictionary")
        return
    record.executable = str(info.get("CFBundleExecutable") or "")
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
            # An archive member is written `lib.a(member.o)`. Only a trailing
            # member is stripped: a scheme named "App (Beta)" puts a "(" in
            # every object path, and cutting there found none of them.
            archive, sep, _ = obj.rpartition("(")
            if Path(archive if sep and obj.endswith(")") else obj).exists():
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
                pass                        # copied already, for another binary
            elif have is not None:
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
                if any("no debug symbols" in w for w in warnings):
                    # Exit 0 and an empty dSYM: the object files were gone by
                    # the time it ran. Not incomplete -- empty.
                    raise RuntimeError(
                        "dsymutil found no debug information: its object files are gone")
                if warnings:
                    # Most likely an object file replaced mid-read by another
                    # build of the scheme: the dSYM is kept, and said to be
                    # incomplete rather than presented as whole.
                    binary.dsym_warnings = warnings[:3]
            else:
                continue
            # The file `atos -o` takes. A dSYM can cover several binaries, and
            # atos given the bundle picked the wrong one and resolved nothing
            # (live, on a phone crash against MyApp.app.dSYM).
            dwarf = await asyncio.to_thread(dwarf_for, target, uuids)
            if not dwarf:
                raise RuntimeError("the dSYM holds no DWARF file with this binary's UUID")
        except (OSError, RuntimeError, TimeoutError) as e:
            if have is None or have not in copied:
                shutil.rmtree(target, ignore_errors=True)
            binary.dsym_error = str(e) or type(e).__name__
            continue
        binary.dsym = str(recorded_as / name)
        binary.dwarf = str(recorded_as / name / dwarf)


def dwarf_for(dsym: Path, uuids: set[str]) -> str:
    """The DWARF file in `dsym` covering `uuids`, relative to it; "" if none."""
    for dwarf in sorted((dsym / "Contents" / "Resources" / "DWARF").glob("*")):
        m = macho.read(dwarf)
        if m is not None and uuids and uuids <= set(m.uuids.values()):
            return str(dwarf.relative_to(dsym))
    return ""


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
    return [record for record, _ in _load(root or RECORDS_DIR)[0]]


def load_with_unreadable(root: Path | None = None) -> tuple[list[BuildRecord], int, bool]:
    """Every complete record, newest first; how many could not be read; and
    whether the directory itself could be listed. `glob` on a directory that
    cannot be read returns nothing rather than raising, which reads as "no
    records" -- the directory is checked first so it is not."""
    root = root or RECORDS_DIR
    try:
        root.stat()
    except (FileNotFoundError, NotADirectoryError):
        return [], 0, True                   # asked: nothing recorded yet
    except OSError:
        # An unsearchable parent: 3.13 raises from is_dir(), 3.14 answers
        # False and globs nothing -- "no records", said of records it never saw.
        return [], 0, False
    if not os.access(root, os.R_OK | os.X_OK):
        return [], 0, False
    records, unreadable = _load(root)
    return [record for record, _ in records], len(unreadable), True


def _load(root: Path) -> tuple[list[tuple[BuildRecord, Path]], list[Path]]:
    """(readable records with the directory each was read from, newest first;
    directories whose record could not be read)."""
    records, unreadable = [], []
    for directory in root.glob("*"):
        if directory.name.endswith(_PARTIAL) or not directory.is_dir():
            continue
        path = directory / "record.json"
        try:
            records.append((BuildRecord.model_validate_json(path.read_text()), directory))
        except (OSError, ValueError) as e:
            logger.warning("Skipping unreadable build record %s: %s", path, e)
            unreadable.append(directory)
    records.sort(key=lambda rd: rd[0].created_at, reverse=True)
    return records, unreadable


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
            if now.timestamp() - partial.stat().st_mtime > STALE_PARTIAL_SECONDS:
                shutil.rmtree(partial)
                removed.append(f"{partial.name} (unfinished)")
        except OSError as e:
            logger.warning("Could not remove %s: %s", partial, e)

    cutoff = now - timedelta(days=max_age_days)
    records, unreadable = _load(root)
    # A record that no longer parses -- an older schema, say -- would otherwise
    # hold its dSYMs forever. Aged by its directory instead.
    for directory in unreadable:
        try:
            if max_age_days > 0 and directory.stat().st_mtime < cutoff.timestamp():
                shutil.rmtree(directory)
                removed.append(f"{directory.name} (unreadable, older than {max_age_days} days)")
        except OSError as e:
            logger.warning("Could not remove %s: %s", directory, e)
    kept_per_scheme: dict[tuple[str, str], int] = {}
    # The directory it was read from, never one named by the file: a record
    # whose build_id said "../x" had retention delete a sibling of the root.
    for record, directory in records:
        if max_age_days > 0 and record.created_at < cutoff:
            try:
                shutil.rmtree(directory)
                removed.append(f"{record.build_id} (older than {max_age_days} days)")
            except OSError as e:
                logger.warning("Could not remove %s: %s", directory, e)
            continue
        if record.platform not in ("iphoneos", "android") or not (
                any(b.dsym for b in record.binaries) or record.mapping):
            continue
        key = (record.project_path, record.scheme)
        kept_per_scheme[key] = kept_per_scheme.get(key, 0) + 1
        if kept_per_scheme[key] <= keep:
            continue
        try:
            for kept in ("dSYMs", "symbols"):     # iOS dSYMs; Android mapping and libraries
                if (directory / kept).exists():
                    shutil.rmtree(directory / kept)
        except OSError as e:
            logger.warning("Could not remove the symbols of %s: %s", record.build_id, e)
            continue
        for b in record.binaries:
            b.dsym = b.dwarf = ""
        record.mapping = ""
        record.dsyms_expired = True
        try:
            _write(record, directory)
        except OSError as e:
            logger.warning("Could not update %s: %s", record.build_id, e)
        removed.append(f"{record.build_id} dSYMs (beyond the newest {keep} of {record.scheme})")
    return removed


def summary_line(record: BuildRecord) -> str:
    """One line for the build summary an MCP caller reads."""
    what = " ".join(x for x in (record.bundle_id, record.version,
                                f"({record.build_number})" if record.build_number else "") if x)
    if not record.build_id:
        # Nothing was recorded: "Recorded build : ..." said the opposite first.
        what = what or "unknown app"
        return f"Build not recorded ({what}, {record.configuration}): {record.error}."
    head = f"Recorded build {record.build_id}: {what or 'unknown app'}, {record.configuration}"
    if record.error:
        return f"{head} -- {record.error}."
    notes = "".join(f" Note: {n}." for n in record.notes)
    if record.platform == "android":
        libs = sum(1 for b in record.binaries if b.dwarf)
        if record.mapping:
            mapping = f"its R8 mapping ({record.mapping_id[:12]})"
        elif record.minified:
            # Refused, or missing: the note says which, and this must not
            # read as a build that needs none.
            mapping = "no R8 mapping, though R8 built the APK (see the note)"
        elif record.minified is False:
            mapping = "no R8 mapping (not a minified variant)"
        else:
            mapping = f"no R8 mapping (no mapping.txt for {record.configuration})"
        return (f"{head}; kept {mapping} and {libs} native "
                f"librar{'y' if libs == 1 else 'ies'} by BuildId.{notes}")
    if record.platform != "iphoneos":
        n = len(record.binaries)
        whose = "1 binary's" if n == 1 else f"{n} binaries'"
        return f"{head}, {whose} UUIDs.{notes}"
    kept = [b for b in record.binaries if b.dsym]
    failed = [b for b in record.binaries if b.dsym_error]
    parts = [f"{head}; symbols kept for {len(kept)} binar{'y' if len(kept) == 1 else 'ies'}"]
    if failed:
        parts.append(f"not for {', '.join(b.path for b in failed)} ({failed[0].dsym_error})")
    if any(b.dsym_warnings for b in kept):
        parts.append("some may be incomplete (dsymutil warned)")
    # Vendored frameworks' dSYMs can make the count look healthy when the one
    # that matters is missing.
    names = (record.executable, f"{record.executable}.debug.dylib")
    own = [b for b in record.binaries if record.executable and b.path in names]
    if own and not any(b.dsym for b in own):
        parts.append(f"none for the app's own code ({', '.join(b.path for b in own)})")
    elif not kept and not failed:
        parts.append("none of its binaries were built from source here")
    return "; ".join(parts) + "." + notes
