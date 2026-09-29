"""Symbolicate a device's crash reports against the build that crashed (#326).

A device report names the app's code by image UUID, load address and offset.
The phone names a Debug build's functions but has no file or line, and names
nothing at all in a stripped Release build. Only the exact build's DWARF turns
an offset into `AppDelegate.swift:13`, so each image is matched by UUID:

1. quern's own build records, which keep a device build's dSYMs past the next
   build (`server/device/build_records.py`);
2. Spotlight, which indexes dSYMs in Xcode's DerivedData and archives
   (`com_apple_xcode_dsym_uuids`). It does not index `~/.quern`, being hidden,
   which is one reason the records come first.

A UUID with no match is said, never guessed at: the nearest build is not the
build that crashed.

Only the app's own frames (`CrashFrame.app`, decided from the crashed app's
bundle path) that lack a line are sent to `atos`: system frames arrive named,
and a simulator's report usually arrives with file and line from macOS. One
`atos` call per image per crash (measured: 0.5-0.8 s against a 150 MB dSYM),
with all of that image's addresses in it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from server.device import build_records
from server.models import CrashFrame, CrashImage, CrashReport, ImageSymbols
from server.sources import crash_frames

logger = logging.getLogger(__name__)

TOOL_TIMEOUT = 60  # s
#: `(argv) -> (exit code, stdout, stderr)`. Injected by tests, which never
#: run atos or mdfind.
Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

#: `closure #1 in Feed.load() (in MyApp.debug.dylib) (Feed.swift:12)`
_WITH_LINE = re.compile(
    r"^(?P<sym>.+?) \(in (?P<image>.+?)\) \((?P<file>[^()]+):(?P<line>\d+)\)$")
#: `-[UIView layoutSubviews] (in UIKitCore) + 12`
_WITH_OFFSET = re.compile(r"^(?P<sym>.+?) \(in (?P<image>.+?)\) \+ (?P<off>\d+)$")
_UUID = re.compile(r"^[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?"
                   r"[0-9A-Fa-f]{12}$")


async def _run(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TOOL_TIMEOUT)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def normalise_uuid(value: str) -> str:
    """`8-4-4-4-12`, upper case, or "" if it is not a UUID.

    A `.crash` text report writes its UUIDs without dashes and an `.ips` in
    lower case; records and Spotlight match only the dashed upper-case form
    (checked: `==` on the other two finds nothing). And a value that is not a
    UUID must not reach mdfind's query, where `*` matches every dSYM indexed.
    """
    if not isinstance(value, str) or not _UUID.match(value.strip()):
        return ""
    d = value.strip().replace("-", "").upper()
    return f"{d[:8]}-{d[8:12]}-{d[12:16]}-{d[16:20]}-{d[20:]}"


@dataclass
class Found:
    dwarf: Path
    source: str
    build_id: str = ""


@dataclass
class Lookup:
    found: Found | None
    note: str = ""
    #: The answer is final: symbols found, or definitely not on this Mac.
    #: False when a tool could not be asked, so the report is tried again.
    definite: bool = True


class SymbolFinder:
    """Where a UUID's symbols are on this Mac.

    A hit is cached for the server's life, while its DWARF file is still
    there: a UUID names one build forever, but retention can remove the dSYM.
    A miss is not cached, because a build made later may supply it. One
    lookup per UUID is in flight at a time, and one symbolication per report.
    """

    def __init__(self, records_root: Path | None = None, run: Runner | None = None) -> None:
        self.records_root = records_root
        self._run = run
        self._found: dict[str, Found] = {}
        self._inflight: dict[str, asyncio.Future[Lookup]] = {}
        self._report_locks: dict[str, asyncio.Lock] = {}

    @property
    def run(self) -> Runner:
        return self._run or _run            # looked up now, so a test's patch applies

    def report_lock(self, crash_id: str) -> asyncio.Lock:
        return self._report_locks.setdefault(crash_id, asyncio.Lock())

    async def find(self, uuid: str, misses: dict[str, Lookup] | None = None) -> Lookup:
        """Where its symbols are. `misses` holds this read's misses, so ten
        reports of one unsymbolicatable build ask Spotlight once -- and a broken
        mdfind is not asked ten times -- while the next read asks again."""
        wanted = normalise_uuid(uuid)
        if not wanted:
            return Lookup(None, f"{uuid!r} is not a UUID, so it cannot be matched to a build")
        if misses is not None and wanted in misses:
            return misses[wanted]
        cached = self._found.get(wanted)
        if cached is not None:
            if await asyncio.to_thread(_is_file, cached.dwarf):
                return Lookup(cached)
            del self._found[wanted]          # its dSYM was removed since
        pending = self._inflight.get(wanted)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[Lookup] = asyncio.get_running_loop().create_future()
        self._inflight[wanted] = future
        try:
            result = await self._look(wanted)
        except BaseException as e:
            future.set_exception(e)
            future.exception()               # retrieved: no "never retrieved" warning
            raise
        else:
            future.set_result(result)
            if result.found is not None:
                self._found[wanted] = result.found
            elif misses is not None:
                misses[wanted] = result
            return result
        finally:
            del self._inflight[wanted]

    async def _look(self, uuid: str) -> Lookup:
        found, record_note = await asyncio.to_thread(self._from_records, uuid)
        if found is not None:
            return Lookup(found)
        # A record that cannot help does not end the search: Xcode may hold a copy.
        found, spotlight_note, definite = await self._from_spotlight(uuid)
        if found is not None:
            return Lookup(found)
        notes = [n for n in (record_note, spotlight_note) if n]
        return Lookup(None, "; ".join(notes), definite)

    def _from_records(self, uuid: str) -> tuple[Found | None, str]:
        notes: list[str] = []
        try:
            records = build_records.load_all(self.records_root)
        except OSError as e:
            return None, f"quern's build records could not be read ({e})"
        for record in records:
            for binary in record.binaries:
                if uuid not in {normalise_uuid(u) for u in binary.uuids.values()}:
                    continue
                if record.dsyms_expired:
                    notes.append(f"build {record.build_id} made this binary, but its symbols "
                                 f"were removed: only the newest device builds of a scheme "
                                 f"keep them")
                    continue
                dwarf = binary.dwarf
                try:
                    if not dwarf and binary.dsym:
                        # A record made before `dwarf` was kept: find it by UUID.
                        inside = build_records.dwarf_for(Path(binary.dsym), {uuid})
                        dwarf = str(Path(binary.dsym) / inside) if inside else ""
                    if dwarf and Path(dwarf).is_file():
                        return Found(Path(dwarf), "build_record", record.build_id), ""
                except OSError as e:
                    notes.append(f"build {record.build_id}'s dSYM could not be read ({e})")
                    continue
                if binary.dsym or binary.dwarf:
                    notes.append(f"build {record.build_id} made this binary, but its dSYM "
                                 f"is no longer where the record says")
        return None, "; ".join(dict.fromkeys(notes))

    async def _from_spotlight(self, uuid: str) -> tuple[Found | None, str, bool]:
        """(found, note, whether the answer is definite)."""
        try:
            code, out, err = await self.run(["mdfind", f"com_apple_xcode_dsym_uuids == {uuid}"])
        except (OSError, TimeoutError) as e:
            return None, f"Spotlight could not be asked ({type(e).__name__}: {e})", False
        if code != 0:
            why = f"mdfind exited {code}: {err.strip()[:120]}"
            return None, f"Spotlight could not be asked ({why})", False
        unreadable = []
        for line in out.splitlines():
            if not line.strip():
                continue
            dsym = Path(line.strip())
            try:
                # One unreadable hit -- a dSYM in a protected folder raises on
                # Python 3.13 -- must not stop the next one being tried.
                inside = await asyncio.to_thread(build_records.dwarf_for, dsym, {uuid})
            except OSError as e:
                unreadable.append(f"{dsym} ({type(e).__name__})")
                continue
            if inside:
                return Found(dsym / inside, "spotlight"), "", True
        if unreadable:
            where = ", ".join(unreadable)
            return None, f"Spotlight found dSYMs that could not be read: {where}", True
        return None, "", True


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _needs_it(frame: CrashFrame, image: CrashImage | None) -> bool:
    """One of the crashed app's own frames the report left without a line.
    System frames arrive named; a simulator's usually arrive with the line."""
    if not frame.app or frame.offset is None or frame.file:
        return False
    return image is not None and image.base is not None and bool(image.uuid)


