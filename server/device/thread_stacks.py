"""What an app's threads are doing right now (#245, phase 5).

| platform      | read with                                 | symbols                     |
|---------------|-------------------------------------------|-----------------------------|
| iOS simulator | macOS `sample <pid> 1`                    | `sample`'s own              |
| iOS device    | a stackshot, over the Instruments channel | the app's dSYM, as a crash; |
|               |                                           | the OS's from DeviceSupport |
| Android       | -- (needs root)                           |                             |

A simulator app is a Mac process, so `sample` reads it for about a second and
comes back symbolicated: each thread's stack is the path it was on most, with
how often. A phone's stackshot is one instant of every thread, as raw return
addresses. The app's own frames are symbolicated the way a crash's are -- its
dSYM, found by UUID in quern's build records or by Spotlight -- and the OS's
from the symbols Xcode copies off a device the first time it is used for
development (`~/Library/Developer/Xcode/iOS DeviceSupport`). Measured on an
iPhone 11 (iOS 26.7): the stackshot took 0.7 s, indexing its 4,356 DeviceSupport
images 1.6 s, and each `atos` call about 0.04 s once warm.

Android lets no app read another's stacks without root -- `debuggerd` and
`/data/anr` both need it -- so it says so rather than guessing.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.device import performance
from server.models import (
    CrashFrame,
    CrashImage,
    DeviceError,
    ImageSymbols,
    StackThread,
    ThreadStacks,
)
from server.sources import symbolicate

if TYPE_CHECKING:
    from server.device.ios.simctl import SimctlBackend

MAX_FRAMES_DEFAULT = 40
MAX_FRAMES_MAX = 128

#: `sample`'s duration. One second is its smallest whole unit, and enough to
#: tell a thread that is stuck (on one path in every sample) from one that is
#: busy (spread over several).
SAMPLE_SECONDS = 1
#: `sample` symbolicates after sampling: ~4 s in all, measured on a simulator.
_SAMPLE_TIMEOUT_S = 30.0
_STACKSHOT_TIMEOUT_S = 20.0
_DEVICE_SLACK_S = 15.0

MAIN_QUEUE = "com.apple.main-thread"
ANDROID_UNSUPPORTED = (
    "Android lets no app read another's thread stacks without root: debuggerd and "
    "/data/anr need it. A hang's main-thread gap is in a profile instead "
    "(start_profile, hangs preset).")

#: Where Xcode copies a device's OS symbols the first time it is prepared for
#: development. A module attribute, so tests point it elsewhere.
DEVICE_SUPPORT = Path.home() / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport"
#: arm64's shared region base (SHARED_REGION_BASE_ARM64): the dyld shared
#: cache's unslid addresses, and the images extracted from it, start here.
SHARED_REGION_BASE = 0x1_8000_0000


# -- the simulator: `sample` -------------------------------------------------

#: `804 Thread_178997863   DispatchQueue_1: com.apple.main-thread  (serial)`,
#: `804 Thread_178997926: com.apple.uikit.eventfetch-thread`, after the count.
_THREAD = re.compile(
    r"^Thread_(?P<id>\d+)(?::\s+(?P<name>.*?))?"
    r"(?:\s{2,}DispatchQueue_\d+:\s+(?P<queue>.*?)\s+\((?:serial|concurrent)\))?\s*$")
#: `start  (in dyld) + 6688  [0x100bb1e80]`, and the forms around it:
#: `???  (in UIKitCore)  load address 0x184e00000 + 0x228f88  [0x185028f88]`,
#: a trailing `  File.swift:12`, and a frame with no image at all. A busy
#: thread's innermost frame merges the PCs it was caught at --
#: `mach_absolute_time  (in libsystem_kernel.dylib) + 108,112,...
#: [0x19aadd0f8,0x19aadd0fc,...]` (measured) -- and keeps the first offset.
_FRAME = re.compile(
    r"^(?P<sym>.*?)  \(in (?P<img>[^)]*)\)(?:\s+\+\s+(?P<off>\d+)(?:,\d+)*(?:,\.\.\.)?)?"
    r"(?:\s+load address 0x[0-9a-fA-F]+ \+ 0x(?P<loff>[0-9a-fA-F]+))?"
    r"\s+\[0x[0-9a-fA-F]+(?:,0x[0-9a-fA-F]+)*(?:,\.\.\.)?\]"
    r"(?:\s+(?P<file>\S.*?):(?P<line>\d+))?\s*$")
#: A tree line: guide characters, then the sample count at the node's depth.
_NODE = re.compile(r"^(?P<lead>[ +|:!]*?)(?P<count>\d+) (?P<text>\S.*)$")


@dataclass
class _Node:
    count: int
    text: str
    depth: int
    children: list[_Node] = field(default_factory=list)


def _sample_tree(text: str) -> list[_Node]:
    """`sample`'s call graph as one tree per thread, or [] without one."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "Call graph:")
    except StopIteration:
        return []
    roots: list[_Node] = []
    stack: list[_Node] = []
    for line in lines[start + 1:]:
        if not line.strip():
            break                            # the graph ends at its first blank line
        m = _NODE.match(line)
        if m is None:
            continue
        node = _Node(int(m["count"]), m["text"].rstrip(), len(m["lead"]))
        while stack and stack[-1].depth >= node.depth:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _sample_frame(text: str) -> CrashFrame:
    m = _FRAME.match(text)
    if m is None:
        # `???  [0x...]`: not even an image.
        return CrashFrame(symbol="" if text.startswith("???") else text.split("  [")[0])
    symbol = "" if m["sym"] == "???" else m["sym"]
    return CrashFrame(
        image=m["img"], symbol=symbol,
        symbol_offset=int(m["off"]) if m["off"] else None,
        offset=int(m["loff"], 16) if m["loff"] else None,
        file=m["file"] or "", line=int(m["line"]) if m["line"] else None)


