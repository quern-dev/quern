"""Crash frames and images, kept as the report gives them (#326).

A crash report says where it crashed and, for the app's own code, often in
which file and line. The Mac's crash reporter resolves a simulator app's
frames to function and source line, and a Debug build's frames arrive from a
phone with their function names. quern kept the bare `symbol` of the first
five frames and threw the rest away: the image a frame was in, its offset,
source file and line, and every binary's UUID and load address -- which is
everything that would be needed to resolve a frame the report did not.

Here each frame keeps its image, offset, symbol, source file and line; the
first frame in the app's own code is picked out; and the images those frames
point into are kept with UUID and load address.
"""

from __future__ import annotations

import posixpath
import re

from server.models import CrashFrame, CrashImage

#: The crashing thread can be deep (a recursion, a long UIKit stack); past
#: this, frames only cost the caller tokens.
MAX_FRAMES = 30
#: As `top_frames`, formatted.
TOP_FRAMES = 8


def format_frame(frame: CrashFrame) -> str:
    """`UIKitCore: UIApplicationMain + 332`, `MyApp + 0x2aec` when unresolved,
    with `(File.swift:12)` when the report has the source line."""
    if frame.symbol:
        text = frame.symbol
        if frame.symbol_offset is not None:
            text += f" + {frame.symbol_offset}"
    elif frame.offset is not None:
        text = f"0x{frame.offset:x}"
    else:
        text = "?"
    if frame.image:
        text = f"{frame.image}: {text}"
    if frame.file:
        text += f" ({frame.file}:{frame.line})" if frame.line is not None else f" ({frame.file})"
    return text


def first_app_frame(frames: list[CrashFrame]) -> CrashFrame | None:
    return next((f for f in frames if f.app), None)


# -- iOS .ips -------------------------------------------------------------------


def ips_frames(data: dict) -> tuple[list[CrashFrame], list[CrashImage]]:
    """The faulting thread's frames and the images they point into.

    A frame is the app's when its image lies inside the app's bundle -- the
    directory holding `procPath` -- which covers the main binary, a Debug
    build's `.debug.dylib` and embedded frameworks, and nothing of the OS.
    """
    used = data.get("usedImages")
    used = used if isinstance(used, list) else []
    bundle = _bundle_dir(data.get("procPath") or "")
    process = data.get("procName") or ""
    threads = data.get("threads")
    threads = threads if isinstance(threads, list) else []
    faulting = data.get("faultingThread")
    if not isinstance(faulting, int) or not 0 <= faulting < len(threads):
        faulting = next((i for i, t in enumerate(threads)
                         if isinstance(t, dict) and t.get("triggered")), None)
    if faulting is None or not isinstance(threads[faulting], dict):
        return [], []
    raw_frames = threads[faulting].get("frames")
    raw_frames = raw_frames if isinstance(raw_frames, list) else []

    frames: list[CrashFrame] = []
    wanted: dict[int, CrashImage] = {}
    for raw in raw_frames[:MAX_FRAMES]:
        if not isinstance(raw, dict):
            continue
        index = raw.get("imageIndex")
        image = used[index] if isinstance(index, int) and 0 <= index < len(used) else {}
        image = image if isinstance(image, dict) else {}
        path = image.get("path") or ""
        name = image.get("name") or (posixpath.basename(path) if path else "")
        frames.append(CrashFrame(
            image=name,
            offset=_int(raw.get("imageOffset")),
            symbol=raw.get("symbol") or "",
            symbol_offset=_int(raw.get("symbolLocation")),
            file=raw.get("sourceFile") or "",
            line=_int(raw.get("sourceLine")),
            app=_is_app(name, path, bundle, process),
        ))
        if isinstance(index, int) and image and index not in wanted:
            wanted[index] = CrashImage(
                name=name, uuid=image.get("uuid") or "", base=_int(image.get("base")),
                path=path, arch=image.get("arch") or "",
            )
    return frames, list(wanted.values())


def _is_app(name: str, path: str, bundle: str, process: str) -> bool:
    """Inside the app's bundle; or, where paths are elided, named after the
    process -- the main binary, or its Debug build's `.debug.dylib`."""
    if bundle and path.startswith(bundle + "/"):
        return True
    return bool(process) and name in (process, f"{process}.debug.dylib")


_SIMULATOR = re.compile(r"/CoreSimulator/Devices/([0-9A-Fa-f-]{36})/")


def simulator_udid(proc_path: str) -> str:
    """The simulator a report came from, read from its app's path; "" if none.

    A simulator's crash file names no device, so a report without one was
    listed under every device's udid -- an iPhone's list included the
    simulator's crashes.
    """
    m = _SIMULATOR.search(proc_path)
    return m.group(1).upper() if m else ""


def _bundle_dir(proc_path: str) -> str:
    """`/…/MyApp.app` for `/…/MyApp.app/MyApp`; "" when it is not an app bundle."""
    head = posixpath.dirname(proc_path)
    return head if head.endswith(".app") else ""


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


# -- iOS .crash text (iOS 14 and older) ------------------------------------------

#: `3   MyApp    0x0000000102a4c123 -[Foo bar] + 12 (Foo.m:40)`, or unsymbolicated
#: `3   MyApp    0x0000000102a4c123 0x102a40000 + 49443`.
_TEXT_FRAME = re.compile(r"^\d+\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(.+?)\s*$")
_TEXT_UNRESOLVED = re.compile(r"^(0x[0-9a-fA-F]+) \+ (\d+)$")
_TEXT_SYMBOL = re.compile(r"^(.+?) \+ (\d+)(?: \(([^():]+)(?::(\d+))?\))?$")
#: `0x102a40000 - 0x10345ffff MyApp arm64  <0f1e2d...> /var/…/MyApp.app/MyApp`
_TEXT_IMAGE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+)\s+-\s+0x[0-9a-fA-F]+\s+\+?(\S+)\s+(\S+)\s+<([0-9a-fA-F-]+)>\s+(\S.*)$",
)


