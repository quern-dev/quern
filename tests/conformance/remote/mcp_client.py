"""The quern MCP, driven over stdio as an agent's client drives it.

JSON-RPC, one message per line, as the MCP stdio transport defines it. A tool
answer is text: usually JSON, sometimes followed by notice lines (a session
notice, a hint), so the JSON is read from the front and the rest kept.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
DIST = REPO / "mcp" / "dist" / "index.js"


class McpError(RuntimeError):
    """The MCP answered a request with a JSON-RPC error, or not at all."""


class ToolAnswer:
    """One tool call's answer: the JSON it led with, the full text, and
    whether the MCP marked it an error."""

    def __init__(self, result: dict) -> None:
        self.is_error = bool(result.get("isError"))
        self.text = "".join(c.get("text", "") for c in result.get("content", [])
                            if c.get("type") == "text")
        try:
            self.data, _ = json.JSONDecoder().raw_decode(self.text.lstrip())
        except ValueError:
            self.data = None

    def __repr__(self) -> str:
        return f"ToolAnswer(is_error={self.is_error}, text={self.text[:400]!r})"


class McpClient:
    """One MCP process: one agent. Its HOME is a sandbox, so `~/.quern` --
    the local server's state and the paired remotes -- is the test's."""

    def __init__(self, node: str, home: Path, project: Path, *, timeout: float = 60) -> None:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("QUERN_") and k != "HOME"}
        env["HOME"] = str(home)
        self.timeout = timeout
        self.stderr: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next = 0
        self.proc = subprocess.Popen(  # noqa: S603 - this checkout's own MCP
            [node, str(DIST)], cwd=project, env=env, text=True, bufsize=1,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._lines),
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            self._request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "quern-conformance", "version": "1"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            # The fixture's finally is not entered yet: an MCP that never
            # answered would otherwise outlive the run (CR). Killed, not
            # asked: one that did not answer this will not answer a close.
            self.proc.kill()
            self.proc.wait(timeout=5)
            raise

    @staticmethod
    def _pump(stream, out: queue.Queue) -> None:
        for line in stream:
            out.put(line)
        out.put(None)

    def _drain_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())

    def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self._next += 1
        ident = self._next
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        limit = timeout or self.timeout
        # One deadline for the whole request: notifications in between must
        # not each start the wait again, or a chatty MCP that never answers
        # holds the test for ever (CR).
        deadline = time.monotonic() + limit
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise McpError(f"no answer to {method} within {limit}s; "
                               f"stderr: {self.stderr[-5:]}") from None
            if line is None:
                raise McpError(f"the MCP exited ({self.proc.poll()}) during {method}; "
                               f"stderr: {self.stderr[-10:]}")
            try:
                message = json.loads(line)
            except ValueError:
                continue            # not ours: the transport carries only JSON-RPC
            if message.get("id") != ident:
                continue            # a notification, or a request from the server
            if "error" in message:
                raise McpError(f"{method}: {message['error']}")
            return message["result"]

    def call(self, tool: str, timeout: float | None = None, **arguments: Any) -> ToolAnswer:
        return ToolAnswer(self._request("tools/call", {"name": tool, "arguments": arguments},
                                        timeout=timeout))

    def close(self) -> None:
        """End the agent cleanly: stdin closed, as a client does, which ends
        its held session -- and the mocks it set -- at once. Killed if it
        will not go."""
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait(timeout=5)