def parse_sample(text: str, app_image: str = "") -> list[StackThread]:
    """Each thread of `sample`'s report, innermost frame first: the path it
    spent most samples on. `app_image` marks the app's own frames."""
    threads = []
    for root in _sample_tree(text):
        m = _THREAD.match(root.text)
        if m is None:
            continue
        path: list[_Node] = []
        node = root
        while node.children:
            # The child it spent most samples in; the first, on a tie, as
            # `sample` sorts them heaviest first.
            node = max(node.children, key=lambda n: n.count)
            path.append(node)
        frames = [_sample_frame(n.text) for n in reversed(path)]
        # Since Xcode 16 a Debug build's code is in `<App>.debug.dylib`, and
        # the executable only loads it (ENABLE_DEBUG_DYLIB).
        mine = {app_image, f"{app_image}.debug.dylib"} if app_image else set()
        for f in frames:
            f.app = f.image in mine
        threads.append(StackThread(
            id=int(m["id"]), name=m["name"] or "", queue=m["queue"] or "",
            main=(m["queue"] or "") == MAIN_QUEUE, frames=frames,
            samples_on_stack=path[-1].count if path else root.count, samples=root.count))
    if threads and not any(t.main for t in threads):
        threads[0].main = True               # `sample` lists the main thread first
    return threads


async def simulator_stacks(
    simctl: SimctlBackend, udid: str, bundle_id: str, *, all_threads: bool, max_frames: int,
) -> ThreadStacks:
    out = ThreadStacks(udid=udid, bundle_id=bundle_id, platform="ios_simulator",
                       running=False, source="sample")
    pid = await simctl.running_pid(udid, bundle_id)
    if pid is None:
        out.unavailable["threads"] = performance.NOT_RUNNING
        return out
    out.running, out.pid = True, pid
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "sample.txt"
        rc, _, err = await performance._run(
            "sample", str(pid), str(SAMPLE_SECONDS), "-file", str(report),
            timeout=_SAMPLE_TIMEOUT_S)
        text = report.read_text(errors="replace") if report.exists() else ""
    if rc != 0 or not text:
        if await simctl.running_pid(udid, bundle_id) != pid:
            # It exited while being sampled: not a failure of sample's.
            out.running, out.pid = False, None
            out.unavailable["threads"] = performance.NOT_RUNNING
            return out
        raise DeviceError(f"sample failed for pid {pid}: {err.strip() or f'exit {rc}'}",
                          tool="sample")
    threads = parse_sample(text, app_image=_sample_process(text))
    if not threads:
        raise DeviceError(f"sample's report for pid {pid} has no call graph quern can read",
                          tool="sample")
    return _keep(out, threads, all_threads, max_frames)


