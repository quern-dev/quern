"""Symbolicate an Android crash against the build that crashed (#326, step 4).

Two halves, both matched to a build record made by `record_android_build`:

- **Native frames** name their library by GNU BuildId, exactly as iOS names
  an image by UUID. A library the app shipped is resolved against the
  unstripped copy the record kept, with the NDK's `llvm-symbolizer`. The pc a
  tombstone prints is already the one to look up: Android's unwinder adjusts
  a return address before printing it, and `ndk-stack` passes it as it is.
- **Java frames** of a minified build are R8's short names. They are retraced
  against the record's `mapping.txt` with Google's `retrace` (Android SDK
  command-line tools) whenever a record's mapping matches the crash -- the
  whole trace, as one trace, with each block's exception line. retrace needs
  that context: it rewrites a NullPointerException's frames by the rules R8
  wrote for it, and resolves an outlined frame from the one after it. Sent as
  separate frames, an NPE named the inlined callee (measured on a real app).

A frame's `r8-map-id-<id>` names its mapping exactly. Without it the match is
by package and version, which a local build does not change (Gradle builds
here are all version code 99999), so the note always says so. With no record,
R8's marks decide whether to say one is missing: `SourceFile`, an
`r8-map-id-`, or -- where rules strip the source file -- `(Unknown Source:539)`.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from server.device import elf
from server.device.adb import _SDK_SEARCH_PATHS, _find_sdk_tool
from server.models import BuildRecord, CrashFrame, CrashReport, ImageSymbols
from server.sources import android_dropbox, crash_frames

if TYPE_CHECKING:
    # symbolicate imports this module, so its types are for the checker only.
    from server.sources.symbolicate import Read, SymbolFinder

#: retrace loads the whole mapping (192 MB for a real app): 0.7 s measured,
#: but a slow disk or a cold JVM is not atos.
RETRACE_TIMEOUT = 180  # s
_R8_FILE = re.compile(r"^(SourceFile|r8-map-id-[0-9a-f]+)$")
_MAP_ID = re.compile(r"^r8-map-id-([0-9a-f]+)$")
#: D8's own synthetic classes, which a debug build's frames name with
#: `(Unknown Source:2)` too: not R8's mark.
_SYNTHETIC = ("$$ExternalSynthetic", "$$Lambda", "-$$Nest$")
#: A class name as R8 writes it: `l82`, `a`, `Ab`, `y3c`. kotlinc leaves the
#: source file out of whole classes (1,608 in a real debug build, among them
#: kotlinx.coroutines.BuildersKt), and ART prints those `(Unknown Source:N)`
#: too -- so without a minified name that is no mark of R8. Tested on the
#: outer name and each inner one (`Activity$b`): measured, 100% of a real
#: mapping's renamed classes match, as do some kept ones (gms `zza`, a kept
#: `$a`), so it is a hint and never proof.
_MINIFIED_CLASS = re.compile(r"^[a-zA-Z]{1,3}\d{0,4}[a-z]{0,2}$")
#: Appended to every frame sent; retrace copies it onto each line it writes
#: for that frame, inlined expansions included (measured).
_TAG = " ~[QUERN-{}]"
#: `at com.example.Feed.parse(Feed.kt:12) ~[QUERN-3]`, or `<OR> at …` for an
#: ambiguous reading.
_RETRACED = re.compile(r"^\s*(?P<or><OR>\s*)?at (?P<sym>[^\s(]+)\((?P<src>[^)]*)\)"
                       r"\s*~\[QUERN-(?P<i>\d+)\]\s*$")
#: What retrace writes where a frame has no file.
_NO_FILE = ("Unknown Source", "SourceFile", "unavailable", "Native Method",
            "R8$$SyntheticClass")
_JAVA_IMAGE = "java (R8 mapping)"


def is_android(report: CrashReport) -> bool:
    return report.file_path.startswith("dropbox:")


def find_llvm_symbolizer() -> str | None:
    """The newest NDK's llvm-symbolizer, or one on PATH."""
    for root in _sdk_roots():
        # <sdk>/ndk/<version>/toolchains/llvm/prebuilt/<host>/bin/llvm-symbolizer
        found = sorted((root / "ndk").glob("*/toolchains/llvm/prebuilt/*/bin/llvm-symbolizer"),
                       key=lambda p: _version(p.parts[-7]))
        if found:
            return str(found[-1])
    return _find_sdk_tool("llvm-symbolizer", "")


