# Phase 4a: Process Lifecycle Management

## 1. Problem Statement

The quern-debug-server currently requires manual process management. When an AI agent tries to use the system, it frequently encounters already-running instances, kills them ungracefully, and restarts — wasting time and occasionally corrupting state. The proxy defaults to port 8080, which conflicts with common services (Docker containers, other dev tools). There is no reliable way to detect current system state or recover from partial failures.

Phase 4a makes startup idempotent, shutdown graceful, and state discoverable.

## 2. Architecture Overview

```
quern-debug-server start
        │
        ├── Check state.json → health check existing instance
        │   ├── Alive? → Print status, exit 0 (idempotent)
        │   └── Dead/stale? → Clean up, continue to start
        │
        ├── Find available ports (scan from defaults)
        │
        ├── Fork to background (daemon mode)
        │   ├── Server (FastAPI/uvicorn) on port 9100+
        │   ├── Proxy (mitmdump) on port 9101+ (unless --no-proxy)
        │   └── Write state.json + PID file
        │
        └── Foreground: confirm health check, print status, exit 0

quern-debug-server stop
        │
        ├── Read state.json → get PID
        ├── Send SIGTERM → cascades to proxy child
        ├── Wait for clean shutdown (timeout 5s, then SIGKILL)
        └── Remove state.json + PID file

quern-debug-server status
        │
        ├── Read state.json
        ├── Verify health check
        └── Print: server port, proxy port, uptime, active devices, proxy state
```

## 3. State File

**Location:** `~/.quern/state.json`

This is the single source of truth for all tools, CLI commands, MCP servers, and agents. If the file exists, an instance *should* be running. If the health check against it fails, the file is stale and should be cleaned up before starting fresh.

### Schema

```json
{
  "pid": 12345,
  "server_port": 9100,
  "proxy_port": 9101,
  "proxy_enabled": true,
  "started_at": "2026-02-10T20:15:00Z",
  "api_key": "dks8f...",
  "active_devices": [
    {
      "udid": "ABC12345-6789-DEF0-1234-567890ABCDEF",
      "name": "iPhone 16 Pro",
      "os": "18.2",
      "state": "booted"
    }
  ]
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `pid` | int | Server daemon process ID |
| `server_port` | int | HTTP API port |
| `proxy_port` | int | mitmproxy listener port (0 if proxy disabled) |
| `proxy_enabled` | bool | Whether proxy was started |
| `started_at` | ISO 8601 | Daemon start time |
| `api_key` | string | Current API key (so agents can read it without a separate file lookup) |
| `active_devices` | array | Currently tracked devices (updated dynamically) |

### State File Rules

1. **Write on start.** Daemon writes state.json immediately after binding ports, before accepting requests.
2. **Update on change.** Device list, proxy status changes trigger a rewrite.
3. **Delete on stop.** Clean shutdown removes the file.
4. **Stale detection.** Any reader that finds state.json but fails the health check should treat it as stale. Only the `start` command cleans up stale files — other tools report the error and suggest running `start`.

## 4. Port Selection

### Defaults

- **Server:** 9100
- **Proxy:** 9101

### Selection Algorithm

```python
def find_available_port(preferred: int, max_attempts: int = 20) -> int:
    """Try preferred port, then scan upward."""
    for offset in range(max_attempts):
        port = preferred + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available port found in range {preferred}-{preferred + max_attempts}")
```

### Rules

1. Try the default port first.
2. If occupied, scan upward (9100 → 9101 → 9102...).
3. Server port is found first; proxy port starts scanning from `server_port + 1`.
4. This guarantees server and proxy never collide.
5. Final ports are written to state.json so all tools can discover them.
6. CLI `start` prints the selected ports to stdout before exiting.

### Port Conflict Safety

The old default proxy port of 8080 is **retired**. It conflicts with too many common services (Docker, nginx, development servers). The new default of 9101 is in a rarely-used range. The scanning algorithm means even if 9100-9101 are taken (unlikely), the system finds what's available rather than failing or killing other processes.

## 5. Daemon Lifecycle

### Start (`quern-debug-server start`)

```
1. Check for existing state.json
   a. If exists → GET http://127.0.0.1:{server_port}/health
   b. If healthy → print "Server already running on port {port}", exit 0
   c. If unhealthy → log "Cleaning up stale instance", remove state.json, continue

2. Find available ports
   a. server_port = find_available_port(9100)
   b. proxy_port = find_available_port(server_port + 1)  # if --no-proxy, skip

3. Fork to background
   a. Parent: wait for child to signal readiness (via health check), print status, exit 0
   b. Child: become session leader (os.setsid), redirect stdio to log file, continue

4. Child (daemon) process:
   a. Write PID to state.json (preliminary, ports TBD)
   b. Start FastAPI/uvicorn on server_port
   c. Start mitmdump on proxy_port (unless --no-proxy)
   d. Update state.json with final ports and proxy status
   e. Install signal handlers (SIGTERM → graceful shutdown)
   f. Run event loop

