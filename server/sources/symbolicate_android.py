"""Symbolicate an Android crash against the build that crashed (#326, step 4).

Two halves, both matched to a build record made by `record_android_build`:

- **Native frames** name their library by GNU BuildId, exactly as iOS names
  an image by UUID. A library the app shipped is resolved against the
  unstripped copy the record kept, with the NDK's `llvm-symbolizer`. The pc a
  tombstone prints is already the one to look up: Android's unwinder adjusts
  a return address before printing it, and `ndk-stack` passes it as it is.
- **Java frames** of a minified build are R8's short names. They are retraced
  against the record's `mapping.txt` with Google's `retrace` (Android SDK
  command-line tools). Only frames that show R8's marks are sent: R8 rewrites
  every source file to `SourceFile` or `r8-map-id-<id>`, and a debug build's
  frames keep their real file names and need nothing.

A frame's `r8-map-id-<id>` names its mapping exactly. Without it the match is
by package and version code, which a local build does not change (Gradle
builds here are all version code 99999), so the note says when several builds
share one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from server.device.adb import _SDK_SEARCH_PATHS, _find_sdk_tool
from server.models import BuildRecord, CrashFrame, CrashReport, ImageSymbols
from server.sources import crash_frames

#: Retrace loads the whole mapping (192 MB for a real app): slower than atos.
RETRACE_TIMEOUT = 180  # s
_R8_FILE = re.compile(r"^(SourceFile|r8-map-id-[0-9a-f]+)$")
_MAP_ID = re.compile(r"^r8-map-id-([0-9a-f]+)$")
_MARK = "QUERN-FRAME-"
#: `at com.example.Feed.parse(Feed.kt:12)`, or `<OR> at …` for an ambiguous one.
_RETRACED = re.compile(r"^\s*(?P<or><OR>\s*)?at (?P<sym>[^\s(]+)\((?P<src>[^)]*)\)\s*$")
_JAVA_IMAGE = "java (R8 mapping)"


def is_android(report: CrashReport) -> bool:
    return report.file_path.startswith("dropbox:")


def find_llvm_symbolizer() -> str | None:
    """The newest NDK's llvm-symbolizer, or one on PATH."""
    roots = [Path(os.environ[v]) for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
             if os.environ.get(v)] + list(_SDK_SEARCH_PATHS)
    for root in roots:
        found = sorted((root / "ndk").glob("*/toolchains/llvm/prebuilt/*/bin/llvm-symbolizer"),
                       key=lambda p: _version(p.parts[-6]))
        if found:
            return str(found[-1])
    return _find_sdk_tool("llvm-symbolizer", "")


def find_retrace() -> str | None:
    return _find_sdk_tool("retrace", "cmdline-tools/latest/bin")


