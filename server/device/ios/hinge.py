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
on and move the hinge after quern had given up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import platform
import re
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
            cmd = ["xcrun", "clang", "-arch", arch, "-isysroot", sdk,
                   "-target", f"{arch}-apple-ios17.0-simulator",
                   "-framework", "Foundation", "-fobjc-arc", "-Wl,-adhoc_codesign",
                   "-o", str(self.binary_path),
                   *(str(self._root / s) for s in SOURCES)]
            logger.info("Compiling hinge-control for %s", arch)
            await self._run_text(cmd, timeout=_COMPILE_TIMEOUT_S)
            try:
                self._stamp_path.write_text(digest)
            except OSError:
                # A missing stamp costs a rebuild next time, which is safe.
                logger.debug("Could not write the hinge-control stamp", exc_info=True)
            return self.binary_path

    async def read_angle(self, udid: str) -> float | None:
        """The hinge's angle in degrees; None when the guest gives no reading.

        The guest goes silent after a SpringBoard restart until the hinge next
        moves (baguette's finding), so None is an answer, not a failure. A
        helper that could not be run at all raises.
        """
        binary = await self.ensure_binary()
        code, out, err = await self._spawn(udid, [str(binary), "read-angle"], _READ_TIMEOUT_S)
        if code == 0:
            m = _ANGLE_RE.search(out)
            if m:
                return float(m.group(1))
        if code == 1:
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
        """Run the helper in the simulator. On a timeout the guest process is
        killed as well as the host's `simctl spawn`, which does not own it."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "spawn", udid, *argv,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL)
        except OSError as e:
            raise DeviceError(f"could not run simctl: {e}", tool=_TOOL) from e
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException as e:
            # Timed out or cancelled: stop it here and in the guest, where it
            # would otherwise go on and move the hinge after quern gave up.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            out_b = b""
            with contextlib.suppress(Exception):
                if proc.stdout is not None:
                    out_b = await asyncio.wait_for(proc.stdout.read(), 1.0)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2.0)
            await self._kill_guest(udid, out_b.decode(errors="replace"))
            if isinstance(e, TimeoutError):
                raise DeviceError(f"hinge-control on {udid[:8]} did not finish within "
                                  f"{timeout:.0f}s and was stopped; read the hinge before "
                                  "trying again, since it may have moved", tool=_TOOL) from e
            raise
        return proc.returncode or 0, out_b.decode(errors="replace"), err_b.decode(errors="replace")

    async def _kill_guest(self, udid: str, out: str) -> None:
        m = _PID_RE.search(out)
        if not m:
            return
        with contextlib.suppress(Exception):
            p = await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "spawn", udid, "kill", "-9", m.group(1),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), 5.0)

    async def _run_text(self, cmd: list[str], timeout: float = 30.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except (OSError, TimeoutError) as e:
            raise DeviceError(f"could not build hinge-control: {e}", tool=_TOOL) from e
        if proc.returncode != 0:
            raise DeviceError(f"could not build hinge-control: "
                              f"{(err or out).decode(errors='replace').strip()[-400:]}", tool=_TOOL)
        return out.decode(errors="replace").strip()