async def symbolicate_many(
    reports: Iterable[CrashReport], finder: SymbolFinder, *, concurrency: int = 4,
) -> None:
    """Symbolicate each report that has not been, in place.

    Never raises for a tool that is missing or fails: the image's entry in
    `symbols` says so, and the report keeps what it had. A report is marked
    done only when every image got a definite answer, so one that failed for
    want of a working atos or Spotlight is tried again on the next read.
    """
    gate = asyncio.Semaphore(concurrency)
    misses: dict[str, Lookup] = {}

    async def one(report: CrashReport) -> None:
        async with gate, finder.report_lock(report.crash_id):
            if report.symbolicated:           # done while this one waited
                return
            report.symbolicated = await _symbolicate(report, finder, misses)

    await asyncio.gather(*(one(r) for r in reports if not r.symbolicated and not r.mac_process))


def _exact_frames(report: CrashReport) -> set[tuple[str, int | None]]:
    """The frames whose address is the instruction itself, not a return
    address: the crashing thread's top frame, and the one a signal interrupted
    (just below `_sigtramp`, under a crash reporter's handler)."""
    exact: set[tuple[str, int | None]] = set()
    frames = report.frames
    if frames and report.frames_from != "exception":
        exact.add((frames[0].image, frames[0].offset))
    for i, f in enumerate(frames[:-1]):
        if f.symbol == "_sigtramp":
            exact.add((frames[i + 1].image, frames[i + 1].offset))
    return exact


