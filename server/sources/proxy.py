"""Source adapter for mitmproxy network traffic capture.

Spawns `mitmdump` with our addon script as a subprocess and reads
JSON Lines from its stdout. Each flow is dual-emitted:
  1. Full FlowRecord -> FlowStore
  2. Summary LogEntry -> processing pipeline (dedup -> ring buffer)

Follows the same subprocess pattern as SyslogAdapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import shutil
import signal as signal_mod
import subprocess
import sys
import time
import uuid
from collections import Counter, deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from mitmproxy import flowfilter

from server.lifecycle.state import update_state
from server.models import (
    FlowRecord,
    FlowRequest,
    FlowResponse,
    FlowTiming,
    LogEntry,
    LogLevel,
    LogSource,
    TlsRejection,
)
from server.proxy.flow_store import PENDING_MAX, FlowStore
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

#: How the trusted-simulator set reaches mitmdump at spawn (#354). Spelled out
#: here rather than imported, because importing the addon into the server
#: process would run its module-level monkey-patch of mitmproxy's connection
#: handler here too. `tests/test_sim_tls.py` asserts the two spellings agree.
TRUSTED_SIMULATORS_ENV = "QUERN_TRUSTED_SIMULATORS"
#: Makes a session proxy pass through every connection not from a simulator
#: (#558); the addon's spelling, asserted equal by `tests/test_session_proxies.py`.
SIMULATORS_ONLY_ENV = "QUERN_SIMULATORS_ONLY"
#: Past this many tracked in-flight ids, keep only those the store still holds:
#: it evicts past PENDING_MAX, and a request that never finishes would
#: otherwise stay tracked here until the proxy stops. Twice the store's bound,
#: so a full store does not make every request start re-scan the set.
PENDING_IDS_TRIM = 2 * PENDING_MAX
#: Where the addon writes events: a pipe of their own, not mitmdump's stdout.
#: mitmproxy logs errors and their tracebacks to stdout even with `--quiet`,
#: and a traceback line that is only a string literal parses as JSON -- which
#: ended the read loop twice in a CI run and took local capture down with it.
EVENT_FD_ENV = "QUERN_EVENT_FD"
#: The longest event line read whole. A flow carries its bodies; past this the
#: line is skipped and logged, never fatal.
EVENT_LINE_LIMIT = 1024 * 1024
#: Bad lines logged in full before the log thins out to one in every hundred,
#: so something spewing cannot fill the server log.
BAD_LINES_LOGGED = 20
#: The most of one mitmdump stdout/stderr line that reaches the server log.
LOGGED_LINE_MAX = 2000


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


def _signal(process: asyncio.subprocess.Process, method: str) -> None:
    """Send a process a signal, treating "it is already gone" as done.

    asyncio raises ProcessLookupError once it has torn the subprocess transport
    down, which an exit already in flight can do between our check and the
    call. Gone is the outcome the signal was for, and an exception here skipped
    recording why the proxy stopped (CodeRabbit on #413).
    """
    with contextlib.suppress(ProcessLookupError):
        getattr(process, method)()


def _rules_locked(method):
    """Run a rule-changing method under the adapter's rules lock (#562)."""
    @functools.wraps(method)
    async def locked(self, *args, **kwargs):
        async with self._rules_lock:
            return await method(self, *args, **kwargs)
    return locked


class ProxyAdapter(BaseSourceAdapter):
    """Captures HTTP traffic via mitmdump subprocess."""

    def __init__(
        self,
        device_id: str = "",
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
        self._stdout_task: asyncio.Task | None = None
        # The addon's events, on their own pipe (EVENT_FD_ENV). None only
        # before a start, and in tests that hand the loop an iterable stdout.
        self._events: asyncio.StreamReader | None = None
        self._stdout_events_warned = False
        self._events_transport: asyncio.ReadTransport | None = None
        self._bad_lines = 0

        # Intercept state (server-side mirror of addon state)
        self._intercept_pattern: str | None = None
        self._held_flows: dict[str, dict] = {}  # flow_id -> {id, held_at, request}
        #: Clients that refused our certificate, newest last. Collapsed by
        #: (host, device) and bounded, so a retrying app cannot crowd out the
        #: other devices' rejections.
        self._tls_rejections: deque[TlsRejection] = deque(maxlen=50)
        self._intercept_event: asyncio.Event = asyncio.Event()

        # Mock state (server-side mirror)
        self._mock_rules: list[dict] = []  # [{rule_id, pattern, response, simulator_udid}]

        # Bypass state (server-side mirror)
        self._bypass_patterns: list[str] = []
        #: Rule changes and `restart()` take turns (#562): a mock set while the
        #: proxy restarts was either cleared by the stop or recorded with no
        #: process to send it to, and quern then claimed a rule mitmdump lacked.
        self._rules_lock = asyncio.Lock()
        #: Session proxies (#558): more mitmdumps, each capturing other apps in
        #: local mode, that take this one's rules -- every rule change is
        #: broadcast to them -- and report into the same flow store. Set on
        #: the main adapter; a session proxy has `leader` instead.
        self.followers: list[ProxyAdapter] = []
        self.leader: ProxyAdapter | None = None
        #: Requests this run has in flight, so a stop drops only its own: the
        #: store is shared with the other proxies.
        self._pending_ids: set[str] = set()
        #: Status events the addon has sent, by name, across runs: a restart
        #: compares counts to know the new process is serving with its rules.
        self._addon_events: Counter[str] = Counter()
        #: How long a restart waits for that before reporting it unconfirmed.
        self.restart_confirm_timeout = 5.0

        # Simulators whose TLS the addon may decrypt (#354); None = all of
        # them. Unlike the state above this is NOT cleared on stop: it is a
        # fact about the simulators, not about this run of mitmdump. The
        # starting value trusts nobody, which passes TLS through -- the safe
        # direction.
        self._trusted_simulators: list[str] | None = []
        #: Refreshes the set just before every spawn, so no start path can
        #: launch mitmdump with a stale list. Set by the lifespan.
        self.trust_provider: Callable[[], Awaitable[list[str] | None]] | None = None
        #: Connections passed through, per simulator, since the proxy started.
        self._passthrough: dict[str, dict] = {}
        # What the addon itself last confirmed it decrypts, and how many
        # confirmations have arrived -- so a caller can tell "sent" from
        # "in effect" (#414).
        self._addon_trusted: list[str] | None = None
        self._addon_trust_seq = 0
        #: Told about each passed-through connection, so the server can re-check
        #: a simulator it has not confirmed rather than wait for the next tick.
        self.on_passthrough: Callable[[str], None] | None = None

    @property
    def local_capture(self) -> bool:
        """Whether local capture is enabled: processes configured here, or a
        session proxy capturing some (#558)."""
        return bool(self.local_capture_processes) or any(
            f.is_running for f in self.followers)

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
        """Spawn mitmdump with our addon and begin reading output.

        Sends the new process every mock, bypass pattern and intercept the
        server holds (#562), so a start after a failed restart or a crash does
        not leave the server listing rules mitmdump lacks.
        """
        async with self._rules_lock:
            await self._start()

    async def _start(self) -> bool:
        """`start()` under the rules lock. True when mitmdump was started and
        sent the rules."""
        if self._running:
            # A second spawn would kill this run (`_kill_stale_mitmdump`) and
            # report the first caller's proxy as up while it was being replaced.
            return False
        if not await self._spawn():
            return False
        if not await self._send_rules():
            # Its stdin broke, so it is going away with some rules unsent.
            # Stopped here, not left to the read loop, so the next start is
            # not turned away by the guard above and sends them all.
            await self._stop(keep_rules=True)
            return False
        return True

    async def _spawn(self) -> bool:
        """Launch mitmdump and its readers. False when it did not start."""
        try:
            mitmdump = self._find_mitmdump()
        except FileNotFoundError:
            self._error = (
                "mitmdump not found. Install mitmproxy: pip install mitmproxy"
            )
            logger.warning(self._error)
            return False

        # Kill any stale mitmdump from a previous run. A session proxy has no
        # port: it is found by pid instead (`server.proxy.session_proxies`).
        if self.listen_port is not None:
            await asyncio.to_thread(self._kill_stale_mitmdump, self.listen_port)

        cmd = [
            mitmdump,
            "-s", str(ADDON_PATH),
            "--listen-host", self.listen_host,
            "--ssl-insecure",
            "--quiet",
        ]

        if self.listen_port is None:
            # A session proxy (#558): local mode only. Nothing listens, so it
            # needs no port and cannot take the main proxy's.
            cmd.extend(["--mode", "local:" + ",".join(self.local_capture_processes)])
        elif self.local_capture_processes:
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

        # Before the spawn, so the addon starts with a current set rather than
        # catching up from stdin while connections are already arriving. A
        # provider that fails trusts nobody: the previous set may name a
        # simulator erased since.
        if self.trust_provider is not None:
            try:
                self._trusted_simulators = await self.trust_provider()
            except Exception:
                logger.exception("Could not refresh trusted simulators before start")
                self._trusted_simulators = []
        spawned_with = self.trusted_simulators
        env = dict(os.environ)
        env[TRUSTED_SIMULATORS_ENV] = (
            "*" if self._trusted_simulators is None
            else ",".join(self._trusted_simulators)
        )

        # The events pipe. The child keeps the write end; the parent closes its
        # copy as soon as the child has it, so the read end sees EOF exactly
        # when mitmdump exits.
        read_fd, write_fd = os.pipe()
        env[EVENT_FD_ENV] = str(write_fd)
        if self.leader is not None:
            env[SIMULATORS_ONLY_ENV] = "1"
        else:
            env.pop(SIMULATORS_ONLY_ENV, None)  # never inherited by the main one
        # mitmproxy's logger prints without flushing, so on a pipe its errors
        # sat in an 8 KB block until something else flushed -- the addon's
        # events did that by accident, on the shared stdout. Unbuffered, a
        # traceback reaches the log when it happens, and a crash cannot lose it.
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                pass_fds=(write_fd,),
                limit=EVENT_LINE_LIMIT,
            )
        except Exception as e:
            os.close(read_fd)
            self._error = f"Failed to start mitmdump: {e}"
            logger.error(self._error)
            return False
        finally:
            os.close(write_fd)

        # The file object owns the descriptor from here: on a failure it is
        # closed through it, never by number -- the number may already belong
        # to something else by then.
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        try:
            loop = asyncio.get_running_loop()
            self._events = asyncio.StreamReader(limit=EVENT_LINE_LIMIT, loop=loop)
            self._events_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(self._events, loop=loop),
                read_file,
            )
        except Exception as e:
            # No channel means no events: a proxy that captures and reports
            # nothing. Stop it rather than run it blind.
            read_file.close()
            self._events = None
            self._error = f"Could not read mitmdump's events: {e}"
            logger.error(self._error)
            _signal(self._process, "kill")
            await self._process.wait()
            # Its own pipes too, not left to asyncio and the collector: stdin
            # in particular reaches no EOF, and a count of open descriptors
            # taken straight after read three high on a loaded CI runner.
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                transport = (getattr(stream, "_transport", None)
                             or getattr(stream, "transport", None))
                if transport is not None:
                    transport.close()
            await asyncio.sleep(0)
            self._process = None
            return False

        self._running = True
        self.started_at = self._now()
        # A start that succeeded supersedes whatever ended the last run. Kept,
        # `/proxy/status` went on reporting "error" for a proxy that was up and
        # capturing, which reads as down to anything gating on it.
        self._error = None
        self._bad_lines = 0
        self._stdout_events_warned = False
        # Cleared here as well as in `stop()`, because the field says "since the
        # proxy started" and `stop()` is not always what ended the last run: if
        # mitmdump exits on its own, `_read_loop` just falls out of its loop and
        # sets `_running = False`, so the next `start()` would report the dead
        # subprocess's rejections against the new one.
        self._tls_rejections.clear()
        self._passthrough.clear()
        # A refresh that landed while mitmdump was being spawned updated the
        # mirror but had no process to send to. Send it now, or the addon runs
        # on the set from before it.
        if self.trusted_simulators != spawned_with:
            await self.send_command({
                "action": "set_trusted_simulators",
                "udids": self._trusted_simulators,
            })
        # Bound here, not when the task first runs: a start replacing
        # `_process` and `_events` before then would have this run's loop read
        # the next run's stream.
        process = self._process
        stream = self._events if self._events is not None else process.stdout
        self._read_task = asyncio.create_task(self._read_loop(process, stream))
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._stdout_task = asyncio.create_task(self._drain_stdout())
        if self.listen_port is None:
            logger.info("Session proxy started (mitmdump capturing %s)",
                        ", ".join(self.local_capture_processes))
        else:
            logger.info(
                "Proxy adapter started (mitmdump on %s:%d)",
                self.listen_host,
                self.listen_port,
            )
        return True

    async def _send_rules(self) -> bool:
        """Send a new process the rules the server holds. False when a send
        failed: the process is going away (its stdin broke), and the rules stay
        listed for the next start rather than half-sent and forgotten."""
        rules = self.leader or self
        try:
            for rule in rules._mock_rules:
                await self.send_command({
                    "action": "set_mock",
                    "rule_id": rule["rule_id"],
                    "pattern": rule["pattern"],
                    "response": rule["response"],
                    "simulator_udid": rule.get("simulator_udid"),
                })
            if rules._bypass_patterns:
                await self.send_command({
                    "action": "set_bypass", "patterns": list(rules._bypass_patterns),
                })
            if rules._intercept_pattern is not None:
                await self.send_command({
                    "action": "set_intercept", "pattern": rules._intercept_pattern,
                })
        except Exception:
            logger.exception("Could not send the proxy rules to mitmdump")
            return False
        return True

    async def stop(self) -> None:
        """Terminate the mitmdump subprocess and clean up, rules included."""
        async with self._rules_lock:
            await self._stop(keep_rules=False)

    async def _stop(self, *, keep_rules: bool) -> None:
        """`stop()` under the rules lock. `keep_rules` keeps mocks, bypass
        patterns and the intercept for the next start to send (#562)."""
        self._running = False
        # Held before any await, so an overlapping stop never re-reads what
        # another has cleared.
        process = self._process
        tasks = (self._read_task, self._stderr_task, self._stdout_task)

        # Also written here, not only from the addon's "stopped" event: a hard
        # kill gives the addon no chance to emit, and the state file would then
        # keep claiming the proxy is running. Off-thread for the same reason as
        # the handler -- this runs on the event loop.
        if self.leader is None:  # state.json describes the main proxy
            await asyncio.to_thread(update_state, proxy_status="stopped")

        if process and process.returncode is None:
            _signal(process, "terminate")
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                _signal(process, "kill")

        # mitmdump exiting closes the events pipe, so the read loop -- which
        # runs to EOF -- dispatches the events written before the stop: a flow
        # that completed just then is kept, not cancelled away. Before the
        # intercept and mock state below is cleared, which those events may use.
        await self._drain(*tasks)

        if self._process is not process:
            # A start landed while this stop was waiting: the process, pipes,
            # mocks and intercepts below are the new run's. Clearing them
            # closed its events pipe, so its read loop ended and terminated a
            # proxy that start_proxy had just reported running.
            logger.info("Proxy adapter stopped (a newer run is active; left in place)")
            return

        if self._events_transport is not None:
            self._events_transport.close()
        self._events_transport = None
        self._events = None
        self._process = None
        self._read_task = None
        self._stderr_task = None
        self._stdout_task = None

        # Held flows belong to the process: the addon releases them, unmodified,
        # as it shuts down.
        self._held_flows.clear()
        if not keep_rules:
            self._intercept_pattern = None
            self._mock_rules.clear()
            self._bypass_patterns.clear()
        # Rejections too. Restarting the proxy is what someone does *after*
        # installing the cert, so carrying them across would report a problem
        # they have just fixed.
        self._tls_rejections.clear()

        logger.info("Proxy adapter stopped")

    async def send_command(self, command: dict) -> None:
        """Send a JSON command to the addon via stdin."""
        if self._process and self._process.stdin:
            data = json.dumps(command, separators=(",", ":")) + "\n"
            self._process.stdin.write(data.encode("utf-8"))
            await self._process.stdin.drain()

    async def broadcast(self, command: dict) -> None:
        """Send a command to this mitmdump and every session proxy following it
        (#558). A follower that cannot take it is logged, not raised: it is going
        away, and the next start sends it the rules from here anyway."""
        await self.send_command(command)
        for follower in list(self.followers):
            try:
                await follower.send_command(command)
            except Exception:
                logger.exception("Could not send %s to a session proxy",
                                 command.get("action"))

    @property
    def trusted_simulators(self) -> list[str] | None:
        """UDIDs whose TLS is decrypted; None means every simulator."""
        return None if self._trusted_simulators is None else list(self._trusted_simulators)

    async def set_trusted_simulators(self, udids: list[str] | None) -> None:
        """Replace the set of simulators whose TLS may be decrypted.

        Kept across restarts and handed to the next mitmdump at spawn; sent to
        a running one over stdin.
        """
        self._trusted_simulators = (
            None if udids is None else sorted({u.upper() for u in udids})
        )
        await self.broadcast({
            "action": "set_trusted_simulators",
            "udids": self._trusted_simulators,
        })

    @property
    def addon_trust_seq(self) -> int:
        """How many trusted-set confirmations the addon has sent."""
        return self._addon_trust_seq

    async def addon_decrypts(self, udid: str, *, after_seq: int,
                             timeout: float = 3.0) -> bool | None:
        """Whether the addon decrypts `udid`, by its own confirmation of a set
        sent after `after_seq`. None when no such confirmation came in time:
        what was sent is not yet known to be in effect."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._addon_trust_seq <= after_seq:
            if loop.time() >= deadline or not self.is_running:
                return None
            await asyncio.sleep(0.05)
        confirmed = self._addon_trusted
        return confirmed is None or udid.upper() in confirmed

    def passthrough_counts(self) -> dict[str, dict]:
        """Connections passed through since start, keyed by simulator UDID --
        by this proxy and its session proxies, which see other apps on the
        same simulators (#558)."""
        merged: dict[str, dict] = {}
        for adapter in self._all():
            for udid, entry in adapter._passthrough.items():
                into = merged.setdefault(
                    udid, {"connections": 0, "last_host": None, "last_at": None})
                into["connections"] += entry["connections"]
                if (entry["last_at"] or "") >= (into["last_at"] or ""):
                    into["last_host"] = entry["last_host"]
                    into["last_at"] = entry["last_at"]
        return merged

    def tls_rejections(self) -> list[TlsRejection]:
        """Clients that refused our certificate, this proxy's and its session
        proxies'."""
        return [r for a in self._all() for r in a._tls_rejections]

    def _handle_tls_passthrough(self, data: dict) -> None:
        udid = data.get("simulator_udid")
        if not udid:
            return
        entry = self._passthrough.setdefault(
            udid, {"connections": 0, "last_host": None, "last_at": None},
        )
        entry["connections"] += 1
        if data.get("sni"):
            entry["last_host"] = data["sni"]
        entry["last_at"] = datetime.fromtimestamp(
            data.get("timestamp") or time.time(), tz=UTC,
        ).isoformat()
        if self.on_passthrough is not None:
            try:
                self.on_passthrough(udid)
            except Exception:
                logger.exception("on_passthrough failed")

    def status(self):
        """Override to report 'proxying' when running."""
        s = super().status()
        if s.status == "streaming":
            s.status = "proxying"
        return s

    # -------------------------------------------------------------------
    # Intercept convenience methods
    # -------------------------------------------------------------------

    async def restart(self, **settings) -> dict | None:
        """Stop mitmdump and start it again, keeping the rules callers set (#562).

        `stop()` clears mocks, the intercept pattern and bypass patterns, which
        is right for a stop someone asked for and wrong for a restart nobody
        asked for: changing local capture restarts mitmdump -- its process list
        is a launch argument, and on macOS a live mode change leaves the
        redirector on the old list -- and every mock on the server silently
        stopped applying. A restart keeps them, and the start sends them to the
        new process, rule ids and device scopes unchanged.

        `settings` are passed to `reconfigure` between the stop and the start.

        Held flows cannot come across: each is a request waiting inside the old
        process, which releases it unmodified as it exits. The count released
        is returned, with what was re-sent -- None when the new process did not
        start or could not be sent the rules, which then wait for the next
        start -- so the caller can say what happened. `confirmed` says the new
        process reported it was serving (`running`) and echoed every rule
        within `restart_confirm_timeout`: until then, traffic is not captured
        or mocked, so the call returns only once it is, or says it is not.
        None, with the settings
        applied for the next start, when the proxy was no longer running: a
        stop that got the lock first is not undone.
        """
        async with self._rules_lock:
            if not self._running:
                self.reconfigure(**settings)
                return None
            held = len(self._held_flows)
            await self._stop(keep_rules=True)
            # `reconfigure` refuses while running, so new settings -- a local
            # capture list -- go in between the stop and the start.
            if settings:
                self.reconfigure(**settings)
            before = Counter(self._addon_events)
            sent = await self._start()
            restored = {
                "mock_rules": len(self._mock_rules),
                "bypass_patterns": len(self._bypass_patterns),
                "intercept": self._intercept_pattern,
            } if sent else None
            confirmed = sent and await self._serving_with_rules(before)
            return {
                "held_flows_released": held,
                "restored": restored,
                "confirmed": confirmed,
            }

    async def _serving_with_rules(self, before: Counter[str]) -> bool:
        """Whether the addon has, since `before`, said it is serving and echoed
        each rule `_send_rules` sent. False on timeout -- an addon on disk older
        than the server never sends `running` -- or if the process ended."""
        rules = self.leader or self
        want = {"running": 1, "mock_set": len(rules._mock_rules)}
        if rules._bypass_patterns:
            want["bypass_updated"] = 1
        if rules._intercept_pattern is not None:
            want["intercept_set"] = 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.restart_confirm_timeout
        while any(self._addon_events[k] - before[k] < n for k, n in want.items()):
            if loop.time() >= deadline or not self.is_running:
                return False
            await asyncio.sleep(0.05)
        # The events can all be in when the process has already gone. Its read
        # loop clears `_running` at EOF, which the loop above never re-checks
        # once nothing is missing -- and until the loop gets there, only the
        # return code says so.
        process = self._process
        return self.is_running and (process is None or process.returncode is None)

    @_rules_locked
    async def set_intercept(self, pattern: str) -> None:
        """Set an intercept pattern on the addon. Raises ValueError if pattern is invalid."""
        validate_filter_pattern(pattern)
        self._intercept_pattern = pattern
        await self.broadcast({"action": "set_intercept", "pattern": pattern})

    @_rules_locked
    async def clear_intercept(self) -> None:
        """Clear the intercept pattern and release all held flows."""
        self._intercept_pattern = None
        for adapter in self._all():
            adapter._held_flows.clear()
        await self.broadcast({"action": "clear_intercept"})

    async def release_flow(self, flow_id: str, modifications: dict | None = None) -> None:
        """Release a single held flow, optionally with modifications. Sent to the
        proxy holding it, which may be a session proxy (#558)."""
        holder = next((a for a in self._all() if flow_id in a._held_flows), self)
        if modifications:
            await holder.send_command({
                "action": "modify_and_release",
                "flow_id": flow_id,
                "modifications": modifications,
            })
        else:
            await holder.send_command({"action": "release_flow", "flow_id": flow_id})

    async def release_all(self) -> None:
        """Release all held flows."""
        for adapter in self._all():
            adapter._held_flows.clear()
        await self.broadcast({"action": "release_all"})

    @_rules_locked
    async def set_mock(
        self, pattern: str, response: dict,
        rule_id: str | None = None,
        simulator_udid: str | None = None,
    ) -> str:
        """Add a mock response rule. Returns the rule_id.

        `simulator_udid` scopes the rule to one simulator's requests; the
        addon fails closed on any request it cannot attribute.

        Raises ValueError if pattern is invalid.
        """
        validate_filter_pattern(pattern)
        if rule_id is None:
            rule_id = f"mock_{uuid.uuid4().hex[:8]}"
        self._mock_rules.append({
            "rule_id": rule_id, "pattern": pattern,
            "response": response, "simulator_udid": simulator_udid,
        })
        await self.broadcast({
            "action": "set_mock",
            "rule_id": rule_id,
            "pattern": pattern,
            "response": response,
            "simulator_udid": simulator_udid,
        })
        return rule_id

    @_rules_locked
    async def update_mock(
        self, rule_id: str, pattern: str | None = None,
        response: dict | None = None,
        simulator_udid: str | None = None,
        *,
        scope_given: bool = False,
    ) -> dict:
        """Update an existing mock rule. Returns the updated rule.

        The scope changes only when `scope_given`: then `simulator_udid` is the
        new scope, None meaning every device. Otherwise the rule keeps the
        scope it had -- an update that names only a new body must not quietly
        widen a scoped mock to every simulator.

        Raises ValueError if not found or pattern invalid.
        """
        rule = next((r for r in self._mock_rules if r["rule_id"] == rule_id), None)
        if rule is None:
            raise ValueError(f"Mock rule not found: {rule_id}")
        new_pattern = pattern if pattern is not None else rule["pattern"]
        new_response = response if response is not None else rule["response"]
        new_scope = simulator_udid if scope_given else rule.get("simulator_udid")
        if pattern is not None:
            validate_filter_pattern(new_pattern)
        updated = {
            "rule_id": rule_id, "pattern": new_pattern,
            "response": new_response, "simulator_udid": new_scope,
        }
        # Replaced in place, here and in the addon, which replaces a rule_id
        # it already holds. Clearing and re-adding moved the rule to the end,
        # behind rules set after it -- so updating a scoped rule's body could
        # leave it shadowed by a catch-all.
        self._mock_rules = [updated if r["rule_id"] == rule_id else r for r in self._mock_rules]
        await self.broadcast({
            "action": "set_mock",
            "rule_id": rule_id,
            "pattern": new_pattern,
            "response": new_response,
            "simulator_udid": new_scope,
        })
        return updated

    @_rules_locked
    async def clear_mock(self, rule_id: str | None = None) -> int:
        """Remove a specific mock rule or all of them. Returns how many went.

        The count is what makes a deletion checkable. Without it the endpoint
        could not tell "removed it" from "it was never there", and answered
        `200 {"status": "deleted"}` for an id that had never been a rule --
        while `PATCH` on the same id answered 404. A caller tearing down rules
        by id could not detect that it had failed to remove one, and a leaked
        mock does not sit there inertly: it goes on matching real traffic
        (#182).

        The command is still sent when nothing matched locally. The list here
        is the record, but the addon holds its own copy, and a clear for a
        rule it does not have is a no-op -- so sending covers the drift case
        at no cost, and the return value describes *this* record rather than
        what the addon did with it.
        """
        if rule_id:
            before = len(self._mock_rules)
            self._mock_rules = [r for r in self._mock_rules if r["rule_id"] != rule_id]
            removed = before - len(self._mock_rules)
        else:
            removed = len(self._mock_rules)
            self._mock_rules.clear()
        await self.broadcast({"action": "clear_mock", "rule_id": rule_id})
        return removed

    # -------------------------------------------------------------------
    # Bypass convenience methods
    # -------------------------------------------------------------------

    @_rules_locked
    async def set_bypass(self, patterns: list[str]) -> list[str]:
        """Add bypass patterns. Returns the full list after update."""
        for p in patterns:
            if p not in self._bypass_patterns:
                self._bypass_patterns.append(p)
        await self.broadcast({
            "action": "set_bypass",
            "patterns": patterns,
        })
        return list(self._bypass_patterns)

    @_rules_locked
    async def remove_bypass(self, patterns: list[str]) -> list[str]:
        """Remove specific bypass patterns. Returns the remaining list."""
        self._bypass_patterns = [
            p for p in self._bypass_patterns
            if p not in patterns
        ]
        await self.broadcast({
            "action": "remove_bypass",
            "patterns": patterns,
        })
        return list(self._bypass_patterns)

    @_rules_locked
    async def clear_bypass(self) -> None:
        """Remove all bypass patterns."""
        self._bypass_patterns.clear()
        await self.broadcast({"action": "clear_bypass"})

    def get_bypass_patterns(self) -> list[str]:
        """Return current bypass patterns."""
        return list(self._bypass_patterns)

    def _all(self) -> list[ProxyAdapter]:
        """This proxy and the running session proxies following it. One whose
        mitmdump has gone holds nothing any more -- its held flows went with it
        -- and stays listed only until the next claim prunes it."""
        return [self, *(f for f in self.followers if f.is_running)]

    def held_count(self) -> int:
        """Flows held by this proxy and its session proxies."""
        return sum(len(a._held_flows) for a in self._all())

    def holds(self, flow_id: str) -> bool:
        return any(flow_id in a._held_flows for a in self._all())

    def forget_held(self, flow_id: str) -> None:
        """Drop a released flow from whichever proxy held it (the addon's own
        `released` event does the same, later)."""
        for adapter in self._all():
            adapter._held_flows.pop(flow_id, None)

    def get_held_flows(self) -> list[dict]:
        """Return held flows with computed age_seconds, session proxies' too."""
        now = datetime.now(UTC)
        result = []
        held = {k: v for a in self._all() for k, v in a._held_flows.items()}
        for flow_id, info in held.items():
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
        except TimeoutError:
            return False

    # -------------------------------------------------------------------
    # Read loop and event handlers
    # -------------------------------------------------------------------

    async def _lines(
        self,
        stream: asyncio.StreamReader | AsyncIterable[bytes],
        on_overlong: Callable[[], None],
    ) -> AsyncIterator[bytes]:
        """Raw lines from a stream, skipping any line past its reader's limit.

        StreamReader raises for an over-long line, and an `async for` over it
        ends there. A drain that ends leaves its pipe unread, the pipe fills,
        and mitmdump blocks writing to it -- the proxy frozen while reporting
        healthy. So no single line may end any of the three readers.
        """
        if not isinstance(stream, asyncio.StreamReader):
            async for raw_line in stream:
                yield raw_line
            return
        while True:
            try:
                raw_line = await stream.readline()
            except ValueError:
                on_overlong()
                continue
            if not raw_line:
                return
            yield raw_line

    async def _event_lines(self, stream=None) -> AsyncIterator[bytes]:
        """Raw lines from the addon: its events pipe, or stdout without one.

        A line past EVENT_LINE_LIMIT is skipped, not fatal. StreamReader raises
        for it and discards what it buffered, and an `async for` would have
        ended there -- the same death as a bad line, by another road.
        """
        if stream is None:
            assert self._process is not None
            stream = self._events if self._events is not None else self._process.stdout
        assert stream is not None
        async for raw_line in self._lines(
            stream,
            lambda: self._bad_line("an event line over the size limit was skipped", b""),
        ):
            yield raw_line

    def _bad_line(self, what: str, line: bytes | str) -> None:
        """Log a line the loop could not use, quoting it -- bounded."""
        self._bad_lines += 1
        if self._bad_lines <= BAD_LINES_LOGGED or self._bad_lines % 100 == 0:
            text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
            logger.warning(
                "mitmdump: %s (%d so far): %r", what, self._bad_lines, text[:500],
            )

    async def _dispatch(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "flow":
            await self._handle_flow(data)
        elif msg_type == "request_started":
            self._handle_started(data)
        elif msg_type == "intercepted":
            self._handle_intercepted(data)
        elif msg_type == "released":
            self._handle_released(data)
        elif msg_type == "status":
            await self._handle_status_event(data)
        elif msg_type == "tls_rejected":
            self._handle_tls_rejected(data)
        elif msg_type == "tls_passthrough":
            self._handle_tls_passthrough(data)
        elif msg_type == "error":
            logger.warning("Addon error: %s", data)

    async def _read_loop(self, process=None, stream=None) -> None:
        """Read the addon's events and dispatch them, one line at a time.

        Nothing a single line holds may end this loop. It did, twice in one
        CI run: a line that parsed as a JSON string reached `data.get`, the
        exception left the loop, and mitmdump kept running with nobody reading
        it -- so local capture routed the simulator into a pipe that filled and
        stalled, and every request failed until someone restarted the proxy.
        A bad line now costs that line, and a handler's bug costs its event.
        """
        # start() passes this run's process and stream; a direct call reads the
        # adapter's current ones.
        if process is None:
            assert self._process is not None
            process = self._process
        try:
            async for raw_line in self._event_lines(stream):
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    self._bad_line("a line that is not JSON", line)
                    continue
                if not isinstance(data, dict):
                    self._bad_line("a JSON line that is not an object", line)
                    continue

                try:
                    await self._dispatch(data)
                except Exception:
                    logger.exception(
                        "Proxy event handler failed; skipping the event: %r", line[:500],
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running and self._process is process:
                self._error = f"Read loop error: {e}"
            # Logged during stop's drain too, which processes what the stream
            # had already written; a failure there would otherwise leave no trace.
            logger.exception("Proxy read loop failed")
        finally:
            # Only this run's: start_proxy and stop_proxy share no lock, so a
            # loop still draining can outlive its run. It must not mark the new
            # run stopped, drop the new run's in-flight flows, or end it.
            mine = self._process is process
            unexpected = self._running and mine
            if mine:
                self._running = False
                self._drop_own_pending()
            if unexpected:
                await self._end_unexpected_run(process)

    def _drop_own_pending(self) -> None:
        """Nothing mitmdump was carrying will finish now. Left in place, those
        requests would read as in flight forever. Only this proxy's: the store
        is shared with any session proxies (#558)."""
        dropped = self.flow_store.drop_pending(
            self._pending_ids, announce=self.leader is None)
        self._pending_ids.clear()
        if dropped:
            logger.info("Proxy stopped with %d request(s) in flight", dropped)

    async def _end_unexpected_run(self, process: asyncio.subprocess.Process) -> None:
        """The loop ended without `stop()`: report it, and leave nothing half-alive.

        A mitmdump nobody reads is worse than none. Local capture keeps routing
        the captured apps through it, its output pipe fills, and their requests
        hang -- while a proxy that has exited lets them through uncaptured.
        """
        is_process = isinstance(process, asyncio.subprocess.Process)
        if is_process and process.returncode is None:
            # The events pipe reaches EOF before asyncio has reaped an exiting
            # child, so a crash looked "still running" every time. Give the
            # exit a moment to register before deciding.
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                logger.error("Proxy read loop ended with mitmdump still running; stopping it")
                _signal(process, "terminate")
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    _signal(process, "kill")
                    await process.wait()
        returncode = getattr(process, "returncode", None)
        reason = (
            f"mitmdump exited unexpectedly (code {returncode})"
            if returncode is not None else "mitmdump stopped unexpectedly"
        )
        # Teardown takes seconds, and a start is allowed meanwhile: it only
        # checks `_running`. A newer run owns the status, so this one's ending
        # must not overwrite it.
        if self._process is not process:
            logger.info("An earlier mitmdump ended (%s) after a newer one started", reason)
            return
        if not self._error:
            self._error = reason
        logger.error("Proxy stopped: %s", self._error)
        try:
            if self.leader is None:  # state.json describes the main proxy
                await asyncio.to_thread(update_state, proxy_status="stopped")
        except Exception:
            logger.exception("Could not record the proxy as stopped")

    async def _drain_stdout(self) -> None:
        """Log mitmdump's own stdout: mitmproxy's logger writes there -- its
        errors and tracebacks -- and those are what explain a crash afterwards.

        An event here means the addon on disk predates its pipe: the files
        under a running server changed, by a downgrade or an update whose
        restart failed. Those are dispatched rather than logged, so capture
        keeps working, and said once."""
        assert self._process is not None
        if self._events is None or self._process.stdout is None:
            return  # without a pipe, stdout is the events: _read_loop owns it
        try:
            async for raw_line in self._lines(
                self._process.stdout,
                lambda: logger.warning("mitmdump: skipped a stdout line over the size limit"),
            ):
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if line.startswith("{") and await self._stdout_event(line):
                    continue
                logger.warning("mitmdump: %s", line[:LOGGED_LINE_MAX])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not read mitmdump's stdout")

    async def _stdout_event(self, line: str) -> bool:
        """Dispatch an event an older addon wrote to stdout. True if it was one."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict) or "type" not in data:
            return False
        if not self._stdout_events_warned:
            self._stdout_events_warned = True
            logger.warning(
                "Proxy events are arriving on mitmdump's stdout: the addon on disk "
                "predates its event pipe. Restart quern to run matching code."
            )
        try:
            await self._dispatch(data)
        except Exception:
            logger.exception("Proxy event handler failed; skipping the event: %r", line[:500])
        return True

    async def _drain_stderr(self) -> None:
        """Read and log stderr from mitmdump so errors aren't lost."""
        assert self._process is not None
        assert self._process.stderr is not None
        try:
            async for raw_line in self._lines(
                self._process.stderr,
                lambda: logger.warning("mitmdump stderr: skipped a line over the size limit"),
            ):
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.warning("mitmdump stderr: %s", line[:LOGGED_LINE_MAX])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not read mitmdump's stderr")

    def _handle_tls_rejected(self, data: dict) -> None:
        """A client refused our certificate.

        Kept as an observation, deliberately: it is not yet allowed to change
        any device's recorded trust. Writing it back is the `TrustClaim` work
        in #149, and doing it here would mean a single hostile client on the
        network could mark a device untrusted. Recording and reporting is the
        whole of phase 1.

        Bounded, because an app retrying a rejected handshake produces one of
        these per attempt and this is a long-lived process.
        """
        sni = data.get("sni")
        client_ip = data.get("client_ip")
        at = datetime.fromtimestamp(
            data.get("timestamp") or time.time(), tz=UTC,
        ).isoformat()
        # The alert, verbatim and uninterpreted. Recorded because a
        # certificate-pinned app on a device that trusts the CA perfectly well
        # refuses too, and dropping this left both looking identical, with the
        # log asserting the first -- sending someone to reinstall a
        # certificate that was never wrong.
        #
        # It narrows the possibilities without deciding between them. "With a
        # different alert", which this comment used to claim, is false on
        # Android: measured, an untrusted CA and a pinned client both say
        # `certificate unknown`. See `TlsRejection` for the measurement.
        alert = (data.get("error") or "").strip() or None

        # Collapsed by (host, device). A retrying app produces one of these per
        # attempt, so appending blindly let one loop evict every other device's
        # rejection inside a second -- and the single rejection from the device
        # you were actually debugging is the one that mattered.
        for existing in self._tls_rejections:
            if existing.sni == sni and existing.client_ip == client_ip and (
                existing.simulator_udid == data.get("simulator_udid")
            ) and existing.device_serial == data.get("device_serial"):
                existing.count += 1
                existing.last_at = at
                if alert and not existing.alert:
                    existing.alert = alert
                # Move it to the end. Updating in place left an actively
                # retrying device leftmost, so 50 unique keys would evict the
                # one that was seen most recently -- while its own `last_at`
                # said so. Also what "newest last" means.
                self._tls_rejections.remove(existing)
                self._tls_rejections.append(existing)
                return

        self._tls_rejections.append(TlsRejection(
            sni=sni, client_ip=client_ip, alert=alert,
            source_process=data.get("source_process"),
            source_pid=data.get("source_pid"),
            simulator_udid=data.get("simulator_udid"),
            device_serial=data.get("device_serial"),
            count=1, first_at=at, last_at=at,
        ))
        logger.warning(
            "Client %s refused the certificate we offered for %s (%s)",
            client_ip or "?", sni or "?", alert or "no alert reported",
        )

    def _handle_started(self, data: dict) -> None:
        """A request has started (#364): in flight until its flow arrives."""
        flow = self._parse_flow(data)
        if flow is not None:
            self.flow_store.note_started(flow)
            # Only what the store took: a start arriving after its flow is not
            # in flight (CodeRabbit). And trimmed to what it still holds once
            # past its bound, which evicts requests that never finish -- left
            # here, those would grow without limit on a long-lived proxy.
            if self.flow_store.is_pending(flow.id):
                self._pending_ids.add(flow.id)
            if len(self._pending_ids) > PENDING_IDS_TRIM:
                self._pending_ids = {
                    i for i in self._pending_ids if self.flow_store.is_pending(i)}

    async def _handle_flow(self, data: dict) -> None:
        """Process a flow event from the addon."""
        flow = self._parse_flow(data)
        if flow is None:
            return

        # 1. Store full flow record
        self._pending_ids.discard(flow.id)
        await self.flow_store.add(flow)

        # 2. Emit summary log entry into the processing pipeline
        level = _classify_level(flow)
        message = _format_summary(flow)
        if flow.mock_rule_id:
            # The mock's own log line, which used to come from a separate
            # mock_hit record -- one that also stored the flow a second time.
            message = f"MOCK ({flow.mock_rule_id}): {message}"

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
        held_at = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(UTC)

        self._held_flows[flow_id] = {
            "id": flow_id,
            "held_at": held_at,
            "request": req_data,
        }
        self._intercept_event.set()
        if self.leader is not None:
            # Waiters wait on the main adapter, which lists every proxy's.
            self.leader._intercept_event.set()

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

    async def _handle_status_event(self, data: dict) -> None:
        """Handle status events from the addon that update local state mirrors."""
        event = data.get("event")
        self._addon_events[event] += 1
        # update_state() takes fcntl.LOCK_EX and does synchronous file I/O, so
        # calling it inline would block the event loop behind whichever writer
        # holds the lock -- stalling unrelated coroutines for a state write
        # nothing is waiting on.
        if event == "started":
            # state.json's proxy_status was written as "starting" at boot and
            # never advanced, so every unauthenticated consumer saw a proxy that
            # was permanently starting up (#122). The addon's own "started" is
            # the honest signal -- it means the script loaded, rather than
            # merely that the subprocess was spawned. (Its servers come up
            # about 0.1 s later, with "running".) Only the main proxy's:
            # state.json has one proxy_status, and it is the main one's.
            if self.leader is None:
                await asyncio.to_thread(update_state, proxy_status="running")
        elif event == "stopped":
            if self.leader is None:
                await asyncio.to_thread(update_state, proxy_status="stopped")
        elif event == "trusted_simulators_updated":
            udids = data.get("udids")
            self._addon_trusted = None if udids is None else [str(u).upper() for u in udids]
            self._addon_trust_seq += 1
        elif event == "intercept_set":
            self._intercept_pattern = data.get("pattern")
        elif event == "intercept_cleared":
            self._intercept_pattern = None
            self._held_flows.clear()
        elif event == "mock_set":
            # Already tracked in set_mock(), but handle for completeness
            pass
        elif event == "mocks_cleared":
            rule_id = data.get("rule_id")
            if not rule_id:
                # Full clear — sync the mirror. Per-rule clears are already
                # handled by clear_mock() / update_mock() before the echo arrives.
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
            timestamp = datetime.fromtimestamp(ts, tz=UTC) if ts else self._now()

            return FlowRecord(
                id=data.get("id", uuid.uuid4().hex[:12]),
                timestamp=timestamp,
                device_id=self.device_id,
                request=request,
                response=response,
                timing=timing,
                tls=data.get("tls"),
                error=data.get("error"),
                tags=["mocked"] if data.get("mock_rule_id") else [],
                mock_rule_id=data.get("mock_rule_id"),
                source_process=data.get("source_process"),
                source_pid=data.get("source_pid"),
                simulator_udid=data.get("simulator_udid"),
                device_serial=data.get("device_serial"),
                client_ip=data.get("client_ip"),
                started_monotonic=data.get("started_monotonic"),
            )
        except Exception as e:
            logger.warning("Failed to parse flow data: %s", e)
            return None
