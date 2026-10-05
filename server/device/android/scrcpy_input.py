"""Multi-finger touch on Android, through scrcpy's server (#252).

Android gives the shell user no way to inject a touch with more than one
pointer: `input` has none, uiautomator2's `injectInputEvent` takes one, its
two-pointer `gesture` moves each finger in a straight line, and writing the
touchscreen's `/dev/input` node is denied by SELinux on every device tried
(the emulator and a Pixel 5 on Android 14). What does work is a process
started through `app_process` as the shell user, injecting MotionEvents
through `InputManager` -- which is what scrcpy's server is. So gestures run it
with video and audio off, control only, one per device, kept for the next
gesture.

scrcpy is a dependency of Android gestures and nothing else: without it they
are refused with the install command. Its control protocol is tied to the
scrcpy version, so the server is started with the version of the scrcpy that
installed it, and an upgrade moves both together.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from server.device.gestures import TAP_HOLD_MS, Plan
from server.models import DeviceError, DeviceOperationUnsupportedError

logger = logging.getLogger(__name__)

_TOOL = "scrcpy"
REMOTE_JAR = "/data/local/tmp/quern-scrcpy-server.jar"

# Control messages (scrcpy's control_msg.h). One touch event is 32 bytes: type,
# action, pointer id, position, the screen size the position is relative to,
# pressure, action button, buttons.
_INJECT_TOUCH = 2
_DOWN, _UP, _MOVE = 0, 1, 2
_TOUCH = struct.Struct(">BBqiiHHHII")
#: scrcpy reserves -1 to -3 (mouse, generic and virtual fingers).
_POINTER_BASE = 1000

#: How long to wait for the server's socket after starting it.
CONNECT_TIMEOUT = 8.0


def _install_hint() -> str:
    return "Android gestures need scrcpy's server: brew install scrcpy"


@dataclass(frozen=True)
class ScrcpyServer:
    jar: Path
    version: str


def find_server() -> ScrcpyServer | None:
    """The scrcpy-server jar and the version it must be started with.

    `SCRCPY_SERVER_PATH` first, as scrcpy itself reads it; then the jar
    installed beside the `scrcpy` binary. None when either half is missing --
    a jar of unknown version cannot be started, since the server refuses a
    version that is not its own.
    """
    binary = shutil.which("scrcpy")
    if binary is None:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    words = out.split()
    if len(words) < 2 or words[0] != "scrcpy":
        return None
    version = words[1]
    candidates = []
    if env := os.environ.get("SCRCPY_SERVER_PATH"):
        candidates.append(Path(env))
    real = Path(os.path.realpath(binary))
    candidates.append(real.parents[1] / "share" / "scrcpy" / "scrcpy-server")
    candidates.append(Path(binary).resolve().parent.parent / "share" / "scrcpy" / "scrcpy-server")
    for jar in candidates:
        if jar.is_file():
            return ScrcpyServer(jar, version)
    return None


def touch_message(action: int, finger: int, x: float, y: float,
                  width: int, height: int, pressure: float) -> bytes:
    """One INJECT_TOUCH_EVENT. Positions are pixels on a `width` x `height`
    screen, which must be the device's current size or the server drops it."""
    p = 0xFFFF if pressure >= 1 else max(0, int(pressure * 0x10000))
    return _TOUCH.pack(_INJECT_TOUCH, action, _POINTER_BASE + finger,
                       round(x), round(y), width, height, p, 0, 0)


def gesture_messages(plan: Plan, width: int, height: int) -> list[tuple[bytes, float]]:
    """The plan as (message, seconds to wait after it) pairs.

    Each finger is its own pointer: down in turn, moved in turn at each
    waypoint, up in turn -- what scrcpy's client sends, and the server works
    out POINTER_DOWN and POINTER_UP from the fingers it has down.
    """
    out: list[tuple[bytes, float]] = []

    def msg(action, finger, p, pressure):
        return touch_message(action, finger, p[0], p[1], width, height, pressure)

    if plan.paths is not None:
        step = plan.duration / (len(plan.paths[0]) - 1)
        for f, path in enumerate(plan.paths):
            out.append((msg(_DOWN, f, path[0], 1.0), 0.0))
        for i in range(1, len(plan.paths[0])):
            out[-1] = (out[-1][0], step)
            for f, path in enumerate(plan.paths):
                out.append((msg(_MOVE, f, path[i], 1.0), 0.0))
        out[-1] = (out[-1][0], 0.016)
        for f, path in enumerate(plan.paths):
            out.append((msg(_UP, f, path[-1], 0.0), 0.0))
        return out
    points = plan.points or []
    for n in range(plan.count):
        for f, p in enumerate(points):
            out.append((msg(_DOWN, f, p, 1.0), 0.0))
        out[-1] = (out[-1][0], TAP_HOLD_MS / 1000)
        for f, p in enumerate(points):
            out.append((msg(_UP, f, p, 0.0), 0.0))
        if n < plan.count - 1:
            out[-1] = (out[-1][0], plan.interval)
    return out


