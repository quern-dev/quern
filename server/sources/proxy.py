"""Source adapter for mitmproxy network traffic capture.

Spawns `mitmdump` with our addon script as a subprocess and reads
JSON Lines from its stdout. Each flow is dual-emitted:
  1. Full FlowRecord → FlowStore
  2. Summary LogEntry → processing pipeline (dedup → ring buffer)

Follows the same subprocess pattern as SyslogAdapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.models import (
    FlowRecord,
    FlowRequest,
    FlowResponse,
    FlowTiming,
    LogEntry,
    LogLevel,
    LogSource,
)
from server.proxy.flow_store import FlowStore
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

# Path to the addon script (lives alongside this module's parent)
ADDON_PATH = Path(__file__).resolve().parent.parent / "proxy" / "addon.py"


def _classify_level(flow: FlowRecord) -> LogLevel:
    """Classify a flow's log level based on status code and errors."""
    if flow.error:
        return LogLevel.ERROR

    if flow.response is None:
        return LogLevel.WARNING

    code = flow.response.status_code
    if code >= 500:
        return LogLevel.ERROR
    if code >= 400:
        return LogLevel.WARNING
    return LogLevel.INFO


def _format_summary(flow: FlowRecord) -> str:
    """Format a one-line summary of a flow for the log buffer."""
    req = flow.request
    parts = [f"{req.method} {req.path}"]

    if flow.response:
        resp = flow.response
        parts.append(f"→ {resp.status_code} {resp.reason}".rstrip())
        if flow.timing.total_ms is not None:
            parts.append(f"({flow.timing.total_ms:.0f}ms")
            if resp.body_size:
                parts.append(f", {_human_size(resp.body_size)})")
            else:
                parts.append(")")
        elif resp.body_size:
            parts.append(f"({_human_size(resp.body_size)})")
    elif flow.error:
        parts.append(f"→ ERROR: {flow.error}")

    return " ".join(parts)


def _human_size(nbytes: int) -> str:
    """Format bytes as human-readable string."""
    if nbytes < 1024:
        return f"{nbytes}B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f}KB"
    return f"{nbytes / (1024 * 1024):.1f}MB"


class ProxyAdapter(BaseSourceAdapter):
    """Captures HTTP traffic via mitmdump subprocess."""

    def __init__(
        self,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        flow_store: FlowStore | None = None,
        listen_host: str = "0.0.0.0",
        listen_port: int = 8080,
    ) -> None:
        super().__init__(
            adapter_id="proxy",
            adapter_type="mitmproxy",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.flow_store = flow_store or FlowStore()
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    @staticmethod
    def _find_mitmdump() -> str:
        """Locate the mitmdump binary, preferring the active venv."""
        # Check next to the running Python (same venv)
        venv_bin = Path(sys.executable).parent / "mitmdump"
        if venv_bin.is_file():
            return str(venv_bin)
        # Fall back to system PATH
        found = shutil.which("mitmdump")
        if found:
            return found
        raise FileNotFoundError("mitmdump not found")

    async def start(self) -> None:
        """Spawn mitmdump with our addon and begin reading output."""
        try:
            mitmdump = self._find_mitmdump()
        except FileNotFoundError:
            self._error = (
                "mitmdump not found. Install mitmproxy: pip install mitmproxy"
            )
            logger.warning(self._error)
            return

        cmd = [
            mitmdump,
            "-s", str(ADDON_PATH),
            "--listen-host", self.listen_host,
            "--listen-port", str(self.listen_port),
            "--quiet",
        ]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            self._error = f"Failed to start mitmdump: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(
            "Proxy adapter started (mitmdump on %s:%d)",
            self.listen_host,
            self.listen_port,
        )

    async def stop(self) -> None:
        """Terminate the mitmdump subprocess and clean up."""
        self._running = False

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        self._process = None
        self._read_task = None
        logger.info("Proxy adapter stopped")

    async def send_command(self, command: dict) -> None:
        """Send a JSON command to the addon via stdin."""
        if self._process and self._process.stdin:
            data = json.dumps(command, separators=(",", ":")) + "\n"
            self._process.stdin.write(data.encode("utf-8"))
            await self._process.stdin.drain()

    def status(self):
        """Override to report 'proxying' when running."""
        s = super().status()
        if s.status == "streaming":
            s.status = "proxying"
        return s

    async def _read_loop(self) -> None:
        """Read JSON Lines from mitmdump stdout and dispatch."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            async for raw_line in self._process.stdout:
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from mitmdump: %s", line[:200])
                    continue

                msg_type = data.get("type")
                if msg_type == "flow":
                    await self._handle_flow(data)
                elif msg_type == "status":
                    logger.info("Proxy addon status: %s", data.get("event"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("Proxy read loop failed")
        finally:
            self._running = False

    async def _handle_flow(self, data: dict) -> None:
        """Process a flow event from the addon."""
        flow = self._parse_flow(data)
        if flow is None:
            return

        # 1. Store full flow record
        await self.flow_store.add(flow)

        # 2. Emit summary log entry into the processing pipeline
        level = _classify_level(flow)
        message = _format_summary(flow)

        entry = LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=flow.timestamp,
            device_id=self.device_id,
            process="network",
            subsystem=flow.request.host,
            level=level,
            message=message,
            source=LogSource.PROXY,
        )
        await self.emit(entry)

    def _parse_flow(self, data: dict) -> FlowRecord | None:
        """Parse addon JSON into a FlowRecord."""
        try:
            req_data = data.get("request", {})
            request = FlowRequest(
                method=req_data.get("method", ""),
                url=req_data.get("url", ""),
                host=req_data.get("host", ""),
                path=req_data.get("path", ""),
                headers=req_data.get("headers", {}),
                body=req_data.get("body"),
                body_size=req_data.get("body_size", 0),
                body_truncated=req_data.get("body_truncated", False),
                body_encoding=req_data.get("body_encoding", "utf-8"),
            )

            response = None
            resp_data = data.get("response")
            if resp_data:
                response = FlowResponse(
                    status_code=resp_data.get("status_code", 0),
                    reason=resp_data.get("reason", ""),
                    headers=resp_data.get("headers", {}),
                    body=resp_data.get("body"),
                    body_size=resp_data.get("body_size", 0),
                    body_truncated=resp_data.get("body_truncated", False),
                    body_encoding=resp_data.get("body_encoding", "utf-8"),
                )

            timing_data = data.get("timing", {})
            timing = FlowTiming(
                dns_ms=timing_data.get("dns_ms"),
                connect_ms=timing_data.get("connect_ms"),
                tls_ms=timing_data.get("tls_ms"),
                request_ms=timing_data.get("request_ms"),
                response_ms=timing_data.get("response_ms"),
                total_ms=timing_data.get("total_ms"),
            )

            ts = data.get("timestamp", 0)
            timestamp = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else self._now()

            return FlowRecord(
                id=data.get("id", uuid.uuid4().hex[:12]),
                timestamp=timestamp,
                device_id=self.device_id,
                request=request,
                response=response,
                timing=timing,
                tls=data.get("tls"),
                error=data.get("error"),
                tags=[],
            )
        except Exception as e:
            logger.warning("Failed to parse flow data: %s", e)
            return None
