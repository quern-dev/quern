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

A segment that will not start refuses the recording that asked for it,
and a resumed recording carries on without one. One that will not finish
cleanly is said in the recording and makes it incomplete.

quern-media's stderr goes to `video-<n>.log` beside the movie, never a pipe.
It ignores SIGINT and SIGTERM to finalise the movie on them, but not
SIGPIPE: with a pipe, a quern that died took its reader with it, and the
first line quern-media wrote after that -- the one it writes on the way to
finalising -- killed it with the movie unopenable. With a file it outlives
quern, and the next quern finds it running, stops it properly, and reads its
summary from the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: How long quern-media may take to start capturing before the segment is
#: given up on: capture setup on a booted simulator, not a build.
START_TIMEOUT = 15.0  # s
#: How long it may take to finalise the movie after SIGINT: past its own
#: 60s `finish` budget, so quern never kills a finalise quern-media would
#: have completed -- killed, the movie is unopenable (review).
STOP_TIMEOUT = 65.0  # s
#: A keyframe request is fire-and-forget: one slow answer must not hold the
#: action that asked.
KEYFRAME_TIMEOUT = 1.0  # s

#: `[record] 1234 frames over 5.40s from host 612668.550500, 2 dropped -> /p.mp4`
_SUMMARY = re.compile(r"\[record\] (\d+) frames over ([\d.]+)s from host ([\d.]+), "
                      r"(\d+) dropped -> (.+)$")
#: Written once the simulator's framebuffer is being captured. Not the port:
#: quern-media binds that first, so a simulator that is not booted had an
#: open port and then an exit, and the recording started without video
#: (review).
_STREAMING = "[capture] streaming simulator"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def log_path(movie: Path) -> Path:
    """Where quern-media's output for `movie` goes."""
    return movie.with_suffix(".log")


def _lines(log: Path) -> list[str]:
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16_384))      # the summary is at the end
            return [x for x in f.read().decode(errors="replace").splitlines() if x.strip()]
    except OSError:
        return []


def _tail(lines: list[str]) -> str:
    """What to quote of quern-media's output: its `error:` lines when it
    wrote any -- a failure prints its usage after the error, and the last
    lines alone quoted the usage and not why (live) -- else its last lines."""
    errors = [x.strip() for x in lines if x.lstrip().startswith("error:")]
    return " / ".join(errors[-3:] or lines[-5:]) or "no output"


def summary(movie: Path) -> dict:
    """What quern-media said about `movie` when it finished, from its log:
    frames, duration_s, start_host_time and frames_dropped -- or
    start_host_time None and an `error` saying why there is none."""
    lines = _lines(log_path(movie))
    for line in reversed(lines):
        if m := _SUMMARY.search(line):
            return {"frames": int(m.group(1)), "duration_s": float(m.group(2)),
                    "start_host_time": float(m.group(3)), "frames_dropped": int(m.group(4))}
    return {"start_host_time": None,
            "error": f"quern-media gave no recording summary: {_tail(lines)}"}


@dataclass
class Segment:
    """One movie being recorded."""

    path: Path
    udid: str
    process: asyncio.subprocess.Process | None = None
    port: int = 0
    keyframes_requested: int = 0
    keyframes_failed: int = 0

    @property
    def pid(self) -> int | None:
        return getattr(self.process, "pid", None)


class VideoError(RuntimeError):
    """A segment that could not start, with what quern-media said."""