class _Session:
    """One control-only scrcpy server on one device."""

    def __init__(self, adb: str, serial: str, server: ScrcpyServer) -> None:
        self.adb, self.serial, self.server = adb, serial, server
        self.scid = random.randint(1, 0x7FFFFFFF)
        self.process: asyncio.subprocess.Process | None = None
        self.port: int | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def _adb(self, *args: str, timeout: float = 30.0) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.adb, "-s", self.serial, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise DeviceError(f"adb {args[0]} did not answer within {timeout:g}s",
                              tool=_TOOL) from None
        if proc.returncode != 0:
            raise DeviceError(f"adb {args[0]} failed: {err.decode().strip()}", tool=_TOOL)
        return out.decode()

    @property
    def alive(self) -> bool:
        return (self.process is not None and self.process.returncode is None
                and self.writer is not None and not self.writer.is_closing())

    async def start(self) -> None:
        await self._adb("push", str(self.server.jar), REMOTE_JAR, timeout=60)
        name = f"scrcpy_{self.scid:08x}"
        self.process = await asyncio.create_subprocess_exec(
            self.adb, "-s", self.serial, "shell", f"CLASSPATH={REMOTE_JAR}", "app_process",
            "/", "com.genymobile.scrcpy.Server", self.server.version,
            f"scid={self.scid:08x}", "log_level=warn", "tunnel_forward=true",
            "video=false", "audio=false", "control=true", "cleanup=true",
            "send_device_meta=true", "send_dummy_byte=true",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            self.port = int((await self._adb("forward", "tcp:0", f"localabstract:{name}")).strip())
            await self._connect()
        except BaseException:
            await self.close()
            raise

    async def _connect(self) -> None:
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self.process is not None and self.process.returncode is not None:
                said = (await self.process.stdout.read()).decode(errors="replace").strip() \
                    if self.process.stdout else ""
                raise DeviceError(f"scrcpy's server exited at start: {said[-300:]}", tool=_TOOL)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                # The forward accepts before the server is listening, and a
                # connection made too early reads nothing: the dummy byte is
                # how the server says it is there.
                dummy = await asyncio.wait_for(reader.read(1), 2.0)
                if dummy:
                    await asyncio.wait_for(reader.readexactly(64), 2.0)  # device name
                    self.reader, self.writer = reader, writer
                    return
                writer.close()
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as e:
                last = e
            await asyncio.sleep(0.2)
        raise DeviceError(f"scrcpy's server on {self.serial} did not answer within "
                          f"{CONNECT_TIMEOUT:g}s ({last!r})", tool=_TOOL)

    async def send(self, messages: list[tuple[bytes, float]]) -> None:
        assert self.writer is not None
        for message, wait in messages:
            self.writer.write(message)
            await self.writer.drain()
            if wait:
                await asyncio.sleep(wait)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.process.wait(), 2.0)
        if self.port is not None:
            with contextlib.suppress(Exception):
                await self._adb("forward", "--remove", f"tcp:{self.port}", timeout=5)
        self.writer = self.reader = None


class ScrcpyInput:
    """One session per device, started on the first gesture and kept."""

    def __init__(self, adb_path: str | None) -> None:
        self._adb = adb_path
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def perform(self, serial: str, plan: Plan, width: int, height: int) -> None:
        if not self._adb:
            raise DeviceError("adb not found", tool=_TOOL)
        server = await asyncio.to_thread(find_server)
        if server is None:
            raise DeviceOperationUnsupportedError(_install_hint(), tool=_TOOL)
        messages = gesture_messages(plan, width, height)
        async with self._locks.setdefault(serial, asyncio.Lock()):
            session = self._sessions.get(serial)
            if session is None or not session.alive or session.server != server:
                if session is not None:
                    await session.close()
                session = _Session(self._adb, serial, server)
                self._sessions[serial] = session
                try:
                    await session.start()
                except BaseException:
                    self._sessions.pop(serial, None)
                    raise
            try:
                await session.send(messages)
            except (OSError, ConnectionError) as e:
                # Not re-sent: part of it may have landed, and a second
                # rotation turns twice as far. The next gesture starts a new
                # server.
                self._sessions.pop(serial, None)
                await session.close()
                raise DeviceError(
                    f"the {plan.kind} on {serial} was interrupted ({e!r}) and not sent "
                    f"again; check the screen", tool=_TOOL) from e

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