async def _symbolicate(
    report: CrashReport, finder: SymbolFinder, misses: dict[str, Lookup] | None = None,
) -> bool:
    """Symbolicate one report; return whether every answer was definite."""
    images = {i.name: i for i in report.images}
    # The app frame may lie past the cap on `frames`; it is its own object.
    frames = list(report.frames) + ([report.app_frame] if report.app_frame else [])
    by_image: dict[str, list[CrashFrame]] = {}
    for f in frames:
        if _needs_it(f, images.get(f.image)):
            by_image.setdefault(f.image, []).append(f)
    exact = _exact_frames(report)
    entries: list[ImageSymbols] = []
    definite = True
    try:
        for name, todo in by_image.items():
            image = images[name]
            # By place, not by object: the app frame is also in `frames`, and
            # counting it twice read "3 of 4" for a crash with three frames.
            entry = ImageSymbols(image=name, uuid=normalise_uuid(image.uuid) or image.uuid,
                                 frames_total=len({f.offset for f in todo}))
            entries.append(entry)
            try:
                definite &= await _one_image(image, todo, exact, entry, finder, misses)
            except Exception as e:  # noqa: BLE001 -- the report is still a report
                logger.exception("Symbolicating %s in %s failed", name, report.crash_id)
                entry.note = f"symbolication failed: {type(e).__name__}: {e}"
                definite = False
    finally:
        # Also on cancellation: what did resolve is shown consistently, and
        # the next attempt replaces these entries rather than adding to them.
        report.symbols = entries
        top = report.frames[:crash_frames.TOP_FRAMES]
        report.top_frames = [crash_frames.format_frame(f) for f in top]
    return definite


