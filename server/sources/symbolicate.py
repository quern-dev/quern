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

Only frames that need it are sent to `atos`, and only in images inside the app
bundle: system frames arrive named, and a simulator's report arrives with file
and line from macOS. One `atos` call per image per crash (measured: 0.78 s
against a 150 MB dSYM), with all of that image's addresses in it.
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
_WITH_LINE = re.compile(r"^(?P<sym>.+?) \(in (?P<image>.+?)\) \((?P<file>[^()]+):(?P<line>\d+)\)$")
#: `-[UIView layoutSubviews] (in UIKitCore) + 12`
_WITH_OFFSET = re.compile(r"^(?P<sym>.+?) \(in (?P<image>.+?)\) \+ (?P<off>\d+)$")
#: `0x00000040 (in MyApp.debug.dylib)`: atos could not resolve it.
_UNRESOLVED = re.compile(r"^0x[0-9a-fA-F]+ \(in .+\)$")


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


@dataclass
class Found:
    dwarf: Path
    source: str
    build_id: str = ""


class SymbolFinder:
    """Where a UUID's symbols are on this Mac.

    A hit is cached for the server's life: a UUID names one build, forever. A
    miss is not, because a build made later may supply it.
    """

    def __init__(self, records_root: Path | None = None, run: Runner | None = None) -> None:
        self.records_root = records_root
        self._run = run
        self._found: dict[str, Found] = {}

    @property
    def run(self) -> Runner:
        return self._run or _run            # looked up now, so a test's patch applies

    async def find(self, uuid: str) -> tuple[Found | None, str]:
        """(where its symbols are, or None; why not, when not)."""
        uuid = uuid.upper()
        if uuid in self._found:
            return self._found[uuid], ""
        found, note = await asyncio.to_thread(self._from_records, uuid)
        if found is None:
            # An expired record does not end the search: Xcode may hold a copy.
            found, spotlight_note = await self._from_spotlight(uuid)
            note = "; ".join(n for n in (note, spotlight_note) if n) if found is None else ""
        if found is not None:
            self._found[uuid] = found
        return found, note

    def _from_records(self, uuid: str) -> tuple[Found | None, str]:
        expired = ""
        for record in build_records.load_all(self.records_root):
            for binary in record.binaries:
                if uuid not in {u.upper() for u in binary.uuids.values()}:
                    continue
                if record.dsyms_expired:
                    expired = (f"build {record.build_id} made this binary, but its symbols were "
                               f"removed: only the newest device builds of a scheme keep them")
                    continue
                dwarf = binary.dwarf
                if not dwarf and binary.dsym:
                    # A record made before `dwarf` was kept: find it by UUID.
                    inside = build_records.dwarf_for(Path(binary.dsym), {uuid})
                    dwarf = str(Path(binary.dsym) / inside) if inside else ""
                if dwarf and Path(dwarf).is_file():
                    return Found(Path(dwarf), "build_record", record.build_id), ""
        return None, expired

    async def _from_spotlight(self, uuid: str) -> tuple[Found | None, str]:
        try:
            code, out, err = await self.run(["mdfind", f"com_apple_xcode_dsym_uuids == {uuid}"])
        except (OSError, TimeoutError) as e:
            return None, f"Spotlight could not be asked ({type(e).__name__}: {e})"
        if code != 0:
            return None, f"Spotlight could not be asked (mdfind exited {code}: {err.strip()[:120]})"
        for line in out.splitlines():
            dsym = Path(line.strip())
            if not line.strip() or not dsym.is_dir():
                continue
            inside = await asyncio.to_thread(build_records.dwarf_for, dsym, {uuid})
            if inside:
                return Found(dsym / inside, "spotlight"), ""
        return None, ""


def _needs_it(frame: CrashFrame, image: CrashImage | None) -> bool:
    """A frame in the app's bundle that the report left without a symbol or a
    line. System frames arrive named; a simulator's arrive with the line."""
    if image is None or image.base is None or frame.offset is None or not image.uuid:
        return False
    if ".app/" not in image.path and ".appex/" not in image.path:
        return False
    return not frame.symbol or not frame.file


async def symbolicate_many(
    reports: Iterable[CrashReport], finder: SymbolFinder, *, concurrency: int = 4,
) -> None:
    """Symbolicate each report that has not been, in place.

    Never raises for a tool that is missing or fails: the image's entry in
    `symbols` says so, and the report keeps what it had.
    """
    pending = [r for r in reports if not r.symbolicated]
    gate = asyncio.Semaphore(concurrency)

    async def one(report: CrashReport) -> None:
        async with gate:
            try:
                await _symbolicate(report, finder)
            except Exception as e:  # noqa: BLE001 -- a report is still a report
                logger.exception("Symbolicating %s failed", report.crash_id)
                report.symbols.append(ImageSymbols(
                    image="", note=f"symbolication failed: {type(e).__name__}: {e}"))
            report.symbolicated = True

    await asyncio.gather(*(one(r) for r in pending))