def crash_text_frames(content: str) -> tuple[list[CrashFrame], list[CrashImage]]:
    """The crashed thread's frames from a text report, and their images."""
    images_by_name: dict[str, CrashImage] = {}
    section = content.split("Binary Images:", 1)
    if len(section) == 2:
        for line in section[1].splitlines():
            m = _TEXT_IMAGE.match(line)
            if m:
                images_by_name.setdefault(m.group(2), CrashImage(
                    name=m.group(2), base=int(m.group(1), 16), arch=m.group(3),
                    uuid=m.group(4), path=m.group(5).strip(),
                ))
    proc = re.search(r"^Path:\s+(\S.*)$", content, re.M)
    bundle = _bundle_dir(proc.group(1).strip()) if proc else ""
    process = re.search(r"^Process:\s+(\S+)", content, re.M)
    process = process.group(1) if process else ""

    crashed = re.search(r"^Thread \d+ Crashed:.*\n((?:\d+\s+.+\n?)+)", content, re.M)
    frames: list[CrashFrame] = []
    used: dict[str, CrashImage] = {}
    for line in (crashed.group(1).splitlines() if crashed else [])[:MAX_FRAMES]:
        m = _TEXT_FRAME.match(line.strip())
        if not m:
            continue
        name, address, rest = m.group(1), int(m.group(2), 16), m.group(3)
        image = images_by_name.get(name)
        frame = CrashFrame(image=name, app=_is_app(
            name, image.path if image else "", bundle, process))
        if u := _TEXT_UNRESOLVED.match(rest):
            frame.offset = int(u.group(2))
        elif s := _TEXT_SYMBOL.match(rest):
            frame.symbol, frame.symbol_offset = s.group(1), int(s.group(2))
            frame.file = s.group(3) or ""
            frame.line = int(s.group(4)) if s.group(4) else None
        if frame.offset is None and image is not None and image.base is not None:
            frame.offset = address - image.base
        frames.append(frame)
        if image is not None:
            used.setdefault(name, image)
    return frames, list(used.values())


# -- Android ---------------------------------------------------------------------

#: `#00 pc 000000000009e498  /apex/…/libc.so (abort+168) (BuildId: cd79…)`
_NATIVE = re.compile(r"^\s*#\d+ pc ([0-9a-fA-F]+)\s+(\S+)(.*)$")
_NATIVE_BUILD_ID = re.compile(r"\s*\(BuildId: ([0-9a-fA-F]+)\)\s*$")
_NATIVE_APK_OFFSET = re.compile(r"\s*\(offset 0x[0-9a-fA-F]+\)")
#: `at com.example.Foo.bar(Foo.java:42)`, `(Native Method)`, `(Unknown Source:3)`
_JAVA = re.compile(r"^\s*at (\S+?)\((.*)\)\s*$")
_JAVA_SOURCE = re.compile(r"^([^:]+?)(?::(\d+))?$")
#: The platform's and the common libraries' packages: not the app's own code.
_JAVA_NOT_APP = (
    "android.", "androidx.", "com.android.internal.", "dalvik.", "java.", "javax.",
    "jdk.", "kotlin.", "kotlinx.", "libcore.", "sun.", "org.apache.", "org.json.",
    "com.google.android.material.", "com.google.android.gms.", "okhttp3.", "okio.",
    "retrofit2.", "io.reactivex.",
)


def native_frames(lines: list[str]) -> list[CrashFrame]:
    """Frames from tombstone backtrace lines. A frame is the app's when its
    library was installed with the app (`/data/app/…`)."""
    frames = []
    for line in lines[:MAX_FRAMES]:
        m = _NATIVE.match(line)
        if not m:
            continue
        path, rest = m.group(2), m.group(3)
        build_id = _NATIVE_BUILD_ID.search(rest)
        if build_id:
            rest = rest[:build_id.start()]
        symbol, symbol_offset = _native_symbol(_NATIVE_APK_OFFSET.sub("", rest).strip())
        frames.append(CrashFrame(
            image=posixpath.basename(path.split("!")[-1]),
            offset=int(m.group(1), 16),
            symbol=symbol,
            symbol_offset=symbol_offset,
            build_id=build_id.group(1) if build_id else "",
            app=path.startswith("/data/app/"),
        ))
    return frames


def _native_symbol(text: str) -> tuple[str, int | None]:
    """`(android::Looper::pollOnce(int, int*)+112)` -> the symbol and 112.

    The outer brackets, not the first ")": C++ symbols carry their own.
    """
    if not (text.startswith("(") and text.endswith(")")):
        return "", None
    inner = text[1:-1]
    name, sep, offset = inner.rpartition("+")
    if sep and offset.isdigit():
        return name.strip(), int(offset)
    return inner.strip(), None


def java_frames(lines: list[str]) -> list[CrashFrame]:
    """Frames from `at …` lines. A frame is the app's unless its class is in a
    platform or common-library package -- a heuristic, since the report does
    not say which classes the app shipped."""
    frames = []
    for line in lines[:MAX_FRAMES]:
        m = _JAVA.match(line)
        if not m:
            continue
        symbol, where = m.group(1), m.group(2)
        source = _JAVA_SOURCE.match(where) if where not in ("Native Method", "") else None
        file = source.group(1) if source and source.group(1) != "Unknown Source" else ""
        frames.append(CrashFrame(
            symbol=symbol,
            file=file,
            line=int(source.group(2)) if source and source.group(2) and file else None,
            app=not symbol.startswith(_JAVA_NOT_APP),
        ))
    return frames