class VideoRecorder:
    """Starts and stops quern-media for recordings. `binary` is a callable
    returning the binary's path, building it if need be; tests pass their
    own, and their own `spawn`."""

    def __init__(self, binary, spawn=asyncio.create_subprocess_exec,
                 command_of=None) -> None:
        self._binary = binary
        self._spawn = spawn
        #: pid -> its command line, "" when there is no such process; raises
        #: OSError when it cannot tell.
        self._command_of = command_of or _command_of

    async def start(self, udid: str, path: Path) -> Segment:
        binary = await self._binary()
        seg = Segment(path=path, udid=udid, port=_free_port())
        log = log_path(path)
        with open(log, "wb") as out:            # truncated: a summary must be this run's
            seg.process = await self._spawn(
                str(binary), "--sim-udid", udid, "--record", str(path), "--serve",
                str(seg.port), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=out)
        try:
            await self._wait_streaming(seg)
        except BaseException:
            await self.stop(seg)
            raise
        return seg

    async def _wait_streaming(self, seg: Segment) -> None:
        log = log_path(seg.path)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            lines = await asyncio.to_thread(_lines, log)
            if any(_STREAMING in line for line in lines):
                return
            if seg.process.returncode is not None:
                raise VideoError(f"quern-media exited ({seg.process.returncode}) before it "
                                 f"started recording {seg.udid}: {_tail(lines)}")
            await asyncio.sleep(0.1)
        raise VideoError(f"quern-media did not start recording {seg.udid} within "
                         f"{START_TIMEOUT:g}s: {_tail(await asyncio.to_thread(_lines, log))}")

    async def keyframe(self, seg: Segment) -> bool:
        """Ask for a keyframe now. Never raises; False if it was not asked."""
        if seg.process is None or seg.process.returncode is not None:
            return False
        seg.keyframes_requested += 1
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", seg.port), KEYFRAME_TIMEOUT)
            writer.write(b"POST /keyframe HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), KEYFRAME_TIMEOUT)
            ok = b" 204 " in status or b" 200 " in status
        except (OSError, TimeoutError, ValueError):
            ok = False
        finally:
            if writer is not None:
                writer.close()
        if not ok:
            seg.keyframes_failed += 1
        return ok

    async def stop(self, seg: Segment) -> dict:
        """Finish the movie and say what it holds.

        SIGINT, which quern-media handles by writing the moov atom: a movie
        killed without it is unopenable rather than shorter. The summary it
        prints is the only source of `start_host_time`, so a segment that
        ends without one says why instead of claiming a join it cannot make.

        One that had already exited is said too: its movie ends at its last
        frame, not at this stop, so nothing after that is in it.
        """
        proc = seg.process
        exited_early = proc is not None and proc.returncode is not None
        killed = False
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                killed = True
        result: dict = {"path": str(seg.path), "keyframes_requested": seg.keyframes_requested,
                        "keyframes_failed": seg.keyframes_failed,
                        "exit_status": proc.returncode if proc else None,
                        **await asyncio.to_thread(summary, seg.path)}
        if killed:
            result["error"] = (f"quern-media did not finish within {STOP_TIMEOUT:g}s and was "
                               f"killed: the movie was not finalised and may not open")
            result["start_host_time"] = None
        elif exited_early:
            result["exited_before_stop"] = True
            result.setdefault("error", f"quern-media exited ({proc.returncode}) before the "
                                       f"recording stopped: the movie ends at its last frame")
        return result

    async def reap(self, pid: int, movie: Path) -> dict | None:
        """Finish a quern-media an earlier quern left recording `movie`, and
        return its summary; None if it is not running (or `pid` is now some
        other process -- matched on the movie's path, never the pid alone).
        Raises OSError if that cannot be told."""
        command = await asyncio.to_thread(self._command_of, pid)
        if not command or "quern-media" not in command or str(movie) not in command:
            return None
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGINT)
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):        # asked again, not read as gone
                if not await asyncio.to_thread(self._command_of, pid):
                    # Stopped now, so the movie runs to now: the stop line
                    # this becomes is its end, as for any other stop.
                    return {"path": str(movie), "reaped": True,
                            **await asyncio.to_thread(summary, movie)}
            await asyncio.sleep(0.2)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
        return {"path": str(movie), "start_host_time": None,
                "error": f"quern-media left running by an earlier quern did not finish within "
                         f"{STOP_TIMEOUT:g}s and was killed: the movie may not open"}


def _command_of(pid: int) -> str:
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                           text=True, timeout=5)
    except subprocess.SubprocessError as e:
        raise OSError(f"ps could not be asked about {pid}: {e}") from e
    if r.returncode not in (0, 1) or (r.returncode == 1 and r.stderr.strip()):
        raise OSError(f"ps could not be asked about {pid}: {r.stderr.strip()}")
    return r.stdout.strip()