async def _symbolicate(report: CrashReport, finder: SymbolFinder) -> None:
    images = {i.name: i for i in report.images}
    # The app frame may lie past the cap on `frames`; it is its own object.
    frames = list(report.frames) + ([report.app_frame] if report.app_frame else [])
    by_image: dict[str, list[CrashFrame]] = {}
    for f in frames:
        if _needs_it(f, images.get(f.image)):
            by_image.setdefault(f.image, []).append(f)
    # Only the crashing thread's top frame is the instruction that faulted.
    # Every other frame is a return address -- the instruction after a call --
    # and after a call that never returns (fatalError) that instruction can be
    # compiler-generated code: atos put a real crash's own frame at
    # `<compiler-generated>:0`, and at `DebugMenuPresenter.swift:170`, the line
    # of the fatalError, one byte earlier. So they are looked up at address - 1,
    # as crash tools do. An exception backtrace is return addresses throughout.
    top = report.frames[0] if report.frames and report.frames_from != "exception" else None

    def lookup(frame: CrashFrame, image: CrashImage) -> int:
        address = image.base + frame.offset
        is_top = top is not None and frame.image == top.image and frame.offset == top.offset
        return address if is_top else address - 1

    for name, todo in by_image.items():
        image = images[name]
        # By place, not by object: the app frame is also in `frames`, and
        # counting it twice read "3 of 4" for a crash with three frames.
        places = {f.offset for f in todo}
        entry = ImageSymbols(image=name, uuid=image.uuid.upper(), frames_total=len(places))
        report.symbols.append(entry)
        found, note = await finder.find(image.uuid)
        if found is None:
            entry.note = note or (f"no symbols on this Mac for {name} {entry.uuid}: no build "
                                  f"record or indexed dSYM has that UUID")
            continue
        entry.source, entry.build_id, entry.dwarf = found.source, found.build_id, str(found.dwarf)
        addresses = sorted({lookup(f, image) for f in todo})
        argv = ["xcrun", "atos", "-o", str(found.dwarf), "-arch", image.arch or "arm64",
                "-l", hex(image.base), *(hex(a) for a in addresses)]
        try:
            code, out, err = await finder.run(argv)
        except (OSError, TimeoutError) as e:
            entry.note = f"atos could not run: {type(e).__name__}: {e}"
            continue
        if code != 0:
            entry.note = f"atos exited {code}: {err.strip()[:160]}"
            continue
        lines = out.splitlines()
        if len(lines) != len(addresses):
            # One line per address, in order: anything else cannot be matched
            # up safely, and a wrong line is worse than none.
            entry.note = f"atos gave {len(lines)} lines for {len(addresses)} addresses"
            continue
        resolved = dict(zip(addresses, (_parse(line) for line in lines), strict=True))
        gained: set[int] = set()
        for f in todo:
            hit = resolved.get(lookup(f, image))
            if hit is None:
                continue
            symbol, symbol_offset, file, line = hit
            # Counted only when it gained something: a line, or a name it did
            # not have. A frame atos put at `<compiler-generated>:0` gained
            # neither, and "4 of 4 resolved" beside it read as a pass.
            if file or not f.symbol:
                gained.add(f.offset)
            f.symbol = symbol
            if symbol_offset is not None:
                f.symbol_offset = symbol_offset
            if file:
                f.file, f.line = file, line
        entry.frames_resolved = len(gained)
        if entry.frames_resolved < entry.frames_total:
            entry.note = (f"{entry.frames_total - entry.frames_resolved} of "
                          f"{entry.frames_total} frames have no source line in its symbols "
                          f"(compiler-generated code, or not in them at all)")
    top = report.frames[:crash_frames.TOP_FRAMES]
    report.top_frames = [crash_frames.format_frame(f) for f in top]


def _parse(line: str) -> tuple[str, int | None, str, int | None] | None:
    """(symbol, offset into it, file, line) from one line of atos, or None."""
    line = line.strip()
    if not line or _UNRESOLVED.match(line):
        return None
    m = _WITH_LINE.match(line)
    if m:
        file, number = m.group("file"), int(m.group("line"))
        # `(<compiler-generated>:0)`: a symbol, but no place in the source.
        if file.startswith("<") or number == 0:
            return m.group("sym"), None, "", None
        return m.group("sym"), None, file, number
    m = _WITH_OFFSET.match(line)
    if m:
        return m.group("sym"), int(m.group("off")), "", None
    return None