def _sample_process(text: str) -> str:
    """The app's executable name, from `Process:  QuernProbe [49423]`."""
    m = re.search(r"^Process:\s+(.+?) \[\d+\]", text, re.M)
    return m[1] if m else ""


# -- a physical iPhone: a stackshot ------------------------------------------

def _pmd3() -> Any:
    lib = performance._pmd3()
    try:
        from pymobiledevice3.services.dvt.instruments.core_profile_session_tap import (
            CoreProfileSessionTap,
        )
    except ImportError as e:
        raise DeviceError(f"get_thread_stacks on a physical device needs pymobiledevice3's "
                          f"core profile session ({e})", tool="pymobiledevice3") from e
    lib.CoreProfileSessionTap = CoreProfileSessionTap
    return lib


@dataclass
class Shot:
    """What a stackshot says about one app, before symbols."""

    pid: int
    #: (thread id, name, queue, return addresses innermost first)
    threads: list[tuple[int, str, str, list[int]]]
    #: (load address, UUID) of each of the app's own images.
    images: list[tuple[int, str]]
    #: The dyld shared cache's slide, slid base and UUID.
    slide: int | None
    cache_base: int | None
    cache_uuid: str
    #: Which DeviceSupport folder would hold the OS's symbols.
    device: tuple[str, str, str]           # (product type, version, build)


def read_shot(raw: dict, pid: int, device: tuple[str, str, str]) -> Shot | None:
    """The app's part of a parsed stackshot, or None when it is not in it."""
    tasks = raw.get("task_snapshots") or {}
    # By the task's own pid, which it carries: the dict's keys were the pids
    # in every shot read (ints from pymobiledevice3, strings once through
    # JSON), but nothing promises they stay so.
    task = next((t for t in tasks.values() if isinstance(t, dict)
                 and (t.get("task_snapshot") or {}).get("ts_pid") == pid), None)
    if task is None:
        task = tasks.get(pid, tasks.get(str(pid)))
    if task is None:
        return None
    threads = []
    for tid, th in (task.get("thread_snapshots") or {}).items():
        lrs = [int(f["lr"]) for f in th.get("user_stack_frames") or []
               if isinstance(f, dict) and f.get("lr")]
        threads.append((int(tid), str(th.get("pth_name") or ""),
                        str(th.get("dispatch_queue_label") or ""), lrs))
    threads.sort(key=lambda t: t[0])
    images = sorted((int(i["imageLoadAddress"]), str(i["imageUUID"]))
                    for i in task.get("dyld_load_info") or [] if "imageLoadAddress" in i)
    cache = raw.get("shared_cache_dyld_load_info") or {}
    slide, base = cache.get("imageLoadAddress"), cache.get("imageSlidBaseAddress")
    return Shot(pid=pid, threads=threads, images=images,
                slide=int(slide) if slide is not None else None,
                cache_base=int(base) if base is not None else None,
                cache_uuid=str(cache.get("imageUUID") or ""), device=device)


