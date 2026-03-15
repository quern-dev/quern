"""Manages scrcpy subprocesses for Android device preview."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("quern-debug-server.scrcpy-preview")


@dataclass
class ScrcpySession:
    serial: str
    name: str
    process: asyncio.subprocess.Process
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ScrcpyPreview:
    """Manages scrcpy subprocesses for Android device preview."""

    def __init__(self) -> None:
        self._processes: dict[str, ScrcpySession] = {}  # serial → session
        self._monitor_tasks: dict[str, asyncio.Task] = {}

    async def add(self, serial: str, name: str) -> ScrcpySession:
        """Start a scrcpy preview for an Android device.

        Args:
            serial: ADB serial (e.g. 'emulator-5554' or USB serial).
            name: Human-readable device name for the window title.

        Returns:
            ScrcpySession record.

        Raises:
            RuntimeError: If scrcpy is not installed or process fails to start.
        """
        if serial in self._processes:
            return self._processes[serial]

        if not self.is_available():
            raise RuntimeError(
                "scrcpy is not installed. Install with: brew install scrcpy"
            )

        window_title = f"Quern: {name}"
        proc = await asyncio.create_subprocess_exec(
            "scrcpy",
            "-s", serial,
            "--window-title", window_title,
            "--no-audio",
            "--stay-awake",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        session = ScrcpySession(
            serial=serial,
            name=name,
            process=proc,
        )
        self._processes[serial] = session

        # Monitor process in background — remove from _processes on exit
        self._monitor_tasks[serial] = asyncio.create_task(
            self._monitor(serial)
        )

        logger.info("Started scrcpy preview for %s (%s), pid %d", name, serial, proc.pid)
        return session

    async def _monitor(self, serial: str) -> None:
        """Wait for scrcpy process to exit and clean up."""
        session = self._processes.get(serial)
        if session is None:
            return

        await session.process.wait()
        # Only clean up if we haven't been explicitly removed
        if serial in self._processes:
            logger.info(
                "scrcpy exited for %s (%s), return code %d",
                session.name, serial, session.process.returncode,
            )
            del self._processes[serial]
        self._monitor_tasks.pop(serial, None)

    async def remove(self, serial: str) -> None:
        """Stop a scrcpy preview for a device."""
        session = self._processes.pop(serial, None)
        if session is None:
            return

        # Cancel monitor task
        task = self._monitor_tasks.pop(serial, None)
        if task and not task.done():
            task.cancel()

        if session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                session.process.kill()
                await session.process.wait()

        logger.info("Stopped scrcpy preview for %s (%s)", session.name, serial)

    async def stop(self) -> None:
        """Kill all scrcpy processes."""
        serials = list(self._processes.keys())
        for serial in serials:
            await self.remove(serial)

    def status(self) -> dict:
        """Return current scrcpy preview state."""
        active = {
            serial: {
                "name": s.name,
                "started_at": s.started_at.isoformat(),
                "pid": s.process.pid,
            }
            for serial, s in self._processes.items()
        }
        return {
            "available": self.is_available(),
            "active": active,
            "active_count": len(active),
        }

    def is_available(self) -> bool:
        """Check if scrcpy is on PATH."""
        return shutil.which("scrcpy") is not None
