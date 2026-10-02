"""Video for a recording: one `quern-media --record` per quern run (#364, phase 3).

A segment is a simulator's screen written to `<dir>/video-<n>.mp4` for as
long as one quern process records it. Frames are stamped on the host clock
(`CMClockGetHostTimeClock()`, mach absolute time), the same clock as every
`monotonic` in the recording, so joining needs no conversion: a moment's
offset into the movie is its monotonic time minus the segment's
`startHostTime`, which quern-media reports when it finishes (#290). The
movie's own timeline is not zero-based -- it runs in host seconds -- so the
offset is the thing to seek by, never a timestamp read from the file.

`--serve` on loopback as well as `--record`, for one reason: `POST /keyframe`
(#289). A keyframe at each action's start makes every action a seek point;
without one, an idle screen composites nothing and the next keyframe is
arbitrarily far away.

Nothing here can fail a recording. A segment that will not start, or will
not finish cleanly, is said in the recording and the run carries on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: How long quern-media may take to start serving before the segment is
#: given up on: capture setup on a booted simulator, not a build.
START_TIMEOUT = 15.0  # s
#: How long it may take to finalise the movie after SIGINT. A long run's
#: moov atom is written at the end, so this is generous.
STOP_TIMEOUT = 30.0  # s
#: A keyframe request is fire-and-forget: one slow answer must not hold the
#: action that asked.
KEYFRAME_TIMEOUT = 1.0  # s

#: `[record] 1234 frames over 5.40s from host 612668.550500, 2 dropped -> /p.mp4`
_SUMMARY = re.compile(r"\[record\] (\d+) frames over ([\d.]+)s from host ([\d.]+), "
                      r"(\d+) dropped -> (.+)$")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Segment:
    """One movie, and what quern-media said about it."""

    path: Path
    udid: str
    process: asyncio.subprocess.Process | None = None
    port: int = 0
    log: deque = field(default_factory=lambda: deque(maxlen=40))
    drain: asyncio.Task | None = None
    keyframes_requested: int = 0
    keyframes_failed: int = 0

    def summary_line(self) -> str | None:
        return next((line for line in reversed(self.log) if _SUMMARY.search(line)), None)

    def tail(self) -> str:
        return " / ".join(list(self.log)[-5:]) or "no output"


class VideoError(RuntimeError):
    """A segment that could not start, with what quern-media said."""


class VideoRecorder:
    """Starts and stops quern-media for recordings. `binary` is a callable
    returning the binary's path, building it if need be; tests pass their
    own, and their own `spawn`."""

    def __init__(self, binary, spawn=asyncio.create_subprocess_exec) -> None:
        self._binary = binary
        self._spawn = spawn

    async def start(self, udid: str, path: Path) -> Segment:
        binary = await self._binary()
        seg = Segment(path=path, udid=udid, port=_free_port())
        seg.process = await self._spawn(
            str(binary), "--sim-udid", udid, "--record", str(path), "--serve", str(seg.port),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        seg.drain = asyncio.create_task(self._drain(seg), name=f"video-drain[{udid[:8]}]")
        try:
            await self._wait_serving(seg)
        except BaseException:
            await self.stop(seg)
            raise
        return seg

    async def _drain(self, seg: Segment) -> None:
        """Keep stderr empty -- a long run would otherwise fill the pipe and
        block quern-media -- and keep its last lines, which hold the summary."""
        stream = seg.process.stderr
        with contextlib.suppress(asyncio.CancelledError):
            while line := await stream.readline():
                text = line.decode(errors="replace").rstrip()
                if text:
                    seg.log.append(text)
                    logger.debug("quern-media[%s]: %s", seg.udid[:8], text)

    async def _wait_serving(self, seg: Segment) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if seg.process.returncode is not None:
                await asyncio.sleep(0.05)       # let the drain take the last lines
                raise VideoError(f"quern-media exited ({seg.process.returncode}) before it "
                                 f"started recording {seg.udid}: {seg.tail()}")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", seg.port)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        raise VideoError(f"quern-media did not start recording {seg.udid} within "
                         f"{START_TIMEOUT:g}s: {seg.tail()}")

    async def keyframe(self, seg: Segment) -> bool:
        """Ask for a keyframe now. Never raises; False if it was not asked."""
        if seg.process is None or seg.process.returncode is not None:
            return False
        seg.keyframes_requested += 1
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", seg.port), KEYFRAME_TIMEOUT)
            writer.write(b"POST /keyframe HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), KEYFRAME_TIMEOUT)
            writer.close()
            ok = b" 204 " in status or b" 200 " in status
        except (OSError, TimeoutError):
            ok = False
        if not ok:
            seg.keyframes_failed += 1
        return ok

    async def stop(self, seg: Segment) -> dict:
        """Finish the movie and say what it holds.

        SIGINT, which quern-media handles by writing the moov atom: a movie
        killed without it is unopenable rather than shorter. The summary it
        prints is the only source of `start_host_time`, so a segment that
        ends without one says why instead of claiming a join it cannot make.
        """
        proc = seg.process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                seg.log.append(f"did not finish within {STOP_TIMEOUT:g}s and was killed")
        if seg.drain is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(seg.drain, 2)
        result: dict = {"path": str(seg.path), "keyframes_requested": seg.keyframes_requested,
                        "keyframes_failed": seg.keyframes_failed,
                        "exit_status": proc.returncode if proc else None}
        line = seg.summary_line()
        if line and (m := _SUMMARY.search(line)):
            result.update(frames=int(m.group(1)), duration_s=float(m.group(2)),
                          start_host_time=float(m.group(3)), frames_dropped=int(m.group(4)))
        else:
            result.update(start_host_time=None,
                          error=f"quern-media gave no recording summary: {seg.tail()}")
        return result
