# Phase 2b: Automatic System Proxy Configuration & Cleanup

## Problem

iOS simulators inherit the host Mac's system proxy settings — they have no independent proxy configuration. To capture simulator traffic through mitmproxy, the user must configure macOS system proxy via `networksetup`. The current `proxy_setup_guide` endpoint generates the correct commands, but the user (or agent) must execute them manually.

The critical failure mode: if `stop_proxy()` is called (or the server shuts down) without reverting the system proxy, **the machine loses internet connectivity** because traffic is still being routed to a proxy that no longer exists. Recovery requires the user to manually run cleanup commands or navigate to System Settings.

## Current State

- `start_proxy()` spawns the `mitmdump` subprocess. Does not touch system proxy.
- `stop_proxy()` terminates `mitmdump`. Does not touch system proxy.
- `GET /proxy/setup-guide` returns `networksetup` commands as text strings for manual execution.
- `state.json` tracks `proxy_status` ("running"/"stopped"/"crashed"/"disabled") but has no record of system proxy configuration.
- Server shutdown (`lifespan` teardown, `remove_state()`) does not run any cleanup commands.
- Helper functions already exist for detecting the active network interface: `_detect_active_interface()`, `_get_default_route_device()`, `_bsd_device_to_service_name()`.

## Design

### Core Pattern: Snapshot → Configure → Restore

1. **Before configuring**, read the current system proxy state for the active interface and save it.
2. **Configure** the system proxy to point at mitmproxy.
3. **On stop/shutdown/crash**, restore the saved state exactly.

### Snapshot Format

Capture the current proxy state for a given network interface:

```python
@dataclass
class SystemProxySnapshot:
    interface: str          # e.g. "Wi-Fi"
    http_proxy_enabled: bool
    http_proxy_server: str  # empty string if disabled
    http_proxy_port: int    # 0 if disabled
    https_proxy_enabled: bool
    https_proxy_server: str
    https_proxy_port: int
    timestamp: str          # ISO 8601, when snapshot was taken
```

Read via:
```
networksetup -getwebproxy "Wi-Fi"
networksetup -getsecurewebproxy "Wi-Fi"
```

These return structured output like:
```
Enabled: Yes
Server: 127.0.0.1
Port: 9101
Authenticated Proxy Enabled: 0
```

### State Persistence

Add to `ServerState` in `state.json`:

```python
class ServerState(TypedDict, total=False):
    # ... existing fields ...
    system_proxy_configured: bool           # True if we touched system settings
    system_proxy_interface: str | None      # Which interface we configured
    system_proxy_snapshot: dict | None      # Pre-configuration state (serialized SystemProxySnapshot)
```

This ensures cleanup survives server restarts — if the server crashes and restarts, it can read state.json and still restore the original proxy settings.

### Automatic Behavior

**`start_proxy()`** changes:
1. Spawn mitmdump (existing behavior)
2. Detect active interface via `_detect_active_interface()`
3. Snapshot current proxy state for that interface
4. Run `networksetup -setwebproxy` and `-setsecurewebproxy`
5. Save snapshot + interface to `state.json`
6. Log what was done

If interface detection fails, log a warning and skip system proxy configuration (mitmdump still starts — user can configure manually or use the standalone endpoint).

**`stop_proxy()`** changes:
1. Terminate mitmdump (existing behavior)
2. If `system_proxy_configured` in state: restore from snapshot
3. Clear `system_proxy_*` fields in state.json
4. Log what was restored

**Server shutdown** (lifespan teardown):
1. Stop all adapters including proxy (existing)
2. If `system_proxy_configured`: restore from snapshot
3. `remove_state()` (existing)

**Signal handling** (SIGTERM/SIGINT):
1. Restore system proxy if configured
2. Existing cleanup

### Restore Logic

Restoring from snapshot means:

- If the original state had proxy enabled → re-set to the original server:port
- If the original state had proxy disabled → turn it off with `-setwebproxystate ... off`

```python
async def _restore_system_proxy(snapshot: SystemProxySnapshot) -> None:
    iface = snapshot.interface
    if snapshot.http_proxy_enabled:
        subprocess.run(["networksetup", "-setwebproxy", iface,
                        snapshot.http_proxy_server, str(snapshot.http_proxy_port)])
    else:
        subprocess.run(["networksetup", "-setwebproxystate", iface, "off"])

    if snapshot.https_proxy_enabled:
        subprocess.run(["networksetup", "-setsecurewebproxy", iface,
                        snapshot.https_proxy_server, str(snapshot.https_proxy_port)])
    else:
        subprocess.run(["networksetup", "-setsecurewebproxystate", iface, "off"])
```

## API Changes

### Modified Endpoints

**`POST /proxy/start`** — new optional fields in request body:

```json
{
  "port": 9101,
  "listen_host": "0.0.0.0",
  "system_proxy": true
}
```

- `system_proxy` (bool, default: `true`): Whether to auto-configure macOS system proxy.
- When `true`, the response includes a `system_proxy` object with the interface and status.

Response additions:
```json
{
  "status": "running",
  "port": 9101,
  "system_proxy": {
    "configured": true,
    "interface": "Wi-Fi",
    "original_state": "disabled"
  }
}
```

**`POST /proxy/stop`** — response additions:

```json
{
  "status": "stopped",
  "system_proxy": {
    "restored": true,
    "interface": "Wi-Fi",
    "restored_to": "disabled"
  }
}
```

If system proxy was not configured by us, `system_proxy` is `null`.

### New Endpoints

