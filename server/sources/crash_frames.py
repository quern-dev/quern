"""Crash frames and images, kept as the report gives them (#326).

A crash report says where it crashed and, for the app's own code, often in
which file and line. The Mac's crash reporter resolves a simulator app's
frames to function and source line, and a Debug build's frames arrive from a
phone with their function names. quern kept the bare `symbol` of the first
five frames and threw the rest away: the image a frame was in, its offset,
source file and line, and every binary's UUID and load address -- which is
everything that would be needed to resolve a frame the report did not.

Here each frame keeps its image, offset, symbol, source file and line; the
frame that says where in the app's code it happened is picked out; and the
images the frames point into are kept with UUID and load address.

Picking that frame is most of the work, and a real crash is what settled it.
A Swift `fatalError` from the Geocaching app's debug menu (EXC_BREAKPOINT)
put Swift's `_assertionFailure` at the top of the crashing thread and the
app's own `DebugMenuPresenter.attemptToCorruptSqlite()` at
`DebugMenuPresenter.swift:368` right under it: the first app frame. What is
not the answer: an uncaught exception's crashing thread, which is only
`abort` under the run loop -- the throw site is in `lastExceptionBacktrace`;
a crash reporter's signal handler, which runs on the crashing thread above
`_sigtramp`; the app's entry point, which is where an idle main thread sits;
and any frame at all when another process ended the app (`killed_by`).
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

#: The app's entry point: where an idle main thread sits, under
#: UIApplicationMain. Not where anything went wrong -- when the run loop is
#: above it. An abort() called from main itself is main.
_ENTRY_POINTS = ("main", "__debug_main_executable_dylib_entry_point")
_RUN_LOOP = ("UIApplicationMain", "NSApplicationMain", "CFRunLoopRun", "GSEventRunModal",
             "WKApplicationMain", "_dispatch_main_queue")
#: `byProc` when `byPid` is missing: the name a real crash carries. Measured
#: on every self-inflicted report seen; the process's own name also shows up
#: (an abort(), an uncaught exception), truncated to 32 characters.
_SELF = ("", "exc handler")


def format_frame(frame: CrashFrame) -> str:
    """`UIKitCore: UIApplicationMain + 332`, `MyApp: 0x2aec` when unresolved,
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
    """Where in the app's code it happened: the first app frame, not counting a
    signal handler's frames, or the app's entry point where the run loop sits
    above it."""
    start = next((i + 1 for i, f in enumerate(frames) if f.symbol == "_sigtramp"), 0)
    for i in range(start, len(frames)):
        frame = frames[i]
        if frame.app and not (_is_entry_point(frame) and _run_loop_above(frames[start:i])):
            return frame
    return None


def _is_entry_point(frame: CrashFrame) -> bool:
    return frame.symbol in _ENTRY_POINTS or frame.symbol.endswith("$main()")


def _run_loop_above(frames: list[CrashFrame]) -> bool:
    return any(any(name in f.symbol for name in _RUN_LOOP) for f in frames)


def _str(value) -> str:
    """A report field as text; anything else is treated as absent."""
    return value if isinstance(value, str) else ""


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:          # includes a string past Python's digit limit
            return None
    return None


# -- iOS .ips -------------------------------------------------------------------