def _version(name: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", name))


def _is_r8(frame: CrashFrame) -> bool:
    return not frame.build_id and bool(_R8_FILE.match(frame.file or ""))


async def symbolicate(report: CrashReport, finder, read) -> bool:
    """Symbolicate one Android report in place; return whether it is settled."""
    settled = {e.image: e for e in report.symbols if e.settled}
    frames = list(report.frames)
    if report.app_frame is not None and not any(report.app_frame is f for f in frames):
        frames.append(report.app_frame)           # past the cap on `frames`
    native: dict[str, list[CrashFrame]] = {}
    for f in frames:
        if f.build_id and f.app and f.offset is not None and not f.file:
            native.setdefault(f.image, []).append(f)
    java = [f for f in frames if _is_r8(f)]
    if not native and not java and not settled:
        return True                               # nothing to do, nothing touched
    entries = list(settled.values())
    done = True
    before = [(f.symbol, f.file, f.line) for f in report.frames]
    try:
        for name, todo in native.items():
            if name in settled:
                continue
            entry = ImageSymbols(image=name, uuid=todo[0].build_id,
                                 frames_total=len({f.offset for f in todo}))
            entries.append(entry)
            try:
                entry.settled = await _native(todo, entry, finder, read)
            except Exception as e:  # noqa: BLE001 -- the report is still a report
                entry.note = f"symbolication failed: {type(e).__name__}: {e}"
            done &= entry.settled
        if java and _JAVA_IMAGE not in settled:
            entry = ImageSymbols(image=_JAVA_IMAGE, frames_total=len(java))
            entries.append(entry)
            try:
                entry.settled = await _java(report, java, entry, finder, read)
            except Exception as e:  # noqa: BLE001
                entry.note = f"symbolication failed: {type(e).__name__}: {e}"
            done &= entry.settled
    finally:
        report.symbols = entries
        after = [(f.symbol, f.file, f.line) for f in report.frames]
        if after != before:
            # Rebuilt only once something resolved: until then top_frames is
            # the record's own text (a tombstone line, a trace line).
            top = report.frames[:crash_frames.TOP_FRAMES]
            report.top_frames = [crash_frames.format_frame(f) for f in top]
    return done


async def _records(finder, read) -> list[BuildRecord]:
    records, _unreadable, _listable = await finder._records(read)
    return [r for r in records if r.platform == "android" and not r.dsyms_expired]


# ── native ───────────────────────────────────────────────────────────────────

async def _native(todo: list[CrashFrame], entry: ImageSymbols, finder, read) -> bool:
    build_id = todo[0].build_id.lower()
    library = None
    for record in await _records(finder, read):
        for b in record.binaries:
            if build_id in {u.lower() for u in b.uuids.values()} and b.dwarf:
                library = (record, b)
                break
        if library:
            break
    if library is None:
        entry.note = (f"no build record has {entry.image} with BuildId {build_id}: "
                      f"record the build with record_android_build")
        return False
    record, binary = library
    entry.source, entry.build_id, entry.dwarf = "build_record", record.build_id, binary.dwarf
    if not await asyncio.to_thread(_readable, Path(binary.dwarf)):
        entry.note = "the record's copy of this library is gone or cannot be read"
        return False
    tool = find_llvm_symbolizer()
    if tool is None:
        entry.note = "llvm-symbolizer was not found: install the Android NDK"
        return False
    wanted = sorted({f.offset for f in todo if f.offset >= 0})
    try:
        code, out, err = await finder.run(
            [tool, f"--obj={binary.dwarf}", "--output-style=JSON", *(hex(a) for a in wanted)])
    except (OSError, TimeoutError) as e:
        entry.note = f"llvm-symbolizer could not run: {type(e).__name__}: {e}"
        return False
    if code != 0:
        entry.note = f"llvm-symbolizer exited {code}: {err.strip()[:160]}"
        return code > 0
    try:
        answers = {int(a["Address"], 16): a.get("Symbol") or [] for a in json.loads(out)}
    except (ValueError, KeyError, TypeError) as e:
        entry.note = f"llvm-symbolizer's output could not be read ({type(e).__name__})"
        return True
    lined = named = 0
    for f in todo:
        symbols = answers.get(f.offset) or []
        top = symbols[0] if symbols else {}
        function, file, line = top.get("FunctionName") or "", top.get("FileName") or "", \
            top.get("Line") or 0
        if function and not f.symbol:
            f.symbol, named = function, named + 1
        if file and line:
            f.file, f.line, lined = os.path.basename(file), int(line), lined + 1
    entry.frames_resolved = lined
    if lined < entry.frames_total:
        entry.note = (f"{entry.frames_total - lined} of {entry.frames_total} frames have no "
                      f"source line: the library has no debug information for them"
                      + (f"; {named} got a function name" if named else ""))
    return True


# ── Java ─────────────────────────────────────────────────────────────────────

async def _java(report: CrashReport, java: list[CrashFrame], entry: ImageSymbols,
                finder, read) -> bool:
    record, how = _mapping_for(report, java, await _records(finder, read))
    if record is None:
        entry.note = how
        return False
    entry.source, entry.build_id, entry.dwarf = "build_record", record.build_id, record.mapping
    entry.uuid = record.mapping_id
    if not await asyncio.to_thread(_readable, Path(record.mapping)):
        entry.note = "the record's mapping.txt is gone or cannot be read"
        return False
    tool = find_retrace()
    if tool is None:
        entry.note = ("retrace was not found: install the Android SDK command-line tools "
                      "(sdkmanager \"cmdline-tools;latest\")")
        return False
    lines = []
    for i, f in enumerate(java):
        where = f.file + (f":{f.line}" if f.line else "")
        lines += [f"{_MARK}{i}", f"\tat {f.symbol}({where})"]
    with tempfile.TemporaryDirectory(prefix="quern-retrace-") as tmp:
        trace = Path(tmp) / "trace.txt"
        trace.write_text("\n".join(lines) + "\n")
        try:
            code, out, err = await finder.run([tool, record.mapping, str(trace)])
        except (OSError, TimeoutError) as e:
            entry.note = f"retrace could not run: {type(e).__name__}: {e}"
            return False
    if code != 0:
        entry.note = f"retrace exited {code}: {err.strip()[:160]}"
        return code > 0
    blocks = _blocks(out, len(java))
    if blocks is None:
        entry.note = "retrace's output could not be matched to the frames sent"
        return True
    resolved = ambiguous = inlined = 0
    for f, block in zip(java, blocks, strict=True):
        primary = [m for m in block if not m.group("or")]
        if not primary:
            continue
        m = primary[0]
        f.symbol = m.group("sym")
        file, _, line = m.group("src").partition(":")
        f.file = file if file not in ("Unknown Source", "SourceFile") else ""
        f.line = int(line) if line.isdigit() else None
        resolved += bool(f.file and f.line)
        ambiguous += any(x.group("or") for x in block)
        inlined += len(primary) > 1
    _reassess_app(report)
    entry.frames_resolved = resolved
    notes = [how] if how else []
    if resolved < entry.frames_total:
        notes.append(f"{entry.frames_total - resolved} of {entry.frames_total} frames have no "
                     f"source line after retracing")
    if ambiguous:
        notes.append(f"{ambiguous} frames were ambiguous in the mapping; the first reading "
                     f"is shown")
    if inlined:
        notes.append(f"{inlined} frames were inlined; the innermost function is shown")
    entry.note = "; ".join(notes)
    return True


def _mapping_for(report: CrashReport, java: list[CrashFrame],
                 records: list[BuildRecord]) -> tuple[BuildRecord | None, str]:
    """(the record whose mapping retraces these frames, a note on how sure)."""
    stamped = {m.group(1) for f in java if (m := _MAP_ID.match(f.file or ""))}
    with_mapping = [r for r in records if r.mapping]
    if stamped:
        exact = [r for r in with_mapping if r.mapping_id in stamped]
        if exact:
            return exact[0], ""
        return None, (f"no build record has the R8 mapping {sorted(stamped)[0][:12]} this "
                      f"crash was built with: record the build with record_android_build")
    same = [r for r in with_mapping
            if r.bundle_id == report.bundle_id and r.build_number == report.build_version
            and (not report.app_version or r.version == report.app_version)]
    if not same:
        return None, (f"no build record has an R8 mapping for {report.bundle_id} "
                      f"{report.app_version} ({report.build_version}): record the build with "
                      f"record_android_build")
    note = ""
    if len(same) > 1:
        note = (f"matched by package and version only, and {len(same)} recorded builds share "
                f"them; the newest, {same[0].build_id}, was used")
    return same[0], note


def _blocks(out: str, count: int) -> list[list[re.Match]] | None:
    """retrace's output split back into one list of `at` lines per frame sent,
    by the marker lines, which it passes through unchanged."""
    blocks: list[list[re.Match]] = []
    current: list[re.Match] | None = None
    for line in out.splitlines():
        if line.startswith(_MARK):
            current = []
            blocks.append(current)
            continue
        m = _RETRACED.match(line)
        if m and current is not None:
            current.append(m)
    return blocks if len(blocks) == count else None


def _reassess_app(report: CrashReport) -> None:
    """With the real names back, decide again which frames are the app's --
    an obfuscated name told nothing -- and where in the app it crashed."""
    java = [f for f in report.frames if not f.build_id]
    if not java:
        return
    flags = crash_frames._java_app_flags([f.symbol for f in java], report.bundle_id)
    for f, app in zip(java, flags, strict=True):
        f.app = app
    if report.app_frame is None or not report.app_frame.app:
        report.app_frame = crash_frames.first_app_frame(report.frames)


def _readable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False