async def take_stackshot(uuid: str, bundle_id: str) -> tuple[int | None, dict | None,
                                                               tuple[str, str, str]]:
    """(pid, parsed stackshot, (product type, version, build)); the pid is
    None when the app is not running, and then nothing else was taken."""
    lib = _pmd3()
    address = await performance._device_address(uuid)
    budget = _STACKSHOT_TIMEOUT_S + _DEVICE_SLACK_S
    rsd = lib.RemoteServiceDiscoveryService(address)
    deadline = asyncio.timeout(budget)
    try:
        async with deadline:
            try:
                await rsd.connect()
                props = (rsd.peer_info or {}).get("Properties") or {}
                device = (str(props.get("ProductType") or ""), str(props.get("OSVersion") or ""),
                          str(props.get("BuildVersion") or ""))
                async with lib.DvtProvider(rsd) as dvt:
                    async with lib.ProcessControl(dvt) as pc:
                        pid = await pc.process_identifier_for_bundle_identifier(bundle_id)
                    if not pid:
                        return None, None, device
                    config = await lib.CoreProfileSessionTap.get_time_config(dvt)
                    async with lib.CoreProfileSessionTap(dvt, config) as tap:
                        raw = await tap.get_stackshot(timeout=_STACKSHOT_TIMEOUT_S)
                    return int(pid), raw, device
            finally:
                with contextlib.suppress(Exception):
                    await rsd.close()
    except TimeoutError as e:
        if deadline.expired():
            raise DeviceError(f"{uuid[:8]} did not answer within {budget:g}s",
                              tool="pymobiledevice3") from None
        raise DeviceError(f"the stackshot from {uuid[:8]} timed out: {e!r}",
                          tool="pymobiledevice3") from e
    except DeviceError:
        raise
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        if type(e).__name__ == "ExtractingStackshotError":
            # pymobiledevice3's own timeout, not asyncio's.
            raise DeviceError(f"{uuid[:8]} sent no stackshot within {_STACKSHOT_TIMEOUT_S:g}s "
                              f"({e})", tool="pymobiledevice3") from e
        if "kperf" in str(e).lower():
            # One kernel profiler per device: an Instruments or xctrace
            # recording holds it, quern's own profiles included.
            raise DeviceError(
                f"{uuid[:8]}'s kernel profiler is in use by another session -- an "
                f"Instruments or xctrace recording, or a quern profile on this device; "
                f"ask again once it ends ({e})", tool="pymobiledevice3") from e
        name = type(e).__name__
        raise DeviceError(f"the stackshot from {uuid[:8]} failed: {name}: {e}",
                          tool="pymobiledevice3") from e


async def device_stacks(
    uuid: str, bundle_id: str, finder: symbolicate.SymbolFinder, *,
    all_threads: bool, max_frames: int,
) -> ThreadStacks:
    out = ThreadStacks(udid=uuid, bundle_id=bundle_id, platform="ios_device",
                       running=False, source="stackshot")
    pid, raw, device = await take_stackshot(uuid, bundle_id)
    if pid is None:
        out.unavailable["threads"] = performance.NOT_RUNNING
        return out
    out.running, out.pid = True, pid
    shot = read_shot(raw or {}, pid, device)
    if shot is None or not shot.threads:
        # The app was running a moment before; gone now, or the shot is short.
        out.unavailable["threads"] = (f"the stackshot holds no threads for pid {pid}: the "
                                      f"app may have exited as it was taken")
        return out
    out.thread_count = len(shot.threads)
    # Chosen and cut before symbolicating: every image looked up costs a
    # lookup and an atos call, and frames nobody sees are not worth either.
    main = next((i for i, t in enumerate(shot.threads) if t[2] == MAIN_QUEUE), 0)
    order = [main] + ([i for i in range(len(shot.threads)) if i != main] if all_threads else [])
    shot.threads = [shot.threads[i] for i in order]
    cut = [max(0, len(t[3]) - max_frames) for t in shot.threads]
    shot.threads = [(tid, name, queue, lrs[:max_frames]) for tid, name, queue, lrs in shot.threads]
    threads, out.symbols = await symbolicate_shot(shot, finder, bundle_id)
    threads[0].main = True
    for t, n in zip(threads, cut, strict=True):
        t.frames_cut = n
    out.threads = threads
    return out


# -- symbolicating a stackshot -----------------------------------------------

#: DeviceSupport folder -> (its tree's signature, the sorted __TEXT ranges of
#: its images).
_INDEX: dict[str, tuple[tuple[int, int], list[tuple[int, int, str]]]] = {}
_INDEX_LOCKS: dict[str, asyncio.Lock] = {}