5. Parent verification:
   a. Poll health check (up to 5 seconds, 100ms intervals)
   b. If healthy → print status block, exit 0
   c. If timeout → print error, suggest checking logs, exit 1
```

### Status Output (printed by `start` and `status`)

```
quern-debug-server running
  PID:    12345
  Server: http://127.0.0.1:9100
  Proxy:  http://127.0.0.1:9101
  API Key: dks8f...abcd
  Uptime: 2h 15m
  Devices: iPhone 16 Pro (18.2) [booted]
```

### Stop (`quern-debug-server stop`)

```
1. Read state.json → get PID
   a. If no state.json → print "No server running", exit 0

2. Verify process exists: os.kill(pid, 0)
   a. If not running → clean up state.json, print "Cleaned up stale state", exit 0

3. Send SIGTERM to daemon PID
   a. Daemon's signal handler:
      i.   Stop accepting new requests
      ii.  Stop proxy subprocess (SIGTERM → wait 2s → SIGKILL)
      iii. Flush any pending state
      iv.  Remove state.json
      v.   Exit 0

4. Wait for process to exit (timeout 5s)
   a. If still running after 5s → SIGKILL, clean up state.json manually

5. Print "Server stopped", exit 0
```

### Restart (`quern-debug-server restart`)

Sugar for `stop` + `start`. Preserves any CLI flags from the original start if none are specified (reads them from state.json).

## 6. Proxy Subprocess Management

### Watchdog

The proxy (mitmdump) runs as a child process of the daemon. The daemon monitors it:

```python
async def _proxy_watchdog(self):
    """Monitor proxy subprocess, update status on unexpected exit."""
    while self._running:
        if self._proxy_process and self._proxy_process.returncode is not None:
            logger.warning("Proxy exited unexpectedly (code %d)", self._proxy_process.returncode)
            self._proxy_status = "crashed"
            self._update_state_file()
            # Do NOT auto-restart — report degraded status
            # Agent or user can explicitly restart proxy if needed
            break
        await asyncio.sleep(1.0)
```

### Design Decision: No Auto-Restart

If the proxy crashes, the daemon marks status as degraded rather than auto-restarting. Rationale:
- Auto-restart can mask persistent errors (bad addon, port conflict after start)
- The agent should know the proxy died so it can decide what to do
- Explicit restart via MCP tool or CLI is safer
- The server continues functioning without the proxy — logs still work

### Proxy Pause/Resume vs. Stop/Start

Two levels of control:

| Action | What happens | Proxy process | Use case |
|--------|-------------|---------------|----------|
| **Pause capture** | Addon stops recording flows | Stays running | "I don't need network data right now" |
| **Resume capture** | Addon starts recording again | Stays running | "Start capturing again" |
| **Restart proxy** | Kill + relaunch mitmdump | Recycled | "Reset proxy state / change port" |
| **Stop server** | Everything shuts down | Killed | "Done for now" |

Pause/resume is fast (no process overhead) and preserves the proxy's TLS state and flow store. The agent should prefer pause/resume over restart in most cases.

## 7. CLI Interface

### Commands

```
quern-debug-server start [OPTIONS]
    --no-proxy          Don't start the proxy
    --port PORT         Server port (default: 9100, auto-scans if taken)
    --proxy-port PORT   Proxy port (default: server_port + 1)
    --verbose           Daemon logs to stderr as well as log file
    --foreground        Don't daemonize (for debugging the server itself)

quern-debug-server stop
    No options. Finds and stops the running instance.

quern-debug-server restart [OPTIONS]
    Same options as start. Stops existing instance first.

quern-debug-server status
    No options. Reads state.json and verifies health.
    Exit code 0 if running, 1 if not.

quern-debug-server run [OPTIONS]        # Phase 4c — deferred
    --device DEVICE     Device name or UDID
    --proxy / --no-proxy
    --prompt PROMPT     Scenario to execute
    --output FILE       Results file