def ips_frames(data: dict) -> tuple[list[CrashFrame], list[CrashImage], bool]:
    """The frames that explain the crash, their images, and whether they are
    the exception's backtrace rather than the crashing thread.

    An uncaught exception's crashing thread is only `abort` under the run
    loop; the throw site is in `lastExceptionBacktrace`, which is used when
    the report has one.

    A frame is the app's when its image lies inside the app's bundle -- the
    outermost `.app` in `procPath`, so an extension's host app counts too --
    or, where paths are elided, is named after the process.
    """
    used = data.get("usedImages")
    used = used if isinstance(used, list) else []
    bundle = _bundle_dir(_str(data.get("procPath")))
    process = _str(data.get("procName"))

    exception = data.get("lastExceptionBacktrace")
    from_exception = isinstance(exception, list) and bool(exception)
    if from_exception:
        raw_frames = exception
    else:
        threads = data.get("threads")
        threads = threads if isinstance(threads, list) else []
        faulting = data.get("faultingThread")
        if isinstance(faulting, bool) or not isinstance(faulting, int) \
                or not 0 <= faulting < len(threads):
            faulting = next((i for i, t in enumerate(threads)
                             if isinstance(t, dict) and t.get("triggered")), None)
        if faulting is None or not isinstance(threads[faulting], dict):
            return [], [], False
        raw_frames = threads[faulting].get("frames")
        raw_frames = raw_frames if isinstance(raw_frames, list) else []

    # Every frame, so the app frame can be picked from the whole stack; the
    # caller caps what is returned. A deep SwiftUI or UIKit stack can put the
    # app's frame past any cap.
    frames: list[CrashFrame] = []
    wanted: dict[int, CrashImage] = {}
    for raw in raw_frames:
        if not isinstance(raw, dict):
            continue
        index = raw.get("imageIndex")
        valid = isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(used)
        image = used[index] if valid else {}
        image = image if isinstance(image, dict) else {}
        path = _str(image.get("path"))
        name = _str(image.get("name")) or (posixpath.basename(path) if path else "")
        frames.append(CrashFrame(
            image=name,
            offset=_int(raw.get("imageOffset")),
            symbol=_str(raw.get("symbol")),
            symbol_offset=_int(raw.get("symbolLocation")),
            file=_str(raw.get("sourceFile")),
            line=_int(raw.get("sourceLine")),
            app=_is_app(name, path, bundle, process),
        ))
        if valid and image and index not in wanted:
            wanted[index] = CrashImage(
                name=name, uuid=_str(image.get("uuid")), base=_int(image.get("base")),
                path=path, arch=_str(image.get("arch")),
            )
    return frames, list(wanted.values()), from_exception


def ips_reason(data: dict) -> str:
    """The report's own words for why.

    `asi`, the application-specific information ("Terminating app due to
    uncaught exception …"); and for a termination that was not a signal -- a
    watchdog, a memory limit -- its explanation. A Swift `fatalError` puts its
    message in the app's log, not the report.
    """
    parts: list[str] = []
    asi = data.get("asi")
    if isinstance(asi, dict):
        parts += [line.strip() for values in asi.values() if isinstance(values, list)
                  for line in values if isinstance(line, str) and line.strip()]
    termination = data.get("termination")
    if isinstance(termination, dict) and _str(termination.get("namespace")) not in ("", "SIGNAL"):
        reasons = termination.get("reasons")
        if isinstance(reasons, list):
            parts += [r.strip() for r in reasons if isinstance(r, str) and r.strip()]
        elif _str(termination.get("indicator")):
            parts.append(_str(termination.get("indicator")))
    return " ".join(parts)[:500]


def ips_killed_by(data: dict) -> str:
    """The process that sent the signal that ended the app, when it was not
    the app itself.

    Decided by pid, not name: `byPid` equals the app's `pid` for every
    self-inflicted crash measured -- `exc handler` for a fault, the app's own
    name for an abort() or an uncaught exception, which the kernel truncates
    to 32 characters, so a longer name never matches. A kill from a shell
    (`zsh`, `python3.11`) or `devicectl` (`dtappserviced`) has another pid.

    Only for a signal. A watchdog or a memory limit is the system ending the
    app too, but there the frames are where it hung, which is the answer --
    so it keeps its frames, and its explanation goes in `reason`.
    """
    termination = data.get("termination")
    if not isinstance(termination, dict) or _str(termination.get("namespace")) != "SIGNAL":
        return ""
    by = _str(termination.get("byProc"))
    by_pid, pid = _int(termination.get("byPid")), _int(data.get("pid"))
    if by_pid is not None and pid is not None:
        return "" if by_pid == pid else (by or f"pid {by_pid}")
    return "" if by in _SELF or by == _str(data.get("procName"))[:32] else by


def _is_app(name: str, path: str, bundle: str, process: str) -> bool:
    """Inside the app's bundle; or, where the path is elided, named after the
    process -- the main binary, or its Debug build's `.debug.dylib`. Not by
    name when the path is known: an app named `Contacts` would otherwise claim
    `/System/Library/Frameworks/Contacts.framework`."""
    if path:
        return bool(bundle) and path.startswith(bundle + "/")
    return bool(process) and name in (process, f"{process}.debug.dylib")


def _bundle_dir(proc_path: str) -> str:
    """The outermost `…/X.app` in the path; "" when there is none.

    Outermost, so an extension (`MyApp.app/PlugIns/Widget.appex/Widget`) is
    counted with the app that hosts it and its frameworks. Only an `.app`: a
    daemon at `/usr/libexec/foo` would otherwise make every image beside it
    the app's.
    """
    m = re.match(r"^(.*?\.app)/", proc_path)
    return m.group(1) if m else ""