async def _one_image(
    image: CrashImage, todo: list[CrashFrame], exact: set[tuple[str, int | None]],
    entry: ImageSymbols, finder: SymbolFinder, misses: dict[str, Lookup] | None = None,
) -> bool:
    """Symbolicate one image's frames; return whether the answer was definite."""
    lookup = await finder.find(image.uuid, misses)
    if lookup.found is None:
        entry.note = lookup.note or (f"no symbols on this Mac for {image.name} {entry.uuid}: no "
                                     f"build record or indexed dSYM has that UUID")
        return lookup.definite
    found = lookup.found
    entry.source, entry.build_id, entry.dwarf = found.source, found.build_id, str(found.dwarf)

    # Every frame but the exact ones is a return address -- the instruction
    # after a call -- and after a call that never returns (fatalError) that
    # instruction can be compiler-generated code: atos put a real crash's own
    # frame at `<compiler-generated>:0`, and at the fatalError's line one byte
    # earlier. So they are looked up at address - 1, as crash tools do.
    def address(f: CrashFrame) -> tuple[int, int]:
        back = 0 if (f.image, f.offset) in exact else 1
        return image.base + f.offset - back, back

    wanted = sorted({address(f)[0] for f in todo})
    argv = ["xcrun", "atos", "-o", str(found.dwarf), "-arch", image.arch or "arm64",
            "-l", hex(image.base), *(hex(a) for a in wanted)]
    try:
        code, out, err = await finder.run(argv)
    except (OSError, TimeoutError) as e:
        entry.note = f"atos could not run: {type(e).__name__}: {e}"
        return False
    if code != 0:
        entry.note = f"atos exited {code}: {err.strip()[:160]}"
        return False
    lines = out.splitlines()
    if len(lines) != len(wanted):
        # One line per address, in order: anything else cannot be matched up
        # safely, and a wrong line is worse than none.
        entry.note = f"atos gave {len(lines)} lines for {len(wanted)} addresses"
        return False
    answers = dict(zip(wanted, (_parse(line) for line in lines), strict=True))

    with_line: set[int] = set()
    named: set[int] = set()
    for f in todo:
        at, back = address(f)
        hit = answers.get(at)
        if hit is None:
            continue
        symbol, symbol_offset, file, line = hit
        if not f.symbol:
            named.add(f.offset)
        if symbol != f.symbol:
            # The phone's offset was into the phone's symbol; atos's name may
            # differ (inlining), and an offset into the wrong function is noise.
            f.symbol_offset = None
        f.symbol = symbol
        if symbol_offset is not None:
            f.symbol_offset = symbol_offset + back   # atos was asked `back` bytes early
        if file:
            f.file, f.line = file, line
            with_line.add(f.offset)
    entry.frames_resolved = len(with_line)
    missing = entry.frames_total - entry.frames_resolved
    if missing:
        name_only = len(named - with_line)
        entry.note = (f"{missing} of {entry.frames_total} frames have no source line in its "
                      f"symbols (compiler-generated code, or not in them at all)"
                      + (f"; {name_only} got a function name only" if name_only else ""))
    return True


def _parse(line: str) -> tuple[str, int | None, str, int | None] | None:
    """(symbol, offset into it, file, line) from one line of atos, or None
    when atos could not resolve it (`0x00000040 (in MyApp.debug.dylib)`)."""
    line = line.strip()
    m = _WITH_LINE.match(line)
    if m:
        file, number = m.group("file"), int(m.group("line"))
        # `(<compiler-generated>:0)`: a symbol, but no place in the source.
        if file.lstrip("/").startswith("<") or number == 0:
            return m.group("sym"), None, "", None
        return m.group("sym"), None, file, number
    m = _WITH_OFFSET.match(line)
    if m:
        return m.group("sym"), int(m.group("off")), "", None
    return None