def device_support_dir(product_type: str, version: str, build: str,
                       root: Path | None = None) -> Path | None:
    """Xcode's folder of this OS's symbols: `iPhone12,1 26.7 (23H24)`, or the
    older `26.7 (23H24)`. None when it has never been copied."""
    root = root or DEVICE_SUPPORT
    for name in (f"{product_type} {version} ({build})", f"{version} ({build})"):
        candidate = root / name / "Symbols"
        if name.strip() and candidate.is_dir():
            return candidate
    return None


def _text_range(path: str) -> tuple[int, int] | None:
    """A Mach-O's __TEXT segment (vmaddr, vmsize): its arm64(e) slice of a fat
    file. None for anything else."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
            if head[:4] == b"\xca\xfe\xba\xbe":
                count = struct.unpack(">I", head[4:8])[0]
                for i in range(min(count, 16)):
                    cpu, _sub, off, _size, _align = struct.unpack(
                        ">iiIII", head[8 + 20 * i:28 + 20 * i])
                    if cpu == 0x0100000C:            # CPU_TYPE_ARM64
                        f.seek(off)
                        head = f.read(65536)
                        break
                else:
                    return None
    except (OSError, struct.error):
        return None
    if head[:4] != b"\xcf\xfa\xed\xfe":
        return None
    try:
        ncmds = struct.unpack("<I", head[16:20])[0]
        at = 32
        for _ in range(ncmds):
            cmd, size = struct.unpack("<II", head[at:at + 8])
            if cmd == 0x19 and head[at + 8:at + 24].rstrip(b"\0") == b"__TEXT":  # LC_SEGMENT_64
                return struct.unpack("<QQ", head[at + 24:at + 40])
            if size <= 0:
                return None
            at += size
    except struct.error:
        return None                           # load commands past what was read
    return None


def build_index(symbols: Path) -> list[tuple[int, int, str]]:
    """Every image the shared cache was extracted into, as (start, end, path)
    in its unslid addresses, sorted."""
    index = []
    for root, _dirs, files in os.walk(symbols):
        for name in files:
            path = os.path.join(root, name)
            found = _text_range(path)
            if found and found[0] >= SHARED_REGION_BASE and found[1] > 0:
                index.append((found[0], found[0] + found[1], path))
    index.sort()
    return index


def tree_signature(symbols: Path) -> tuple[int, int]:
    """(files, newest folder mtime) under `symbols`. Xcode extracts a device's
    symbols into nested folders, so a file it adds changes its own folder's
    mtime and none of its parents': the top folder's alone would keep an
    index built mid-copy for the server's life (review)."""
    files, newest = 0, 0
    for root, _dirs, names in os.walk(symbols):
        files += len(names)
        with contextlib.suppress(OSError):
            newest = max(newest, os.stat(root).st_mtime_ns)
    return files, newest


async def _index(symbols: Path) -> list[tuple[int, int, str]]:
    key = str(symbols)
    # One build per folder at a time: the first request after a start builds
    # it, and any others that arrive meanwhile take its result.
    async with _INDEX_LOCKS.setdefault(key, asyncio.Lock()):
        signature = await asyncio.to_thread(tree_signature, symbols)
        cached = _INDEX.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        index = await asyncio.to_thread(build_index, symbols)
        if index:                             # an empty one is asked again next time
            _INDEX[key] = (signature, index)
        else:
            _INDEX.pop(key, None)
        return index


_BARE_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")


def _covering(index: list[tuple[int, int, str]], address: int) -> tuple[int, int, str] | None:
    i = bisect.bisect_right(index, (address, float("inf"), "")) - 1
    if i >= 0 and index[i][0] <= address < index[i][1]:
        return index[i]
    return None