_SIMULATOR = re.compile(r"/CoreSimulator/Devices/([0-9A-Fa-f-]{36})/")


def simulator_udid(proc_path: str) -> str:
    """The simulator a report came from, read from its app's path; "" if none.

    A simulator's crash file names no device, so a report without one was
    listed under every device's udid -- an iPhone's list included the
    simulator's crashes.
    """
    m = _SIMULATOR.search(proc_path) if isinstance(proc_path, str) else None
    return m.group(1).upper() if m else ""


# -- iOS .crash text (iOS 14 and older) ------------------------------------------

#: `3   MyApp    0x0000000102a4c123 -[Foo bar] + 12 (Foo.m:40)`. The image name
#: may contain spaces: it is whatever lies between the frame number and the
#: address.
#: `Process:  My App [12345]`: the name runs to the pid, spaces and all.
_TEXT_PROCESS = re.compile(r"^Process:\s+(.+?)(?:\s+\[\d+\])?\s*$", re.M)
_TEXT_FRAME = re.compile(r"^\d+\s+(.+?)\s+(0x[0-9a-fA-F]+)\s+(.+?)\s*$")
#: Unsymbolicated: `0x102a40000 + 49443`, or the macOS style `MyApp + 49443`.
_TEXT_UNRESOLVED = re.compile(r"^(0x[0-9a-fA-F]+|.+?) \+ (\d+)$")
_TEXT_SYMBOL = re.compile(r"^(.+?) \+ (\d+)(?: \(([^():]+)(?::(\d+))?\))?$")
#: `0x102a40000 - 0x10345ffff MyApp arm64  <0f1e2d...> /var/…/MyApp.app/MyApp`
_TEXT_IMAGE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+)\s+-\s+0x[0-9a-fA-F]+\s+\+?(.+?)\s+(\S+)\s+<([0-9a-fA-F-]+)>\s+(\S.*)$",
)


def crash_text_frames(content: str) -> tuple[list[CrashFrame], list[CrashImage]]:
    """The crashed thread's frames from a text report, and their images.

    Every frame, so the caller picks the app's before it caps them."""
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
    process = _TEXT_PROCESS.search(content)
    process = process.group(1) if process else ""

    crashed = re.search(r"^Thread \d+ Crashed:.*\n((?:\d+\s+.+\n?)+)", content, re.M)
    frames: list[CrashFrame] = []
    used: dict[str, CrashImage] = {}
    for line in crashed.group(1).splitlines() if crashed else []:
        m = _TEXT_FRAME.match(line.strip())
        if not m:
            continue
        name, address, rest = m.group(1), int(m.group(2), 16), m.group(3)
        image = images_by_name.get(name)
        frame = CrashFrame(image=name, app=_is_app(
            name, image.path if image else "", bundle, process))
        u = _TEXT_UNRESOLVED.match(rest)
        if u and (u.group(1).startswith("0x") or u.group(1) == name):
            frame.offset = _int(u.group(2))
        elif s := _TEXT_SYMBOL.match(rest):
            frame.symbol, frame.symbol_offset = s.group(1), _int(s.group(2))
            frame.file = s.group(3) or ""
            frame.line = _int(s.group(4)) if s.group(4) else None
        if frame.offset is None and image is not None and image.base is not None:
            frame.offset = address - image.base
        frames.append(frame)
        if image is not None:
            used.setdefault(name, image)
    return frames, list(used.values())


# -- Android ---------------------------------------------------------------------

#: `#00 pc 000000000009e498  /apex/…/libc.so (abort+168) (BuildId: cd79…)`. The
#: path may contain spaces (`/memfd:jit-cache (deleted)`).
_NATIVE = re.compile(r"^\s*#\d+ pc ([0-9a-fA-F]+)\s+(.*)$")
_NATIVE_BUILD_ID = re.compile(r"\s*\(BuildId: ([0-9a-fA-F]+)\)\s*$")
_NATIVE_APK_OFFSET = re.compile(r"\s*\(offset 0x[0-9a-fA-F]+\)")
#: `at com.example.Foo.bar(Foo.java:42)`, `(Native Method)`, `(Unknown Source:3)`
_JAVA = re.compile(r"^\s*at (\S+?)\((.*)\)\s*$")
_JAVA_SOURCE = re.compile(r"^([^:]+?)(?::(\d+))?$")
#: The platform's and common libraries' packages: not the app's own code. Used
#: only when the record does not name the app's package.
_JAVA_NOT_APP = (
    "android.", "androidx.", "com.android.", "dalvik.", "java.", "javax.",
    "jdk.", "kotlin.", "kotlinx.", "libcore.", "sun.", "org.apache.", "org.json.",
    "org.jetbrains.", "org.chromium.", "com.google.android.", "com.google.common.",
    "com.google.firebase.", "com.google.gson.", "com.facebook.", "io.flutter.",
    "com.squareup.", "okhttp3.", "okio.", "retrofit2.", "io.reactivex.", "dagger.",
    "coil.", "io.ktor.", "com.bumptech.glide.",
)