```

### Daemon Log File

Daemon stdout/stderr redirects to `~/.quern/server.log`. Rotated by size (10MB max, keep 3). The `--foreground` flag skips daemonization and logs to the terminal — essential for debugging the server itself.

## 8. MCP Integration

### New Tool: `ensure_server`

The primary tool agents use. Replaces the current pattern of "try to start, handle errors, retry."

```typescript
{
  name: "ensure_server",
  description: "Ensure the Quern debug server is running. Idempotent — starts if needed, reuses if already running.",
  inputSchema: {
    type: "object",
    properties: {
      proxy: { type: "boolean", default: true, description: "Whether to enable the network proxy" }
    }
  }
}
```

**Implementation:** Reads state.json, health checks, returns connection info. If not running, shells out to `quern-debug-server start`, waits for health, returns info.

**Response:**

```json
{
  "status": "running",
  "server_url": "http://127.0.0.1:9100",
  "proxy_port": 9101,
  "api_key": "dks8f...",
  "uptime_seconds": 8100,
  "proxy_status": "active",
  "devices": [
    {"name": "iPhone 16 Pro", "udid": "ABC...", "os": "18.2"}
  ]
}
```

### Updated Tool: `server_status`

Returns the same info as `ensure_server` but never starts the server. Returns `{"status": "not_running"}` if no instance is found.

### Updated MCP Guide Resource

The guide resource should be updated to tell agents:

> Always call `ensure_server` at the start of a session. Do not attempt to start the server manually via shell commands. The tool handles idempotent startup, port discovery, and returns the API key and connection details you need for all subsequent calls.

## 9. State Discovery for External Tools

Any tool (not just MCP) can discover the running server by reading `~/.quern/state.json`. This enables:

- **Claude Code:** Read state.json to find the API endpoint and key
- **Shell scripts:** `jq .server_port ~/.quern/state.json` to get the port
- **CI systems:** Check for state.json to know if the server is available
- **The `run` command (4c):** Uses state.json internally to connect to the running daemon

The state file is the contract between the daemon and all consumers.

## 10. Error Scenarios

| Scenario | Behavior |
|----------|----------|
| `start` when already running | Print status, exit 0 |
| `start` with stale state.json | Clean up, start fresh |
| `start` and preferred port taken | Scan upward, use next available |
| `start` and no ports available in range | Error with clear message |
| `stop` when not running | Print "No server running", exit 0 |
| `stop` and process won't die | SIGKILL after 5s timeout |
| Proxy crashes while server runs | Server continues, proxy status = "crashed" |
| Agent calls `ensure_server` | Idempotent — starts or reuses |
| Two agents call `ensure_server` simultaneously | File lock on state.json prevents race |
| `start --foreground` | No daemonization, logs to terminal |

## 11. File Lock for Concurrent Access

State.json writes use `fcntl.flock()` to prevent races when multiple tools read/write simultaneously:

```python
import fcntl

def write_state(state: dict) -> None:
    state_path = CONFIG_DIR / "state.json"
    with open(state_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(state, f, indent=2)
        # Lock released on file close

def read_state() -> dict | None:
    state_path = CONFIG_DIR / "state.json"
    if not state_path.exists():
        return None
    with open(state_path) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        return json.load(f)
```

## 12. Implementation Plan

### Step 1: State Management (foundation)

- `server/lifecycle/state.py` — read/write/validate state.json with file locking
- `server/lifecycle/ports.py` — port scanning and availability checking
- Tests for state file operations, port scanning, stale detection

### Step 2: Daemon Mode

- `server/lifecycle/daemon.py` — fork, setsid, stdio redirect, signal handlers
- Modify `server/main.py` — integrate daemon mode with existing `create_app()`
- `--foreground` flag preserves current behavior for debugging
- Tests for daemon start/stop/status (process-level integration tests)

### Step 3: CLI Commands

- Refactor `cli()` in `main.py` to support subcommands: `start`, `stop`, `restart`, `status`
- `start` → daemon fork + health check verification
- `stop` → PID lookup + SIGTERM + cleanup
- `status` → state.json read + health check + formatted output
- `restart` → stop + start

### Step 4: Proxy Port Migration

- Change proxy default from 8080 → 9101
- Update ProxyAdapter to accept dynamic port from port scanner
- Update all tests referencing port 8080
- Update CLAUDE.md and documentation

### Step 5: Proxy Watchdog

- `server/lifecycle/watchdog.py` — async monitor for proxy subprocess
- Integration with proxy status reporting
- Update state.json on proxy state changes

### Step 6: MCP Tools

- `ensure_server` tool — reads state, health checks, starts if needed
- Update `server_status` tool with new state.json data
- Update MCP guide resource with new agent workflow

### Step 7: Integration Testing

- Full lifecycle test: start → status → use → stop
- Idempotent start test: start twice, second is no-op
- Stale recovery test: kill daemon without stop, then start
- Port conflict test: occupy 9100, verify scan to 9102
- Proxy crash test: kill mitmdump, verify degraded status

## 13. Design Decisions Summary

1. **Daemon mode with fork.** Agents and CLI don't hold a terminal. `--foreground` for debugging.
2. **state.json as single source of truth.** All tools discover the server through this file. No environment variables, no hardcoded ports.
3. **Port 9101 for proxy.** Retiring 8080. Scanning algorithm prevents conflicts.
4. **No proxy auto-restart.** Crash → degraded status, not silent restart. Agent decides.
5. **Idempotent start.** Running `start` when already running is a successful no-op, not an error.
6. **File locking for concurrency.** Multiple agents/tools can safely read state.json.
7. **`ensure_server` as the agent's entry point.** One tool call replaces the current fragile multi-step startup dance.
8. **Single proxy for multiple simulators.** One mitmproxy instance handles all traffic. Attribution by app/host, not by proxy instance.
9. **Pause/resume over stop/start for proxy.** Preserves TLS state, avoids process churn.
10. **Log file rotation.** Daemon logs to `~/.quern/server.log`, 10MB rotation, keep 3.