**`POST /proxy/configure-system`** — manually configure macOS system proxy

Use case: proxy is already running but system proxy wasn't set (e.g. `start_proxy(system_proxy=false)` was called, or the user wants to re-apply after a VPN change).

Request body:
```json
{
  "interface": "Wi-Fi"
}
```

- `interface` (string, optional): Override auto-detected interface.

Response:
```json
{
  "configured": true,
  "interface": "Wi-Fi",
  "proxy_target": "127.0.0.1:9101",
  "original_state": "disabled"
}
```

Errors:
- 409 if system proxy is already configured by us
- 503 if proxy is not running (nothing to route to)
- 500 if `networksetup` command fails

**`POST /proxy/unconfigure-system`** — restore macOS system proxy to pre-configuration state

Use case: disable system proxy while keeping mitmproxy running (e.g. done testing simulator, switching to physical device proxy config).

Response:
```json
{
  "restored": true,
  "interface": "Wi-Fi",
  "restored_to": "disabled"
}
```

Errors:
- 409 if system proxy was not configured by us (nothing to restore)

## MCP Tools

### Modified Tools

**`start_proxy`** — add `system_proxy` parameter:
```typescript
{
  port: z.number().optional(),
  listen_host: z.string().optional(),
  system_proxy: z.boolean().optional().describe(
    "Configure macOS system proxy automatically (default: true). "
    "Required for simulator traffic capture."
  ),
}
```

**`stop_proxy`** — no parameter changes. Tool description updated to mention system proxy restore. Response now includes `system_proxy.restored` status.

### New Tools

**`configure_system_proxy`**
```typescript
{
  interface: z.string().optional().describe(
    "Network interface name (e.g. 'Wi-Fi'). Auto-detected if omitted."
  ),
}
```

**`unconfigure_system_proxy`** — no parameters. Restores original system proxy state.

## File Changes

| File | Change |
|------|--------|
| `server/api/proxy.py` | Modify `start_proxy`/`stop_proxy`, add `configure_system`/`unconfigure_system` endpoints, add `_snapshot_system_proxy()`, `_configure_system_proxy()`, `_restore_system_proxy()`, `_parse_networksetup_output()` helpers |
| `server/lifecycle/state.py` | Add `system_proxy_configured`, `system_proxy_interface`, `system_proxy_snapshot` to `ServerState` |
| `server/main.py` | Add system proxy restore to lifespan shutdown path |
| `server/models.py` | Add `SystemProxySnapshot` model, update `ProxyStatusResponse` |
| `mcp/src/index.ts` | Add `system_proxy` param to `start_proxy`, add `configure_system_proxy`/`unconfigure_system_proxy` tools |
| `tests/` | Tests for snapshot/configure/restore logic, mocked `subprocess.run` |

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Interface detection fails | Log warning, skip system proxy config, mitmdump still starts |
| `networksetup` command fails | Log error, raise 500 with stderr message, do NOT leave partial state |
| VPN active (utun default route) | Existing `_detect_proxy_warnings()` warns; configure anyway (user opted in) |
| Server crashes without cleanup | Next `start` reads stale `state.json`, sees `system_proxy_configured=true`, restores before proceeding |
| `networksetup` requires admin password | macOS typically allows proxy config without sudo for the current user; if it fails, surface the error clearly |
| Snapshot interface no longer exists | Log warning, skip restore (interface was likely a VPN that disconnected) |
| External proxy change while Quern is running | See "Known Limitations" below |
| SIGKILL / force-kill | See "Known Limitations" below |

## Known Limitations

### External proxy changes are overwritten on restore

If something modifies the system proxy while Quern owns it — the user manually changes it, a VPN client reconfigures it, a corporate MDM policy pushes a PAC URL — Quern's `stop_proxy()` will blindly restore from the snapshot taken at configure time. The intentional change is lost.

This is an acceptable trade-off. The alternative (re-reading current state, diffing against what we expect, and prompting for confirmation) adds significant complexity for a rare scenario. The restore path must be fast and unconditional to be reliable, especially during signal handling.

**Mitigation:** Log a clear message on every restore:

```
[INFO] Restoring system proxy to pre-Quern state (interface: Wi-Fi).
       If you modified proxy settings manually while Quern was running, those changes will be lost.
```

### SIGKILL leaves system proxy configured

SIGKILL (signal 9) cannot be caught. If the server process is force-killed (`kill -9`), no cleanup runs and the system proxy remains pointed at a dead mitmproxy instance. The machine will have no internet until the proxy is manually disabled or Quern is restarted.

**Mitigation:** The snapshot is persisted in `state.json`. The next `quern-debug-server start` checks for `system_proxy_configured=true` in stale state and restores before proceeding. The `quern-debug-server stop` command also checks for stale state. So the window of broken connectivity lasts only until the user runs any Quern CLI command.

**Recovery without Quern:**
```bash
networksetup -setwebproxystate "Wi-Fi" off
networksetup -setsecurewebproxystate "Wi-Fi" off
```

Or via System Settings > Network > Wi-Fi > Proxies.

## Testing

- **Unit tests**: Mock `subprocess.run` for all `networksetup` calls. Test snapshot parsing, configure, restore, and error paths.
- **State persistence**: Verify `state.json` round-trips snapshot data correctly.
- **Idempotency**: `configure-system` when already configured → 409. `unconfigure-system` when not configured → 409.
- **Crash recovery**: Write state with `system_proxy_configured=true`, simulate server start, verify restore runs.
- **Integration**: No real `networksetup` calls in CI — all mocked.