async def symbolicate_shot(
    shot: Shot, finder: symbolicate.SymbolFinder, bundle_id: str,
) -> tuple[list[StackThread], list[ImageSymbols]]:
    """Each thread's frames, symbolicated as far as this Mac can."""
    read = symbolicate.Read()
    symbols: list[ImageSymbols] = []
    cache = shot.cache_base is not None and shot.slide is not None
    index: list[tuple[int, int, str]] = []
    ds_note = ""
    if cache:
        folder = device_support_dir(*shot.device)
        if folder is None:
            kind, version, build = shot.device
            ds_note = (f"no DeviceSupport symbols for {kind} {version} ({build}) in "
                       f"{DEVICE_SUPPORT}: the OS's frames stay addresses until Xcode copies "
                       f"them, which it does when the device is first used with it "
                       f"(Window > Devices and Simulators)")
        else:
            index = await _index(folder)
            if not index:
                ds_note = f"{folder} holds no images quern can read"

    # Every frame is a return address except each thread's innermost, which
    # is the instruction itself: crash tools look the others up a byte back.
    def back(i: int) -> int:
        return 0 if i == 0 else 1

    # The app's own images are looked up first: a dSYM found says how far
    # its image reaches, so a frame past its end is not put in it.
    images: dict[int, _AppImage] = {}
    for base, uuid in shot.images:
        images[base] = await _look_up(base, uuid, finder, read)
    bases = sorted(images)

    threads: list[StackThread] = []
    app_todo: dict[int, list[tuple[CrashFrame, int]]] = {}       # load address -> frames
    sys_todo: dict[str, list[tuple[CrashFrame, int, int]]] = {}  # image path -> frames
    in_cache_unindexed = outside = 0
    for tid, name, queue, lrs in shot.threads:
        frames = []
        for i, lr in enumerate(lrs):
            frame = CrashFrame()
            if cache and lr >= shot.cache_base:
                unslid = lr - shot.slide
                hit = _covering(index, unslid) if index else None
                if hit is None:
                    frame.image = "dyld_shared_cache"
                    frame.offset = lr - shot.cache_base
                    in_cache_unindexed += 1
                else:
                    frame.image, frame.offset = Path(hit[2]).name, unslid - hit[0]
                    sys_todo.setdefault(hit[2], []).append((frame, unslid - back(i), back(i)))
            else:
                at = bisect.bisect_right(bases, lr) - 1
                image = images[bases[at]] if at >= 0 else None
                if image is not None and (image.size is None or lr - image.base < image.size):
                    frame.image, frame.app = image.name, image.app
                    frame.offset = lr - image.base
                    app_todo.setdefault(image.base, []).append((frame, i))
                else:
                    outside += 1
            frames.append(frame)
        threads.append(StackThread(id=tid, name=name, queue=queue,
                                   main=queue == MAIN_QUEUE, frames=frames))

    for base, todo in app_todo.items():
        symbols.append(await _app_image(images[base], todo, finder, read))
    for path, todo in sys_todo.items():
        symbols.append(await _system_image(Path(path), todo, finder))
    if in_cache_unindexed:
        symbols.append(ImageSymbols(
            image="dyld_shared_cache", uuid=shot.cache_uuid, frames_total=in_cache_unindexed,
            note=ds_note or (
                f"{in_cache_unindexed} frames are in the shared cache but in none of the images "
                f"in {device_support_dir(*shot.device)}: the folder may be incomplete -- Xcode "
                f"copies it the first time the device is used, and a copy can be cut short")))
    if outside:
        symbols.append(ImageSymbols(
            image="", frames_total=outside,
            note=f"{outside} frames are in no image the stackshot lists: code made at run "
                 f"time, or an address past the end of an image"))
    return threads, symbols


@dataclass
class _AppImage:
    """One of the task's own (non-cache) images, after its symbols were looked for."""

    base: int
    uuid: str
    name: str
    #: Its __TEXT size from the dSYM; None when no dSYM was found.
    size: int | None
    #: Code the app was built from: a dSYM for it was found on this Mac.
    #: Without one nothing says it is the app's rather than any other image
    #: loaded outside the shared cache.
    app: bool
    found: bool


async def _look_up(base: int, uuid: str, finder: symbolicate.SymbolFinder,
                   read: symbolicate.Read) -> _AppImage:
    lookup = await finder.find(uuid, read)
    if lookup.found is None:
        return _AppImage(base, uuid, f"image {uuid[:8]}", None, False, False)
    dwarf = Path(lookup.found.dwarf)
    text = await asyncio.to_thread(_text_range, str(dwarf))
    return _AppImage(base, uuid, dwarf.name, text[1] if text else None, True, True)