def native_frames(lines: list[str]) -> list[CrashFrame]:
    """Frames from tombstone backtrace lines. A frame is the app's when its
    library was installed with the app (`/data/app/…`)."""
    return [parsed[0] for parsed in map(native_frame, lines) if parsed]


def native_frame(line: str) -> tuple[CrashFrame, str] | None:
    """One tombstone backtrace line as a frame, with its library's path; None
    if it is not one. Returned together so a line that is skipped cannot put
    the paths out of step with the frames."""
    m = _NATIVE.match(line)
    if not m:
        return None
    rest = m.group(2)
    build_id = _NATIVE_BUILD_ID.search(rest)
    if build_id:
        rest = rest[:build_id.start()]
    rest = _NATIVE_APK_OFFSET.sub("", rest).rstrip()
    path, symbol, symbol_offset = _native_path_and_symbol(rest)
    return CrashFrame(
        image=posixpath.basename(path.split("!")[-1]),
        offset=int(m.group(1), 16),
        symbol=symbol,
        symbol_offset=symbol_offset,
        build_id=build_id.group(1) if build_id else "",
        app=path.startswith("/data/app/"),
    ), path


def _native_path_and_symbol(rest: str) -> tuple[str, str, int | None]:
    """`/lib/libfoo.so (Foo::bar(int)+12)` -> path, symbol, 12.

    The symbol is the last bracketed group matched from the end, so C++
    symbols with their own brackets and paths with spaces
    (`/memfd:jit-cache (deleted)`) both come apart right.
    """
    if not rest.endswith(")"):
        return rest, "", None
    depth = 0
    for i in range(len(rest) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(rest[i], 0)
        if depth == 0:
            inner, path = rest[i + 1:-1], rest[:i].rstrip()
            break
    else:
        return rest, "", None
    if inner == "deleted" or not path:
        return rest, "", None
    name, sep, offset = inner.rpartition("+")
    if sep and offset.isascii() and offset.isdigit():
        return path, name.strip(), int(offset)
    return path, inner.strip(), None


def java_frames(lines: list[str], package: str = "") -> list[CrashFrame]:
    """Frames from `at …` lines, with which ones are the app's decided together.

    By the app's package where the record names it: the full applicationId
    first, then shorter prefixes of it -- a Debug build's `com.example.app.debug`
    runs code in `com.example.app`, and a module can live in `com.acme.feature`
    under `com.acme.app` -- never shorter than two segments, and never a
    platform or common-library package. If nothing matches even so, or there
    is no package: every frame outside those packages, a heuristic, since the
    report does not say what the app shipped.
    """
    frames = []
    for line in lines:
        m = _JAVA.match(line)
        if not m:
            continue
        symbol, where = m.group(1), m.group(2)
        source = _JAVA_SOURCE.match(where) if where not in ("Native Method", "") else None
        file = source.group(1) if source and source.group(1) != "Unknown Source" else ""
        frames.append(CrashFrame(
            symbol=symbol,
            file=file,
            line=_int(source.group(2)) if source and source.group(2) and file else None,
        ))
    for frame, app in zip(frames, _java_app_flags([f.symbol for f in frames], package),
                          strict=True):
        frame.app = app
    return frames


def _java_app_flags(symbols: list[str], package: str) -> list[bool]:
    parts = package.split(".") if package else []
    for n in range(len(parts), 1, -1):
        prefix = ".".join(parts[:n]) + "."
        flags = [s.startswith(prefix) and not s.startswith(_JAVA_NOT_APP) for s in symbols]
        if any(flags):
            return flags
    return [not s.startswith(_JAVA_NOT_APP) for s in symbols]