def find_retrace() -> str | None:
    """The SDK's retrace before one on PATH: Homebrew's ProGuard installs a
    `retrace` of its own, which is another program."""
    for root in _sdk_roots():
        candidate = root / "cmdline-tools" / "latest" / "bin" / "retrace"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("retrace")


def _sdk_roots() -> list[Path]:
    return [Path(os.environ[v]) for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
            if os.environ.get(v)] + list(_SDK_SEARCH_PATHS)


def _version(name: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", name))


def _is_java(frame: CrashFrame) -> bool:
    # A native frame names its image; one with no BuildId (a JIT frame, an
    # old Android) is still native, and the Java heuristics do not apply.
    return not frame.image and frame.offset is None and bool(frame.symbol)


def _is_r8(frame: CrashFrame) -> bool:
    """A frame showing R8's marks: `SourceFile`, an `r8-map-id-` stamp, or no
    file but a line on a minified class name -- `l82.onClick(Unknown
    Source:539)`, where rules strip the source file (measured: a real release
    build's crash). A hint rather than proof, used to say a record is missing
    and to count; `_names_r8` is what an unproven mapping is held to."""
    if _names_r8(frame):
        return True
    parts = _class(frame.symbol).rpartition(".")[2].split("$")
    minified = bool(_MINIFIED_CLASS.match(parts[0])) or any(
        _MINIFIED_CLASS.match(p) for p in parts[1:] if not p.isdigit())
    return (not frame.file and frame.line is not None
            and not any(s in frame.symbol for s in _SYNTHETIC) and minified)


def _hints_r8(java: list[CrashFrame]) -> bool:
    """Whether the trace looks minified: a frame with R8's marks, or every
    frame of the app's and its libraries' printed without a source file --
    an app whose rules strip them, where kept classes keep their names. A
    debug build prints its own frames' files."""
    if any(_is_r8(f) for f in java):
        return True
    own = [f for f in java if not f.symbol.startswith(_PLATFORM)
           and not any(s in f.symbol for s in _SYNTHETIC)]
    return bool(own) and all(not f.file and f.line is not None for f in own)


def _names_r8(frame: CrashFrame) -> bool:
    """R8's own spelling of a source file: nothing else writes these."""
    return bool(_R8_FILE.match(frame.file or ""))


async def symbolicate(report: CrashReport, finder: SymbolFinder, read: Read) -> bool:
    """Symbolicate one Android report in place; return whether it is settled."""
    settled = {e.image: e for e in report.symbols if e.settled}
    whole = bool(report.trace)
    beyond = None
    if whole:
        frames = list(report.trace)               # the whole trace, not the first 30
    else:
        frames = list(report.frames)
        if report.app_frame is not None and not any(report.app_frame is f for f in frames):
            beyond = report.app_frame             # past the cap on `frames`
            frames.append(beyond)
    native: dict[str, list[CrashFrame]] = {}
    # Native frames past the cap belong to no image the report lists, so
    # only those shown and the app frame are resolved.
    shown = list(report.frames)
    if report.app_frame is not None and not any(report.app_frame is f for f in shown):
        shown.append(report.app_frame)
    for f in shown:
        if f.build_id and f.app and f.offset is not None and not f.file:
            native.setdefault(f.image, []).append(f)
    java = [f for f in frames if _is_java(f)] if _JAVA_IMAGE not in settled else []
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
        if java:
            entry = ImageSymbols(image=_JAVA_IMAGE)
            try:
                wanted = await _java(report, java, beyond, entry, finder, read)
            except Exception as e:  # noqa: BLE001
                entry.note, wanted = f"symbolication failed: {type(e).__name__}: {e}", True
            if wanted:
                entries.append(entry)
            done &= entry.settled
    finally:
        report.symbols = entries
        if whole:
            report.frames = report.trace[:crash_frames.MAX_FRAMES]
        after = [(f.symbol, f.file, f.line) for f in report.frames]
        if after != before:
            # Rebuilt only once something resolved: until then top_frames is
            # the record's own text (a tombstone line, a trace line).
            top = report.frames[:crash_frames.TOP_FRAMES]
            report.top_frames = [crash_frames.format_frame(f) for f in top]
    return done


async def _records(finder: SymbolFinder, read: Read) -> tuple[list[BuildRecord], str]:
    """Android records, and what to add to a miss: that some could not be
    read is not the same answer as that none exists."""
    records, unreadable, listable = await finder._records(read)
    why = ""
    if not listable:
        why = "; quern's build records directory could not be read"
    elif unreadable:
        why = f"; {unreadable} build record{'s' if unreadable > 1 else ''} could not be read"
    return [r for r in records if r.platform == "android"], why


def _failed(tool: str, code: int, out: str, err: str) -> str:
    # The SDK's scripts print why (JAVA_HOME not set) on stdout.
    said = (err.strip() or out.strip())[-160:]
    return f"{tool} exited {code}" + (f": {said}" if said else "")


# ── native ───────────────────────────────────────────────────────────────────

async def _native(todo: list[CrashFrame], entry: ImageSymbols, finder: SymbolFinder,
                  read: Read) -> bool:
    build_id = todo[0].build_id.lower()
    records, why = await _records(finder, read)
    candidates = [(r, b) for r in records for b in r.binaries
                  if build_id in {u.lower() for u in b.uuids.values()}]
    live = [(r, b) for r, b in candidates if b.dwarf and not r.dsyms_expired]
    if not live:
        if candidates:
            entry.note = (f"the symbols of {entry.image} expired with its build record: only "
                          f"the newest builds of a variant keep them")
            return False                  # recording that build again brings them back
        entry.note = (f"no build record has {entry.image} with BuildId {build_id}: record the "
                      f"build with record_android_build{why}")
        return False
    # The record says which copy; the copy says whether it is that library.
    library = None
    for record, binary in live:
        found = await asyncio.to_thread(elf.read, Path(binary.dwarf))
        if found is not None and found.build_id == build_id:
            library = (record, binary)
            break
    record, binary = library or live[0]
    entry.source, entry.build_id, entry.dwarf = "build_record", record.build_id, binary.dwarf
    if library is None:
        entry.note = (f"the record's copy of {entry.image} is gone, unreadable, or not the "
                      f"library with BuildId {build_id}")
        return False
    tool = find_llvm_symbolizer()
    if tool is None:
        entry.note = "llvm-symbolizer was not found: install the Android NDK"
        return False
    wanted = sorted({f.offset for f in todo if f.offset >= 0})
    try:
        code, out, err = await finder.run(
            [tool, f"--obj={binary.dwarf}", *(hex(a) for a in wanted)])
    except (OSError, TimeoutError) as e:
        entry.note = f"llvm-symbolizer could not run: {type(e).__name__}: {e}"
        return False
    if code != 0:
        entry.note = _failed("llvm-symbolizer", code, out, err)
        return False
    blocks = _plain_blocks(out)
    if len(blocks) != len(wanted):
        # One block per address, in order: anything else cannot be matched up
        # safely, and a wrong line is worse than none.
        entry.note = f"llvm-symbolizer gave {len(blocks)} answers for {len(wanted)} addresses"
        return True
    answers = dict(zip(wanted, blocks, strict=True))
    lined, named, inlined = set(), set(), set()
    for f in todo:
        function, file, line, nested = answers.get(f.offset, ("", "", 0, False))
        if file and line:
            # The line belongs to the function the symbolizer names -- with
            # inlining, not the one the tombstone named from the symbol table --
            # so the two are taken together, and the tombstone's offset into
            # its own symbol goes with its name.
            if function and function != f.symbol:
                f.symbol, f.symbol_offset = function, None
            f.file, f.line = os.path.basename(file), int(line)
            lined.add(f.offset)
            if nested:
                inlined.add(f.offset)
        elif function and not f.symbol:
            f.symbol = function
            named.add(f.offset)
    entry.frames_resolved = len(lined)
    notes = []
    if len(lined) < entry.frames_total:
        notes.append(f"{entry.frames_total - len(lined)} of {entry.frames_total} frames have no "
                     f"source line: the library has no debug information for them"
                     + (f"; {len(named)} got a function name" if named else ""))
    if inlined:
        notes.append(f"{len(inlined)} frames were inlined; the innermost function is shown")
    entry.note = "; ".join(notes)
    return True


def _plain_blocks(out: str) -> list[tuple[str, str, int, bool]]:
    """(function, file, line, inlined) per address from llvm-symbolizer's plain output:
    a function line and a `file:line:col` line per frame, the innermost inlined
    frame first, a blank line between addresses, `??` for what it does not
    know. Plain rather than `--output-style=JSON`, which NDK 23's ignores
    (measured) -- the form ndk-stack reads works on every NDK."""
    blocks = []
    for chunk in out.strip("\n").split("\n\n") if out.strip() else []:
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        function = lines[0] if lines and lines[0] != "??" else ""
        file, line = "", 0
        if len(lines) > 1:
            where = lines[1].rsplit(":", 2)
            if len(where) == 3 and where[0] != "??" and where[1].isdigit():
                file, line = where[0], int(where[1])
        blocks.append((function, file, line, len(lines) > 2))
    return blocks


# ── Java ─────────────────────────────────────────────────────────────────────

#: Classes no app's R8 run renames: retrace must hand them back as they went.
_PLATFORM = ("android.", "java.", "javax.", "dalvik.", "com.android.")


async def _java(report: CrashReport, java: list[CrashFrame], beyond: CrashFrame | None,
                entry: ImageSymbols, finder: SymbolFinder, read: Read) -> bool:
    """Retrace the Java frames into `entry`; return whether it is worth an
    entry at all. With no record and no mark of R8 it is not -- a debug
    build's trace -- but neither is it settled: the record may come later."""
    records, why = await _records(finder, read)
    record, how, exact = _mapping_for(report, java, records)
    if record is None:
        if not _hints_r8(java):
            return False
        entry.note = how + why
        return True
    entry.source, entry.build_id, entry.dwarf = "build_record", record.build_id, record.mapping
    entry.uuid = record.mapping_id
    if not await asyncio.to_thread(_readable, Path(record.mapping)):
        entry.note = "the record's mapping.txt is gone or cannot be read"
        return True
    tool = find_retrace()
    if tool is None:
        entry.note = ("retrace was not found: install the Android SDK command-line tools "
                      "(sdkmanager \"cmdline-tools;latest\")")
        return True
    with tempfile.TemporaryDirectory(prefix="quern-retrace-") as tmp:
        trace = Path(tmp) / "trace.txt"
        trace.write_text(_trace(java))
        try:
            code, out, err = await finder.run([tool, record.mapping, str(trace)])
        except (OSError, TimeoutError) as e:
            entry.note = f"retrace could not run: {type(e).__name__}: {e}"
            return True
    if code != 0:
        entry.note = _failed("retrace", code, out, err)
        return True
    tagged = _tagged(out, java)
    if tagged is None:
        # Settled: retrace answered, and asking again would repeat it.
        entry.note, entry.settled = "retrace's output could not be matched to the frames sent", True
        return True
    readings = {i: _readings(f, tagged.get(i, [])) for i, f in enumerate(java)}
    # The frame's own method is the outermost line of an expansion (the last);
    # the lines before it are what was inlined into it, from any class.
    renamed = [i for i, (lines, _) in readings.items()
               if lines and _class(lines[-1].symbol) != _class(java[i].symbol)]
    # Sent with a line but no file, and given a file back: R8 stripped it. A
    # D8 build prints the file, and a class kotlinc wrote without one comes
    # back without one (measured: BuildersKt). So a kept class of a build
    # whose rules strip source files proves the mapping too.
    stripped = [i for i, (lines, _) in readings.items()
                if lines and lines[-1].file and not java[i].file and java[i].line is not None]
    if not exact and not renamed and not stripped and not any(_names_r8(f) for f in java):
        # Nothing in the trace names its build or is spelled as only R8 spells
        # it, and the mapping renames none of its classes: an unminified build
        # of the same version, whose kept classes it would still "retrace" by
        # line number. `Unknown Source:N` is no proof -- kotlinc prints it too.
        entry.note = (f"{how}; but that mapping renames no class in this trace and finds no "
                      f"file R8 stripped, so the crash is not from that minified build, and "
                      f"its frames were left as they were")
        entry.settled = True
        return True
    counted = [i for i, f in enumerate(java) if i in renamed or i in stripped or _is_r8(f)]
    resolved = sum(1 for i in counted if readings[i][0] and readings[i][0][0].file
                   and readings[i][0][0].line is not None)
    removed = _apply(report, java, beyond, readings)
    # Settled from here: the trace holds retrace's names now, and sending
    # them back would read as a mapping that renames nothing.
    entry.settled = True
    _reassess_app(report, beyond, removed)
    entry.frames_total = len([i for i in counted if readings[i][0]])
    entry.frames_resolved = resolved
    notes = [how] if how else []
    if entry.frames_total > resolved:
        notes.append(f"{entry.frames_total - resolved} of {entry.frames_total} renamed frames "
                     f"have no source line")
    if removed:
        notes.append(f"retrace wrote nothing for {len(removed)} frames (usually outlines and "
                     f"bridges R8 generated), so they were left out")
    ambiguous = sum(1 for _, amb in readings.values() if amb)
    if ambiguous:
        notes.append(f"{ambiguous} frames were ambiguous in the mapping; the first reading "
                     f"is shown")
    inlined = sum(1 for lines, _ in readings.values() if len(lines) > 1)
    if inlined:
        notes.append(f"{inlined} frames were inlined; each inlined function is listed, "
                     f"innermost first")
    entry.note = "; ".join(notes)
    return True


def _class(symbol: str) -> str:
    return symbol.rpartition(".")[0]


def _readings(sent: CrashFrame, lines: list[re.Match]) -> tuple[list[CrashFrame], bool]:
    """The frames retrace gave for one frame sent, innermost first, and whether
    it was ambiguous (the first reading is kept)."""
    out = []
    for m in lines:
        if m.group("or"):
            continue
        symbol = m.group("sym")
        # `com.google.mlkit:translate@@17.0.3:505`: a prebuilt library's file
        # has colons of its own, and the line is after the last.
        file, _, line = m.group("src").rpartition(":")
        if not line.isdigit():
            file, line = m.group("src"), ""
        number = int(line) if line else None
        if sent.line is None:
            number = None       # retrace's line for a frame sent without one is a guess
        elif (_class(symbol) != _class(sent.symbol) and symbol.rpartition(".")[2]
              == sent.symbol.rpartition(".")[2] and number == sent.line):
            # The class is mapped but not the method: retrace renames the class
            # and passes the minified line through, which is not a source line.
            number = None
        out.append(CrashFrame(symbol=symbol, line=number,
                              file="" if file in _NO_FILE or _MAP_ID.match(file) else file))
    return out, len(out) < len(lines)


def _apply(report: CrashReport, java: list[CrashFrame], beyond: CrashFrame | None,
           readings: dict[int, tuple[list[CrashFrame], bool]]) -> list[CrashFrame]:
    """Put the readings into the trace: each frame becomes its first (innermost)
    reading, with the rest of an inlined expansion after it, and a frame
    retrace wrote nothing for leaves -- passing on the exception line that
    began its block. Returns the frames that left."""
    index = {id(f): i for i, f in enumerate(java)}
    whole = bool(report.trace)
    listed = report.trace if whole else report.frames
    result: list[CrashFrame] = []
    removed: list[CrashFrame] = []
    carry = ""
    for f in listed:
        if f.thrown:
            carry = ""                            # a new block: the old header ended
        i = index.get(id(f))
        if i is None:                             # native: nothing to put in
            result.append(f)
            continue
        lines, _ = readings[i]
        if not lines:
            carry = f.thrown or carry
            removed.append(f)
            continue
        _take(f, lines[0])
        if carry and not f.thrown:
            f.thrown, carry = carry, ""
        result.append(f)
        result += lines[1:]
    if whole:
        report.trace = result
    else:
        report.frames = result
    if beyond is not None:
        lines, _ = readings[index[id(beyond)]]
        if lines:
            # Not in the list, so no room for its expansion: the app's own
            # line of it, since it stands for where in the app it crashed.
            flags = crash_frames._java_app_flags([r.symbol for r in lines], report.bundle_id)
            _take(beyond, next((r for r, app in zip(lines, flags, strict=True) if app),
                               lines[0]))
        else:
            removed.append(beyond)
    return removed


def _take(frame: CrashFrame, reading: CrashFrame) -> None:
    frame.symbol, frame.file, frame.line = reading.symbol, reading.file, reading.line


def _trace(java: list[CrashFrame]) -> str:
    """The frames as one trace, in order, each block after its exception line,
    each frame tagged with its index."""
    lines = []
    for i, f in enumerate(java):
        if f.thrown:
            lines.append(f.thrown)
        where = (f.file or "Unknown Source") + (f":{f.line}" if f.line is not None else "")
        lines.append(f"\tat {f.symbol}({where}){_TAG.format(i)}")
    return "\n".join(lines) + "\n"


def _tagged(out: str, java: list[CrashFrame]) -> dict[int, list[re.Match]] | None:
    """retrace's `at` lines, by the index each carries; None when they cannot
    be trusted: none came back, one names a frame that was not sent, or a
    platform frame -- which no mapping renames -- did not come back, as it
    would not if this retrace wrote no tags."""
    found: dict[int, list[re.Match]] = {}
    for line in out.splitlines():
        m = _RETRACED.match(line)
        if m:
            found.setdefault(int(m.group("i")), []).append(m)
    if not found or any(i >= len(java) for i in found):
        return None
    if any(i not in found for i, f in enumerate(java)
           if f.file and not _is_r8(f) and f.symbol.startswith(_PLATFORM)):
        return None
    return found


def _mapping_for(report: CrashReport, java: list[CrashFrame],
                 records: list[BuildRecord]) -> tuple[BuildRecord | None, str, bool]:
    """(the record whose mapping retraces these frames, a note on how sure,
    whether the trace named it exactly)."""
    stamped = {m.group(1) for f in java if (m := _MAP_ID.match(f.file or ""))}
    with_mapping = [r for r in records if r.mapping and not r.dsyms_expired]
    if stamped:
        exact = [r for r in with_mapping if r.mapping_id in stamped]
        if exact:
            return exact[0], "", True
        return None, (f"no build record has the R8 mapping {sorted(stamped)[0][:12]} this "
                      f"crash was built with: record the build with record_android_build"), False
    def of_version(r: BuildRecord) -> bool:
        return (r.bundle_id == report.bundle_id
                and report.build_version in ({r.build_number} | set(r.version_codes))
                and (not report.app_version or r.version == report.app_version))

    newest = next((r for r in records if of_version(r)), None)
    if newest is not None and newest.minified is False and not _hints_r8(java):
        # The build of this version recorded last was not minified, and the
        # trace shows no sign of R8: an older minified record of the same
        # version is not the build that crashed.
        return None, (f"the newest recorded build of {report.bundle_id} {report.app_version} "
                      f"({newest.build_id}) was not minified"), False
    same = [r for r in with_mapping if of_version(r)]
    if not same:
        return None, (f"no build record has an R8 mapping for {report.bundle_id} "
                      f"{report.app_version} ({report.build_version}): if it is a minified "
                      f"build, record it with record_android_build"), False
    # Nothing in the trace names its build, and local builds share a version:
    # a build made after this one was recorded would retrace to wrong names.
    note = (f"matched by package and version only ({same[0].build_id}); a build of the same "
            f"version made since would retrace wrongly")
    if len(same) > 1:
        note += f"; {len(same)} recorded builds share it, and the newest was used"
    return same[0], note, False


def _reassess_app(report: CrashReport, beyond: CrashFrame | None,
                  removed: list[CrashFrame]) -> None:
    """With the real names back, decide again which frames are the app's --
    an obfuscated name told nothing -- and where in the app it crashed, by
    the rule the trace was first read with, over the whole trace."""
    listed = report.trace or report.frames
    java = [f for f in listed if _is_java(f)]
    kept_beyond = beyond is not None and not any(beyond is f for f in removed)
    if kept_beyond:
        java.append(beyond)
    if not java:
        return
    flags = crash_frames._java_app_flags([f.symbol for f in java], report.bundle_id)
    for f, app in zip(java, flags, strict=True):
        f.app = app
    if kept_beyond and beyond.app:
        return            # chosen over the whole trace, of which only it is still here
    if report.kind == "crash":
        report.app_frame = android_dropbox.root_cause_app_frame(
            android_dropbox.java_blocks(listed))
    else:
        report.app_frame = crash_frames.first_app_frame(listed)


def _readable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False
