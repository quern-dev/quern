"""Fold and unfold a foldable simulator -- iPhone Duo -- and read its hinge (#537).

Nothing on the host can set the hinge: `simctl` has no verb for it and Device
Hub's window is opaque to accessibility. tools/hinge-control is a small helper
that runs *inside* the simulator (`simctl spawn`), registers a virtual HID
service shaped like Device Hub's, and dispatches the same pose events; it reads
the hinge through CoreMotion. It is adapted from tddworks/baguette; see its
README for what it does and why.

Each command is its own spawn, about half a second: a pose is rare enough that
a helper kept running per device would buy little for the lifecycle it brings.
A command that outlives its deadline is killed in the guest too -- the host's
`simctl spawn` child does not own the guest process, which would otherwise go
on and move the hinge after quern had given up. A simulator's processes are
the Mac's own, so it is killed from here, by the pid the helper prints first;
the runtime has no `kill` to spawn.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import platform
import re
import signal
import time
from pathlib import Path

from server.config import CONFIG_DIR
from server.models import DeviceError

logger = logging.getLogger(__name__)

_TOOL = "hinge-control"
BINARY_NAME = "hinge-control"
HINGE_DIR = Path(__file__).resolve().parents[3] / "tools" / "hinge-control"
SOURCES = ("Sources/HingeControl.m", "Sources/HingeAngle.m")
#: Read with the sources for the digest, so a change to any header rebuilds.
HEADERS = ("Sources/HingeAngle.h", "Sources/HingePayload.h", "Sources/HingeProtocol.h")

#: Device Hub's poses, in degrees, and its sweep: 0.8s with an ease-out.
POSES = {"closed": 0.0, "open": 130.0, "flat": 180.0}
DEFAULT_SWEEP_MS = 800

#: How long the helper may take beyond the command's own playing time: its
#: spawn (~0.5s) and the HID service settling (0.3s), with room for a busy Mac.
_SLACK_S = 8.0
_READ_TIMEOUT_S = 10.0
_COMPILE_TIMEOUT_S = 120.0

_ANGLE_RE = re.compile(r"^angle (-?[0-9.]+(?:e-?[0-9]+)?)$", re.MULTILINE)
_PID_RE = re.compile(r"^pid (\d+)$", re.MULTILINE)


def _bin_dir() -> Path:
    return CONFIG_DIR / "bin"


def sources_digest(root: Path = HINGE_DIR) -> str:
    """What the helper is built from: rebuilt when any of it changes."""
    h = hashlib.sha256()
    for rel in (*SOURCES, *HEADERS):
        h.update(rel.encode())
        h.update((root / rel).read_bytes())
    return h.hexdigest()


class HingeControl:
    """The helper, compiled once and spawned per command."""

    def __init__(self, root: Path = HINGE_DIR) -> None:
        self._root = root
        self._lock = asyncio.Lock()

    @property
    def binary_path(self) -> Path:
        return _bin_dir() / BINARY_NAME

    @property
    def _stamp_path(self) -> Path:
        return _bin_dir() / f"{BINARY_NAME}.sha256"

    async def ensure_binary(self) -> Path:
        """Compile the helper if what it is built from has changed.

        Signed at link time (`-Wl,-adhoc_codesign`) and never again: the iOS
        26+ simulator's dyld rejects a binary re-signed after the build.
        """
        async with self._lock:
            try:
                digest = sources_digest(self._root)
            except OSError as e:
                raise DeviceError(f"hinge-control sources not found under {self._root}: {e}",
                                  tool=_TOOL) from e
            with contextlib.suppress(OSError):
                if (self.binary_path.exists()
                        and self._stamp_path.read_text().strip() == digest):
                    return self.binary_path
            # Cleared before building, not after: a failed build must not
            # leave an older stamp naming a binary it does not describe.
            with contextlib.suppress(OSError):
                self._stamp_path.unlink()
            _bin_dir().mkdir(parents=True, exist_ok=True)
            sdk = await self._run_text(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-path"])
            arch = platform.machine() or "arm64"
            # Built beside it and renamed into place: a second server sharing
            # CONFIG_DIR/bin may be spawning the old binary while this builds.
            tmp = self.binary_path.with_name(f"{BINARY_NAME}.{os.getpid()}.tmp")
            cmd = ["xcrun", "clang", "-arch", arch, "-isysroot", sdk,
                   "-target", f"{arch}-apple-ios17.0-simulator",
                   "-framework", "Foundation", "-fobjc-arc", "-Wl,-adhoc_codesign",
                   "-o", str(tmp),
                   *(str(self._root / s) for s in SOURCES)]
            logger.info("Compiling hinge-control for %s", arch)
            try:
                await self._run_text(cmd, timeout=_COMPILE_TIMEOUT_S)
                os.replace(tmp, self.binary_path)
            except OSError as e:
                raise DeviceError(f"could not install hinge-control: {e}", tool=_TOOL) from e
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
            try:
                self._stamp_path.write_text(digest)
            except OSError:
                # A missing stamp costs a rebuild next time, which is safe.
                logger.debug("Could not write the hinge-control stamp", exc_info=True)
            return self.binary_path

    async def read_angle(self, udid: str) -> float | None:
        """The hinge's angle in degrees; None when the guest gives no reading.

        The guest goes silent after a SpringBoard restart until the hinge next
        moves (baguette's finding), so None is an answer, not a failure -- but
        only when the helper says that is what happened. Any other exit 1,
        `simctl spawn` failing or dyld refusing the binary, raises.
        """
        binary = await self.ensure_binary()
        code, out, err = await self._spawn(udid, [str(binary), "read-angle"], _READ_TIMEOUT_S)
        if code == 0:
            m = _ANGLE_RE.search(out)
            if m:
                return float(m.group(1))
        if code == 1 and "no hinge sample" in err:
            return None
        raise DeviceError(f"could not read the hinge of {udid[:8]}: exit {code}: "
                          f"{(err or out).strip()[:200]}", tool=_TOOL)

    async def move(self, udid: str, from_deg: float, to_deg: float, duration_ms: int) -> None:
        """Move the hinge: swept over `duration_ms` from `from_deg`, or set at
        once when the duration is 0. Raises when it did not happen."""
        binary = await self.ensure_binary()
        deadline = int(time.time() + _SLACK_S)
        if duration_ms <= 0:
            verb = ["angle", f"{to_deg:g}"]
        else:
            verb = ["sweep", f"{from_deg:g}", f"{to_deg:g}", str(int(duration_ms))]
        await self._act(udid, [str(binary), "--deadline", str(deadline), *verb],
                        duration_ms / 1000 + _SLACK_S)

    async def orient(self, udid: str, value: str) -> None:
        """Device Hub's orientation picker: portrait, pud, landscape-left or
        landscape-right -- physical orientations, as the picker names them."""
        binary = await self.ensure_binary()
        deadline = int(time.time() + _SLACK_S)
        await self._act(udid, [str(binary), "--deadline", str(deadline), "orientation", value],
                        _SLACK_S)

    async def _act(self, udid: str, argv: list[str], timeout: float) -> None:
        code, out, err = await self._spawn(udid, argv, timeout)
        if code == 0:
            return
        if code == 3:
            raise DeviceError("hinge-control did not start before its deadline; nothing was "
                              "done. The simulator may be busy: try again.", tool=_TOOL)
        reason = {1: "the simulator refused the pose event",
                  2: "the helper rejected the command"}.get(code, f"exit {code}")
        raise DeviceError(f"could not move the hinge of {udid[:8]}: {reason}: "
                          f"{(err or out).strip()[:200]}", tool=_TOOL)

    async def _spawn(self, udid: str, argv: list[str], timeout: float) -> tuple[int, str, str]:
        """Run the helper in the simulator. On a timeout or cancellation the
        guest process is killed as well as the host's `simctl spawn`, which
        does not own it."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "spawn", udid, *argv,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL)
        except OSError as e:
            raise DeviceError(f"could not run simctl: {e}", tool=_TOOL) from e
        # Read into lists that outlive a cancelled wait: `communicate()` keeps
        # what it read to itself, so the `pid` line was lost with it.
        out_parts: list[bytes] = []
        err_parts: list[bytes] = []

        async def pump(stream: asyncio.StreamReader | None, into: list[bytes]) -> None:
            while stream is not None and (chunk := await stream.read(4096)):
                into.append(chunk)

        try:
            await asyncio.wait_for(asyncio.gather(
                pump(proc.stdout, out_parts), pump(proc.stderr, err_parts), proc.wait()), timeout)
        except BaseException as e:
            # Timed out or cancelled: stop it here and in the guest, where it
            # would otherwise go on and move the hinge after quern gave up.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2.0)
            stopped = await self._kill_guest(b"".join(out_parts).decode(errors="replace"))
            if isinstance(e, TimeoutError):
                outcome = ("and was stopped" if stopped else
                           "and could not be stopped in the simulator, so it may still act")
                raise DeviceError(f"hinge-control on {udid[:8]} did not finish within "
                                  f"{timeout:.0f}s {outcome}; read the hinge before trying "
                                  "again, since it may have moved", tool=_TOOL) from e
            raise
        return (proc.returncode or 0, b"".join(out_parts).decode(errors="replace"),
                b"".join(err_parts).decode(errors="replace"))

    async def _kill_guest(self, out: str) -> bool:
        """Kill the helper whose `pid` line is in `out`. True when it is gone:
        killed, already exited, or never printed a pid and so never acted."""
        m = _PID_RE.search(out)
        if not m:
            return True
        pid = int(m.group(1))
        # Only if that pid is still the helper: by now it may have exited and
        # the number been reused by something of the user's.
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "comm=",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out_b, _ = await asyncio.wait_for(proc.communicate(), 5.0)
        except (OSError, TimeoutError):
            logger.warning("Could not look up hinge-control pid %d", pid, exc_info=True)
            return False
        if not out_b.decode(errors="replace").strip().endswith(BINARY_NAME):
            return True
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            logger.warning("Could not kill hinge-control pid %d", pid, exc_info=True)
            return False
        return True

    async def _run_text(self, cmd: list[str], timeout: float = 30.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise DeviceError(f"could not build hinge-control: {e}", tool=_TOOL) from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException as e:
            # Not left running to write a binary the next build also writes.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2.0)
            if isinstance(e, TimeoutError):
                raise DeviceError(f"could not build hinge-control: {' '.join(cmd[:2])} "
                                  f"did not finish within {timeout:.0f}s", tool=_TOOL) from e
            raise
        if proc.returncode != 0:
            raise DeviceError(f"could not build hinge-control: "
                              f"{(err or out).decode(errors='replace').strip()[-400:]}", tool=_TOOL)
        return out.decode(errors="replace").strip()
