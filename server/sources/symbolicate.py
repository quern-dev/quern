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
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
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
#: xcrun's own failures, before atos ran: "unable to find utility" (72, checked)
#: and the unaccepted Xcode licence (69).
_XCRUN_DID_NOT_RUN = {69, 72}
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


@dataclass
class Read:
    """What one read of get_latest_crash learns once and shares: its misses,
    so ten reports of one unsymbolicatable build ask Spotlight once, and the
    build records, parsed once rather than once per UUID looked up."""

    misses: dict[str, Lookup] = field(default_factory=dict)
    records: tuple[list, int, bool] | None = None
    records_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SymbolFinder:
    """Where a UUID's symbols are on this Mac.

    A hit is cached for the server's life, while its DWARF file is still
    there: a UUID names one build forever, but retention can remove the dSYM.
    A miss is not cached, here or on the report: a build made later, an index
    that catches up, a dSYM that becomes readable may all supply it, and
    looking again costs a records scan and one Spotlight query. One lookup per
    UUID is in flight at a time, and one symbolication per report.
    """

    def __init__(self, records_root: Path | None = None, run: Runner | None = None) -> None:
        self.records_root = records_root
        self._run = run
        self._found: dict[str, Found] = {}
        self._inflight: dict[str, asyncio.Task[Lookup]] = {}
        # Held weakly: one per crash ever read would otherwise never be freed.
        self._report_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary())

    @property
    def run(self) -> Runner:
        return self._run or _run            # looked up now, so a test's patch applies

    def report_lock(self, crash_id: str) -> asyncio.Lock:
        lock = self._report_locks.get(crash_id)
        if lock is None:
            lock = self._report_locks[crash_id] = asyncio.Lock()
        return lock

    async def find(self, uuid: str, read: Read | None = None) -> Lookup:
        """Where its symbols are. A read's misses are kept for the rest of it
        -- a broken mdfind is not asked ten times -- and the next read asks
        again."""
        read = read or Read()
        wanted = normalise_uuid(uuid)
        if not wanted:
            return Lookup(None, f"{uuid!r} is not a UUID, so it cannot be matched to a build")
        if wanted in read.misses:
            return read.misses[wanted]
        cached = self._found.get(wanted)
        if cached is not None:
            if await asyncio.to_thread(_is_file, cached.dwarf):
                return Lookup(cached)
            if self._found.get(wanted) is cached:  # not one stored meanwhile
                del self._found[wanted]              # its dSYM was removed since
        task = self._inflight.get(wanted)
        if task is None:
            # Its own task, awaited through a shield: a caller that is
            # cancelled stops waiting, and does not cancel everyone else's
            # lookup of the same UUID with it.
            task = asyncio.create_task(self._look(wanted, read))
            self._inflight[wanted] = task
            task.add_done_callback(lambda t: self._done(wanted, t))
        result = await asyncio.shield(task)
        if result.found is not None:
            self._found[wanted] = result.found
        else:
            read.misses[wanted] = result
        return result

    def _done(self, wanted: str, task: asyncio.Task[Lookup]) -> None:
        self._inflight.pop(wanted, None)
        if not task.cancelled():
            # Retrieved here: a waiter cancelled before it finished stops
            # listening, and an exception nobody reads is logged at exit.
            task.exception()

    async def _records(self, read: Read) -> tuple[list, int, bool]:
        async with read.records_lock:
            if read.records is None:
                read.records = await asyncio.to_thread(
                    build_records.load_with_unreadable, self.records_root)
            return read.records

    async def _look(self, uuid: str, read: Read) -> Lookup:
        records = await self._records(read)
        found, record_note = await asyncio.to_thread(self._from_records, uuid, records)
        if found is not None:
            return Lookup(found)
        # A record that cannot help does not end the search: Xcode may hold a copy.
        found, spotlight_note = await self._from_spotlight(uuid)
        if found is not None:
            return Lookup(found)
        return Lookup(None, "; ".join(n for n in (record_note, spotlight_note) if n))

    def _from_records(self, uuid: str, loaded: tuple[list, int, bool]) -> tuple[Found | None, str]:
        records, unreadable, listable = loaded
        notes: list[str] = []
        if not listable:
            notes.append("quern's build records directory could not be read")
        if unreadable:
            # One of them may be the build that crashed: said, and the report
            # is looked at again next time rather than settled as a miss.
            notes.append(f"{unreadable} of quern's build records could not be read")
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
                if not dwarf and binary.dsym:
                    # A record made before `dwarf` was kept: find it by UUID.
                    inside = build_records.dwarf_for(Path(binary.dsym), {uuid})
                    dwarf = str(Path(binary.dsym) / inside) if inside else ""
                if dwarf and _is_file(Path(dwarf)):
                    return Found(Path(dwarf), "build_record", record.build_id), ""
                if binary.dsym or binary.dwarf:
                    notes.append(f"build {record.build_id} made this binary, but its dSYM "
                                 f"is no longer where the record says, or cannot be read")
        return None, "; ".join(notes)

    async def _from_spotlight(self, uuid: str) -> tuple[Found | None, str]:
        try:
            code, out, err = await self.run(["mdfind", f"com_apple_xcode_dsym_uuids == {uuid}"])
        except (OSError, TimeoutError) as e:
            return None, f"Spotlight could not be asked ({type(e).__name__}: {e})"
        if code != 0:
            return None, f"Spotlight could not be asked (mdfind exited {code}: {err.strip()[:120]})"
        listed = []
        for line in out.splitlines():
            if not line.strip():
                continue
            hit = Path(line.strip())
            # Neither this nor macho.read raises for a file it cannot open --
            # it finds nothing -- so a hit that yields nothing is kept to say.
            found = await asyncio.to_thread(_dwarf_in_hit, hit, uuid)
            if found is not None:
                return Found(found, "spotlight"), ""
            listed.append(str(hit))
        if listed:
            # Spotlight says these hold the UUID: reading nothing from them
            # means they could not be read, not that they lack it.
            return None, f"Spotlight lists dSYMs with this UUID that could not be read: " \
                         f"{', '.join(listed[:3])}"
        return None, ""


