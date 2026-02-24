"""Source adapter for mitmproxy network traffic capture.

Spawns `mitmdump` with our addon script as a subprocess and reads
JSON Lines from its stdout. Each flow is dual-emitted:
  1. Full FlowRecord -> FlowStore
  2. Summary LogEntry -> processing pipeline (dedup -> ring buffer)

Follows the same subprocess pattern as SyslogAdapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal as signal_mod
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import flowfilter

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


def validate_filter_pattern(pattern: str) -> None:
    """Validate a mitmproxy filter pattern. Raises ValueError if invalid."""
    try:
        result = flowfilter.parse(pattern)
    except ValueError as e:
        raise ValueError(str(e)) from e
    if result is None:
        raise ValueError(f"Invalid filter expression: {pattern!r}")

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
        parts.append(f"-> {resp.status_code} {resp.reason}".rstrip())
        if flow.timing.total_ms is not None:
            parts.append(f"({flow.timing.total_ms:.0f}ms")
            if resp.body_size:
                parts.append(f", {_human_size(resp.body_size)})")
            else:
                parts.append(")")
        elif resp.body_size:
            parts.append(f"({_human_size(resp.body_size)})")
    elif flow.error:
        parts.append(f"-> ERROR: {flow.error}")

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
        listen_port: int = 9101,
        local_capture_processes: list[str] | None = None,
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
        self.local_capture_processes: list[str] = local_capture_processes or []
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

        # Intercept state (server-side mirror of addon state)
        self._intercept_pattern: str | None = None
        self._held_flows: dict[str, dict] = {}  # flow_id -> {id, held_at, request}
        self._intercept_event: asyncio.Event = asyncio.Event()

        # Mock state (server-side mirror)
        self._mock_rules: list[dict] = []  # [{rule_id, pattern}]

    @property
    def local_capture(self) -> bool:
        """Whether local capture is enabled (any processes configured)."""
        return bool(self.local_capture_processes)

    def reconfigure(
        self,
        listen_port: int | None = None,
        listen_host: str | None = None,
        local_capture_processes: list[str] | None = None,
    ) -> None:
        """Update listen config. Only allowed when stopped."""
        if self._running:
            raise RuntimeError("Cannot reconfigure while running")
        if listen_port is not None:
            self.listen_port = listen_port
        if listen_host is not None:
            self.listen_host = listen_host
        if local_capture_processes is not None:
            self.local_capture_processes = local_capture_processes

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

    @staticmethod
    def _kill_stale_mitmdump(port: int) -> None:
        """Find and kill any stale mitmdump holding our listen port."""
        import socket

        # Find PIDs listening on the port
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return  # Port is free
        except Exception:
            return

        killed_any = False
        for pid_str in result.stdout.strip().splitlines():
            try:
                pid = int(pid_str.strip())
            except ValueError:
                continue

            # Check if this is OUR mitmdump (has our addon path)
            try:
                ps_result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, timeout=5,
                )
                cmd = ps_result.stdout.strip()
            except Exception:
                continue

            addon_marker = str(ADDON_PATH)
            if "mitmdump" not in cmd or addon_marker not in cmd:
                logger.warning(
                    "Port %d held by non-quern process (pid %d): %s",
                    port, pid, cmd[:120],
                )
                continue  # Not ours — don't touch it

            logger.warning("Killing stale mitmdump (pid %d) on port %d", pid, port)
            try:
                os.kill(pid, signal_mod.SIGTERM)
                # Brief wait for clean exit
                for _ in range(10):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    os.kill(pid, signal_mod.SIGKILL)
                killed_any = True
            except ProcessLookupError:
                killed_any = True

        if not killed_any:
            return

        # Wait for the port to actually be free (OS may hold it briefly)
        for _ in range(20):  # up to 2 seconds
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return  # Port is free
                except OSError:
                    time.sleep(0.1)
        logger.warning("Port %d still busy after killing stale mitmdump", port)

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

        # Kill any stale mitmdump from a previous run
        await asyncio.to_thread(self._kill_stale_mitmdump, self.listen_port)

        cmd = [
            mitmdump,
            "-s", str(ADDON_PATH),
            "--listen-host", self.listen_host,
            "--ssl-insecure",
            "--quiet",
        ]

        if self.local_capture_processes:
            # --mode regular@PORT handles the listen port; --mode local:Process1,Process2
            # adds transparent capture for specific processes via macOS System Extension.
            # Don't also pass --listen-port or mitmdump will try to bind the same address twice.
            process_list = ",".join(self.local_capture_processes)
            cmd.extend([
                "--mode", f"regular@{self.listen_port}",
                "--mode", f"local:{process_list}",
            ])
        else:
            cmd.extend(["--listen-port", str(self.listen_port)])

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                limit=1024 * 1024,  # 1MB line buffer (default 64KB too small for large bodies)
            )
        except Exception as e:
            self._error = f"Failed to start mitmdump: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
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

        for task in (self._read_task, getattr(self, "_stderr_task", None)):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._process = None
        self._read_task = None
        self._stderr_task = None

        # Clear intercept/mock state
        self._intercept_pattern = None
        self._held_flows.clear()
        self._mock_rules.clear()

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

    # -------------------------------------------------------------------
    # Intercept convenience methods
    # -------------------------------------------------------------------

    async def set_intercept(self, pattern: str) -> None:
        """Set an intercept pattern on the addon. Raises ValueError if pattern is invalid."""
        validate_filter_pattern(pattern)
        self._intercept_pattern = pattern
        await self.send_command({"action": "set_intercept", "pattern": pattern})

    async def clear_intercept(self) -> None:
        """Clear the intercept pattern and release all held flows."""
        self._intercept_pattern = None
        self._held_flows.clear()
        await self.send_command({"action": "clear_intercept"})

    async def release_flow(self, flow_id: str, modifications: dict | None = None) -> None:
        """Release a single held flow, optionally with modifications."""
        if modifications:
            await self.send_command({
                "action": "modify_and_release",
                "flow_id": flow_id,
                "modifications": modifications,
            })
        else:
            await self.send_command({"action": "release_flow", "flow_id": flow_id})

    async def release_all(self) -> None:
        """Release all held flows."""
        self._held_flows.clear()
        await self.send_command({"action": "release_all"})

    async def set_mock(self, pattern: str, response: dict, rule_id: str | None = None) -> str:
        """Add a mock response rule. Returns the rule_id. Raises ValueError if pattern is invalid."""
        validate_filter_pattern(pattern)
        if rule_id is None:
            rule_id = f"mock_{uuid.uuid4().hex[:8]}"
        self._mock_rules.append({"rule_id": rule_id, "pattern": pattern, "response": response})
        await self.send_command({
            "action": "set_mock",
            "rule_id": rule_id,
            "pattern": pattern,
            "response": response,
        })
        return rule_id

    async def clear_mock(self, rule_id: str | None = None) -> None:
        """Remove a specific mock rule or all mock rules."""
        if rule_id:
            self._mock_rules = [r for r in self._mock_rules if r["rule_id"] != rule_id]
        else:
            self._mock_rules.clear()
        await self.send_command({"action": "clear_mock", "rule_id": rule_id})

    def get_held_flows(self) -> list[dict]:
        """Return held flows with computed age_seconds."""
        now = datetime.now(timezone.utc)
        result = []
        for flow_id, info in self._held_flows.items():
            held_at = info["held_at"]
            age = (now - held_at).total_seconds()
            result.append({
                "id": flow_id,
                "held_at": held_at,
                "age_seconds": round(age, 1),
                "request": info["request"],
            })
        return result

    async def wait_for_held(self, timeout: float) -> bool:
        """Wait for a new flow to be intercepted.

        Clears the event, then waits up to `timeout` seconds for it to be set again.
        Returns True if a new flow was intercepted, False if timeout expired.
        """
        self._intercept_event.clear()
        try:
            await asyncio.wait_for(self._intercept_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -------------------------------------------------------------------
    # Read loop and event handlers
    # -------------------------------------------------------------------

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
                elif msg_type == "intercepted":
                    self._handle_intercepted(data)
                elif msg_type == "released":
                    self._handle_released(data)
                elif msg_type == "mock_hit":
                    await self._handle_mock_hit(data)
                elif msg_type == "status":
                    self._handle_status_event(data)
                elif msg_type == "error":
                    logger.warning("Addon error: %s", data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("Proxy read loop failed")
        finally:
            self._running = False

    async def _drain_stderr(self) -> None:
        """Read and log stderr from mitmdump so errors aren't lost."""
        assert self._process is not None
        assert self._process.stderr is not None
        try:
            async for raw_line in self._process.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.warning("mitmdump stderr: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

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

    def _handle_intercepted(self, data: dict) -> None:
        """Process an intercepted flow event — store in held_flows and signal waiters."""
        flow_id = data.get("id", "")
        req_data = data.get("request", {})
        ts = data.get("timestamp", 0)
        held_at = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)

        self._held_flows[flow_id] = {
            "id": flow_id,
            "held_at": held_at,
            "request": req_data,
        }
        self._intercept_event.set()

        # Emit NOTICE log entry
        method = req_data.get("method", "?")
        path = req_data.get("path", "?")
        entry = LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=held_at,
            device_id=self.device_id,
            process="network",
            subsystem=req_data.get("host", ""),
            level=LogLevel.NOTICE,
            message=f"INTERCEPTED: {method} {path}",
            source=LogSource.PROXY,
        )
        # Fire-and-forget emit (synchronous context)
        asyncio.ensure_future(self.emit(entry))

    def _handle_released(self, data: dict) -> None:
        """Remove a flow from held_flows when released."""
        flow_id = data.get("id", "")
        self._held_flows.pop(flow_id, None)

    async def _handle_mock_hit(self, data: dict) -> None:
        """Process a mock hit — create FlowRecord and emit log entry."""
        flow = self._parse_flow(data)
        if flow is None:
            return

        await self.flow_store.add(flow)

        req = flow.request
        status = flow.response.status_code if flow.response else "?"
        entry = LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=flow.timestamp,
            device_id=self.device_id,
            process="network",
            subsystem=req.host,
            level=LogLevel.INFO,
            message=f"MOCK: {req.method} {req.path} -> {status}",
            source=LogSource.PROXY,
        )
        await self.emit(entry)

    def _handle_status_event(self, data: dict) -> None:
        """Handle status events from the addon that update local state mirrors."""
        event = data.get("event")
        if event == "intercept_set":
            self._intercept_pattern = data.get("pattern")
        elif event == "intercept_cleared":
            self._intercept_pattern = None
            self._held_flows.clear()
        elif event == "mock_set":
            # Already tracked in set_mock(), but handle for completeness
            pass
        elif event == "mocks_cleared":
            rule_id = data.get("rule_id")
            if rule_id:
                self._mock_rules = [r for r in self._mock_rules if r["rule_id"] != rule_id]
            else:
                self._mock_rules.clear()
        else:
            logger.info("Proxy addon status: %s", event)

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
                source_process=data.get("source_process"),
                source_pid=data.get("source_pid"),
                simulator_udid=data.get("simulator_udid"),
                client_ip=data.get("client_ip"),
            )
        except Exception as e:
            logger.warning("Failed to parse flow data: %s", e)
            return None