async def _app_image(image: _AppImage, todo: list[tuple[CrashFrame, int]],
                     finder: symbolicate.SymbolFinder, read: symbolicate.Read) -> ImageSymbols:
    """One of the task's own images, the way a crash's is: by UUID, through
    the crash symbolicator."""
    frames = [f for f, _ in todo]
    crash_image = CrashImage(name=image.name, uuid=image.uuid, base=image.base, arch="arm64")
    exact = {(image.name, f.offset) for f, i in todo if i == 0}
    entry = ImageSymbols(image=image.name, uuid=symbolicate.normalise_uuid(image.uuid)
                         or image.uuid, frames_total=len({f.offset for f in frames}))
    try:
        await symbolicate._one_image(crash_image, frames, exact, entry, finder, read)
    except Exception as e:  # noqa: BLE001 -- the stacks are still stacks
        entry.note = f"symbolication failed: {type(e).__name__}: {e}"
    if not image.found:
        entry.note = (entry.note + "; " if entry.note else "") + (
            "without its dSYM nothing says whether this is the app's own code or another "
            "image loaded outside the shared cache, so its frames are not marked app")
    return entry


async def _system_image(path: Path, todo: list[tuple[CrashFrame, int, int]],
                        finder: symbolicate.SymbolFinder) -> ImageSymbols:
    """One OS image from DeviceSupport, in the shared cache's unslid addresses."""
    entry = ImageSymbols(image=path.name, source="device_support", dwarf=str(path),
                         frames_total=len({f.offset for f, _, _ in todo}))
    text = await asyncio.to_thread(_text_range, str(path))
    if text is None:
        entry.note = f"{path} could not be read"
        return entry
    wanted = sorted({at for _, at, _ in todo})
    try:
        code, out, err = await finder.run(
            ["xcrun", "atos", "-o", str(path), "-l", hex(text[0]), *(hex(a) for a in wanted)])
    except (OSError, TimeoutError) as e:
        entry.note = f"atos could not run: {type(e).__name__}: {e}"
        return entry
    lines = out.splitlines()
    if code != 0 or len(lines) != len(wanted):
        entry.note = (f"atos exited {code}: {err.strip()[:160]}" if code != 0
                      else f"atos gave {len(lines)} lines for {len(wanted)} addresses")
        return entry
    answers = dict(zip(wanted, (symbolicate._parse(ln) for ln in lines), strict=True))
    resolved = set()
    for frame, at, b in todo:
        hit = answers.get(at)
        if hit is None or _BARE_ADDRESS.fullmatch(hit[0]):
            # A function the OS stripped: atos names it by its own address
            # (`0x00000001893e8aa3 (in UIKitCore) + 104`, measured), which is
            # no name at all.
            continue
        symbol, symbol_offset, file, line = hit
        frame.symbol = symbol
        frame.symbol_offset = symbol_offset + b if symbol_offset is not None else None
        frame.file, frame.line = file, line
        resolved.add(frame.offset)
    entry.frames_resolved = len(resolved)
    if entry.frames_resolved < entry.frames_total:
        entry.note = (f"{entry.frames_total - entry.frames_resolved} of {entry.frames_total} "
                      f"frames have no symbol in it (a local function the OS strips)")
    return entry


# -- shared --------------------------------------------------------------------

def _keep(out: ThreadStacks, threads: list[StackThread], all_threads: bool,
          max_frames: int) -> ThreadStacks:
    """The main thread first, the others only when asked for; each cut to
    `max_frames`, innermost kept."""
    threads = sorted(threads, key=lambda t: not t.main)
    out.thread_count = len(threads)
    kept = threads if all_threads else threads[:1]
    for t in kept:
        if len(t.frames) > max_frames:
            t.frames_cut = len(t.frames) - max_frames
            t.frames = t.frames[:max_frames]
    out.threads = kept
    return out


def android_stacks(serial: str, package: str) -> ThreadStacks:
    return ThreadStacks(udid=serial, bundle_id=package, platform="android", running=None,
                        unavailable={"threads": ANDROID_UNSUPPORTED})