def _dwarf_in_hit(hit: Path, uuid: str) -> Path | None:
    """The DWARF file for `uuid` in what Spotlight returned: a dSYM, or the
    root of an `.xcarchive` holding it (measured: an archived build's UUID
    finds the archive, not the dSYM in its `dSYMs/`). Archives are what
    TestFlight and App Store builds leave behind."""
    candidates = [hit]
    if hit.suffix == ".xcarchive":
        candidates += sorted((hit / "dSYMs").glob("*.dSYM"))
    for dsym in candidates:
        inside = build_records.dwarf_for(dsym, {uuid})
        if inside:
            return dsym / inside
    return None


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
    `symbols` says so, and the report keeps what it had. An image is settled
    once atos has answered for it, and a report once all its images are; an
    image with no symbols found yet is looked up again on the next read, so a
    build, an index or a readable copy that appears later is picked up.
    """
    gate = asyncio.Semaphore(concurrency)
    read = Read()

    async def one(report: CrashReport) -> None:
        async with gate, finder.report_lock(report.crash_id):
            if report.symbolicated:           # done while this one waited
                return
            report.symbolicated = await _symbolicate(report, finder, read)

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
    report: CrashReport, finder: SymbolFinder, read: Read | None = None,
) -> bool:
    """Symbolicate one report; return whether every image is settled."""
    images = {i.name: i for i in report.images}
    # Settled images keep their entries: a retry after a partial failure
    # rebuilt the list from frames still lacking a line, and dropped the entry
    # of every image that had resolved -- its frames had their lines by then.
    settled = {e.image: e for e in report.symbols if e.settled}
    # The app frame may lie past the cap on `frames`; it is its own object.
    frames = list(report.frames) + ([report.app_frame] if report.app_frame else [])
    by_image: dict[str, list[CrashFrame]] = {}
    for f in frames:
        if _needs_it(f, images.get(f.image)):
            by_image.setdefault(f.image, []).append(f)
    exact = _exact_frames(report)
    entries: list[ImageSymbols] = list(settled.values())
    done = True
    try:
        for name, todo in by_image.items():
            if name in settled:
                continue
            image = images[name]
            # By place, not by object: the app frame is also in `frames`, and
            # counting it twice read "3 of 4" for a crash with three frames.
            entry = ImageSymbols(image=name, uuid=normalise_uuid(image.uuid) or image.uuid,
                                 frames_total=len({f.offset for f in todo}))
            entries.append(entry)
            try:
                entry.settled = await _one_image(image, todo, exact, entry, finder, read)
            except Exception as e:  # noqa: BLE001 -- the report is still a report
                logger.exception("Symbolicating %s in %s failed", name, report.crash_id)
                entry.note = f"symbolication failed: {type(e).__name__}: {e}"
            done &= entry.settled
    finally:
        # Also on cancellation: what did resolve is shown consistently, and
        # the next attempt replaces these entries rather than adding to them.
        report.symbols = entries
        top = report.frames[:crash_frames.TOP_FRAMES]
        report.top_frames = [crash_frames.format_frame(f) for f in top]
    return done


async def _one_image(
    image: CrashImage, todo: list[CrashFrame], exact: set[tuple[str, int | None]],
    entry: ImageSymbols, finder: SymbolFinder, read: Read | None = None,
) -> bool:
    """Symbolicate one image's frames; return whether it is settled: atos
    answered, so asking again would give the same answer."""
    lookup = await finder.find(image.uuid, read)
    if lookup.found is None:
        entry.note = lookup.note or (f"no symbols on this Mac for {image.name} {entry.uuid}: no "
                                     f"build record or indexed dSYM has that UUID")
        return False
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

    # A negative address -- a malformed report, or a text report whose image
    # base is past the frame -- would reach atos as an option ('-0x10') and
    # fail every frame of the image with it.
    invalid = [f for f in todo if address(f)[0] < 0]
    todo = [f for f in todo if address(f)[0] >= 0]
    if not todo:
        entry.note = "the report gives this image's frames addresses below its load address"
        return True
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
        # Settled only when atos itself answered -- a dSYM for another
        # architecture, say -- so asking again gives the same answer. Not when
        # xcrun never ran it (72: no such tool; 69: the Xcode licence is not
        # accepted, typical right after an update), nor when the dSYM went
        # between the lookup and the call (retention, after a new build).
        if code in _XCRUN_DID_NOT_RUN or "xcrun: error" in err:
            return False
        return await asyncio.to_thread(_is_file, found.dwarf)
    lines = out.splitlines()
    if len(lines) != len(wanted):
        # One line per address, in order: anything else cannot be matched up
        # safely, and a wrong line is worse than none.
        entry.note = f"atos gave {len(lines)} lines for {len(wanted)} addresses"
        return True
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
    bad = len({f.offset for f in invalid})
    missing = entry.frames_total - entry.frames_resolved - bad
    notes = []
    if missing:
        name_only = len(named - with_line)
        notes.append(f"{missing} of {entry.frames_total} frames have no source line in its "
                     f"symbols (compiler-generated code, or not in them at all)"
                     + (f"; {name_only} got a function name only" if name_only else ""))
    if bad:
        notes.append(f"{bad} of {entry.frames_total} had addresses below the image's load address")
    entry.note = "; ".join(notes)
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
