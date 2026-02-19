# Simulator Snapshots & Device Tags

**Status**: Spec
**Date**: February 2026

---

## Problem

Setting up simulators for testing is slow and repetitive. Every test run that needs a logged-in state, a proxy cert installed, or password autofill disabled must either:

1. Manually prepare each simulator before running tests, or
2. Automate the setup flow each time (login, cert install, settings changes) — slow and fragile

Snapshots solve this by capturing a fully-configured simulator state and restoring it in seconds. Tags solve the complementary problem of identifying which simulators are in which state, so test code can request devices by capability rather than by UDID.

---

## Concepts

### Snapshots

A snapshot is a named capture of a simulator's complete state — disk, memory, running processes. Powered by `xcrun simctl snapshot` under the hood. Restoring a snapshot is fast (~2-3s) compared to a cold boot + app setup (~30-60s).

Example snapshots:
- `"clean-proxy"` — Fresh simulator with proxy cert trusted and password autofill disabled
- `"geocaching-logged-in"` — Clean-proxy state plus Geocaching installed and logged in
- `"geocaching-premium"` — Logged in with a premium account

Snapshots are per-simulator. Two simulators can both have a `"clean-proxy"` snapshot, but the actual disk contents are independent.

### Tags

A tag is a string label attached to a device pool entry. Tags are stored in `device-pool.json` alongside claim status and other pool metadata. They persist across server restarts.

Example tags:
- `"geocaching"` — Has Geocaching installed
- `"proxy-ready"` — Proxy cert installed and verified
- `"premium-account"` — Logged into a premium account

Tags describe the *current* state. Restoring a snapshot should update tags to match. Tags can also be set manually for physical devices or states that don't have snapshots.

### Configurations (named presets)

A configuration ties together a snapshot name, a set of tags, and optional setup metadata. Stored in `~/.quern/config.json`. This is the user-facing concept — tests reference configurations by name rather than dealing with snapshots and tags separately.

```json
{
  "default_device_family": "iPhone",
  "configurations": {
    "clean-proxy": {
      "snapshot": "clean-proxy",
      "tags": ["proxy-ready"],
      "description": "Fresh simulator with proxy cert and autofill disabled"
    },
    "geocaching-logged-in": {
      "snapshot": "geocaching-logged-in",
      "tags": ["geocaching", "proxy-ready", "logged-in"],
      "description": "Geocaching installed and logged into test account"
    },
    "geocaching-premium": {
      "snapshot": "geocaching-premium",
      "tags": ["geocaching", "proxy-ready", "logged-in", "premium-account"],
      "description": "Geocaching with premium test account"
    }
  }
}
```

---

## Data Model Changes

### DevicePoolEntry

```python
class DevicePoolEntry(BaseModel):
    # ... existing fields ...
    tags: list[str] = Field(default_factory=list)
    current_snapshot: str | None = None  # Last restored/saved snapshot name
```

### New Models

```python
class SnapshotInfo(BaseModel):
    """A saved simulator snapshot."""
    name: str
    created_at: datetime
    device_udid: str
    device_name: str
    size_bytes: int | None = None  # If available from simctl

class SaveSnapshotRequest(BaseModel):
    udid: str | None = None  # Auto-resolves if omitted
    name: str
    tags: list[str] = Field(default_factory=list)  # Update device tags after save

class RestoreSnapshotRequest(BaseModel):
    udid: str | None = None
    name: str  # Snapshot name or configuration name

class ListSnapshotsRequest(BaseModel):
    udid: str | None = None

class DeleteSnapshotRequest(BaseModel):
    udid: str | None = None
    name: str
```

### Request Model Updates

```python
class ResolveDeviceRequest(BaseModel):
    # ... existing fields ...
    tags: list[str] | None = None       # Require all listed tags
    snapshot: str | None = None         # Restore this snapshot after resolution
    config: str | None = None           # Shorthand: resolve + restore + tag

class EnsureDevicesRequest(BaseModel):
    # ... existing fields ...
    tags: list[str] | None = None
    snapshot: str | None = None
    config: str | None = None
```

---

## API Endpoints

### Snapshot Management

```
POST /api/v1/devices/snapshot/save
POST /api/v1/devices/snapshot/restore
GET  /api/v1/devices/snapshot/list?udid=<udid>
POST /api/v1/devices/snapshot/delete
```

#### POST /snapshot/save

Saves a snapshot of the simulator's current state.

```json
// Request
{ "udid": "DF3B...", "name": "clean-proxy", "tags": ["proxy-ready"] }

// Response
{ "status": "saved", "name": "clean-proxy", "udid": "DF3B..." }
```

Side effects:
- Sets `current_snapshot` on the pool entry
- Replaces device tags with the provided `tags` list (if given)

#### POST /snapshot/restore

Restores a previously saved snapshot. Accepts either a snapshot name (device must have it) or a configuration name from config.json.

```json
// Request — direct snapshot name
{ "udid": "DF3B...", "name": "clean-proxy" }

// Request — configuration name (looked up in config.json)
{ "udid": "DF3B...", "name": "geocaching-logged-in" }

// Response
{ "status": "restored", "name": "clean-proxy", "udid": "DF3B..." }
```

Side effects:
- Sets `current_snapshot` on the pool entry
- If restoring a configuration, applies that configuration's tags to the device

#### GET /snapshot/list

```json
// Response
{
  "snapshots": [
    { "name": "clean-proxy", "created_at": "2026-02-19T...", "device_udid": "DF3B...", "device_name": "iPhone 15 Pro Max" },
    { "name": "geocaching-logged-in", "created_at": "2026-02-19T...", "device_udid": "DF3B...", "device_name": "iPhone 15 Pro Max" }
  ]
}
```

#### POST /snapshot/delete

```json
// Request
{ "udid": "DF3B...", "name": "clean-proxy" }

// Response
{ "status": "deleted", "name": "clean-proxy" }
```

### Tag Management

```
POST /api/v1/devices/tags/set
POST /api/v1/devices/tags/clear
```

#### POST /tags/set

Replace the tag list for a device (or add/remove individual tags).

```json
// Replace all tags
{ "udid": "DF3B...", "tags": ["proxy-ready", "geocaching"] }

// Add tags (without removing existing)
{ "udid": "DF3B...", "add_tags": ["logged-in"] }

// Remove specific tags
{ "udid": "DF3B...", "remove_tags": ["logged-in"] }
```

#### POST /tags/clear

```json
{ "udid": "DF3B..." }
```

### Resolution Protocol Updates

`resolve_device` and `ensure_devices` gain three new parameters:

| Parameter | Type | Behavior |
|-----------|------|----------|
| `tags` | `list[str]` | Only match devices that have ALL listed tags |
| `snapshot` | `str` | After resolving, restore this snapshot on the device |
| `config` | `str` | Shorthand: look up config → apply `tags` filter + `snapshot` restore |

When `config` is provided:
1. Look up the configuration in `~/.quern/config.json`
2. Use the config's `tags` as a filter (find devices that already have these tags)
3. After resolution, restore the config's `snapshot` on each device
4. Update each device's tags to match the config's tag list

When `snapshot` is provided without `config`:
1. Resolve normally (with any other criteria)
2. Restore the named snapshot on each resolved device
3. Tags are NOT automatically updated (user manages them separately)

When `tags` is provided without `snapshot` or `config`:
1. Only match devices that have all listed tags
2. No snapshot restore — assumes devices are already in the right state

Priority: `config` > `snapshot` + `tags` (if `config` is given, `snapshot` and `tags` are ignored).

---

## MCP Tools

### New Tools

```
save_snapshot     — Save simulator state as a named snapshot
restore_snapshot  — Restore a previously saved snapshot (or config name)
list_snapshots    — List snapshots saved on a device
delete_snapshot   — Delete a saved snapshot
set_device_tags   — Set, add, or remove tags on a device
```

### Updated Tools

`resolve_device`, `ensure_devices`, and `claim_device` get `tags`, `snapshot`, and `config` parameters.

### Tool Descriptions

```typescript
save_snapshot:
  "Save the current simulator state as a named snapshot. Use after
   manual setup (installing apps, logging in, configuring settings) to
   capture a reusable baseline. Optionally set tags to describe the state."

restore_snapshot:
  "Restore a simulator to a previously saved snapshot. Accepts a snapshot
   name or a configuration name from ~/.quern/config.json. Configurations
   bundle a snapshot with tags for easy test setup."

resolve_device:
  // Add to existing description:
  "... Optionally filter by tags (device must have ALL listed tags),
   restore a snapshot after resolution, or use a named config that
   combines both. Config names are defined in ~/.quern/config.json."

ensure_devices:
  // Add to existing description:
  "... Supports config= parameter to resolve N devices and restore
   a named snapshot on each, setting up parallel test execution in
   a known state. Example: ensure_devices(count=4, config='geocaching-logged-in')"
```

---

## SimctlBackend Changes

### New Methods

```python
async def snapshot_save(self, udid: str, name: str) -> None:
    """Save a snapshot: xcrun simctl snapshot save <udid> <name>"""

async def snapshot_restore(self, udid: str, name: str) -> None:
    """Restore a snapshot: xcrun simctl snapshot restore <udid> <name>"""

async def snapshot_list(self, udid: str) -> list[SnapshotInfo]:
    """List snapshots: xcrun simctl snapshot list <udid> --json"""

async def snapshot_delete(self, udid: str, name: str) -> None:
    """Delete a snapshot: xcrun simctl snapshot delete <udid> <name>"""
```

### Snapshot + Boot Interaction

- Restoring a snapshot requires the simulator to be **booted** (simctl enforces this)
- If a device is shutdown when snapshot restore is requested, boot it first
- After restore, the simulator remains booted in the snapshot's state
- Saving a snapshot also requires the simulator to be booted

---

## DevicePool Changes

### Tag Filtering in Resolution

```python
def _match_criteria(self, device, ..., tags=None):
    # ... existing checks ...
    if tags and not all(t in device.tags for t in tags):
        return False
    return True
```

### Config Resolution

```python
def _resolve_config(self, config_name: str) -> dict:
    """Look up a named configuration, returning snapshot + tags."""
    from server.config import read_user_config
    configs = read_user_config().get("configurations", {})
    if config_name not in configs:
        raise DeviceError(f"Configuration '{config_name}' not found in ~/.quern/config.json")
    return configs[config_name]
```

### Snapshot Restore in ensure_devices

```python
async def ensure_devices(self, ..., snapshot=None, config=None):
    # ... resolve devices as before ...

    # Apply snapshot restore
    if snapshot_name:
        for udid in selected_udids:
            await self.controller.simctl.snapshot_restore(udid, snapshot_name)
            # Update pool entry
            with self._lock_pool_file():
                state = self._read_state()
                device = state.devices[udid]
                device.current_snapshot = snapshot_name
                if config_tags is not None:
                    device.tags = config_tags
                self._write_state(state)

    return selected_udids
```

---

## Workflow Examples

### One-Time Setup: Create Base Snapshots

```bash
# 1. Boot simulators
quern ensure_devices count=4 os_version=17

# 2. On each simulator:
#    - Install proxy cert (via quern proxy tools)
#    - Disable password autofill in Settings
#    - Save snapshot
quern save_snapshot udid=<UDID1> name=clean-proxy tags=proxy-ready
quern save_snapshot udid=<UDID2> name=clean-proxy tags=proxy-ready
# ... repeat for each

# 3. Install Geocaching, log in, then save another snapshot
quern save_snapshot udid=<UDID1> name=geocaching-logged-in tags=geocaching,proxy-ready,logged-in
```

### Agent-Driven Setup

An AI agent could automate the one-time setup:

```
1. ensure_devices(count=4, os_version="17")
2. For each device:
   a. verify_proxy_setup() — cert already installed?
   b. If not: install cert, trust it, disable autofill via UI automation
   c. save_snapshot(name="clean-proxy", tags=["proxy-ready"])
3. For each device:
   a. restore_snapshot(name="clean-proxy")
   b. install_app(app_path="Geocaching.app")
   c. launch_app(bundle_id="com.groundspeak.GeocachingIntro")
   d. Automate login flow via tap_element / type_text
   e. save_snapshot(name="geocaching-logged-in", tags=["geocaching", "proxy-ready", "logged-in"])
```

### Test Run: Python Tests Using Quern API

```python
import httpx

QUERN = "http://127.0.0.1:9100"

class TestGeocachingLogout:
    @classmethod
    def setup_class(cls):
        """Ensure 4 simulators in geocaching-logged-in state."""
        resp = httpx.post(f"{QUERN}/api/v1/devices/ensure", json={
            "count": 4,
            "os_version": "17",
            "config": "geocaching-logged-in",
            "session_id": "logout-tests",
        })
        resp.raise_for_status()
        cls.devices = resp.json()["devices"]

    @classmethod
    def teardown_class(cls):
        """Release all devices."""
        for d in cls.devices:
            httpx.post(f"{QUERN}/api/v1/devices/release", json={
                "udid": d["udid"],
                "session_id": "logout-tests",
            })

    def test_logout_iphone_15(self):
        udid = self.devices[0]["udid"]
        # ... test logout flow on this device ...

    def test_logout_iphone_15_plus(self):
        udid = self.devices[1]["udid"]
        # ... test on a different device in parallel ...
```

### Quick Restore Between Tests

```python
def setup_method(self):
    """Restore snapshot before each test to get a clean logged-in state."""
    for d in self.devices:
        httpx.post(f"{QUERN}/api/v1/devices/snapshot/restore", json={
            "udid": d["udid"],
            "name": "geocaching-logged-in",
        })
```

### Base Snapshot for All Projects

The `"clean-proxy"` snapshot is project-agnostic — any test suite that needs proxy capture can use it as a starting point:

```python
# Ensure simulators have proxy cert + autofill disabled
resp = httpx.post(f"{QUERN}/api/v1/devices/ensure", json={
    "count": 2,
    "config": "clean-proxy",
    "session_id": "my-tests",
})
```

---

## Snapshot Lifecycle & Cleanup

### The Problem

Snapshots are large (1-5 GB each). A typical test run creates ephemeral snapshots — install a freshly compiled app, get to a test-ready state, snapshot it. After the test run, those snapshots are stale (the app binary came from a specific feature branch build). Without cleanup, disk usage grows unbounded.

But some snapshots are long-lived infrastructure — `"clean-proxy"` takes real effort to set up (cert trust, autofill disabled) and doesn't change between runs. These should never be cleaned.

### Safe Snapshots

The config file declares which snapshots are protected from cleanup:

```json
{
  "safe_snapshots": ["clean-proxy"],
  "configurations": {
    "clean-proxy": {
      "snapshot": "clean-proxy",
      "tags": ["proxy-ready"],
      "safe": true
    },
    "geocaching-logged-in": {
      "snapshot": "geocaching-logged-in",
      "tags": ["geocaching", "proxy-ready", "logged-in"],
      "safe": false
    }
  }
}
```

Two ways to mark a snapshot as safe:
1. **Top-level `safe_snapshots` list** — for snapshots not tied to a configuration
2. **`safe: true` on a configuration** — the config's snapshot name is automatically added to the safe set

These are merged at runtime: `safe_set = config["safe_snapshots"] + [c["snapshot"] for c in configs.values() if c.get("safe")]`

### Cleanup API

```
POST /api/v1/devices/snapshot/cleanup
```

```python
class SnapshotCleanupRequest(BaseModel):
    udid: str | None = None        # Specific device, or all if omitted
    session_id: str | None = None  # Only clean snapshots from this session's run
    dry_run: bool = False          # List what would be deleted without deleting
```

Response:

```json
{
  "status": "cleaned",
  "deleted": [
    { "device_udid": "DF3B...", "device_name": "iPhone 15 Pro Max", "snapshot": "geocaching-logged-in" },
    { "device_udid": "DF3B...", "device_name": "iPhone 15 Pro Max", "snapshot": "feature-123-test-state" }
  ],
  "kept_safe": [
    { "device_udid": "DF3B...", "device_name": "iPhone 15 Pro Max", "snapshot": "clean-proxy" }
  ],
  "freed_bytes": 8423211008
}
```

MCP tool: `cleanup_snapshots`

### When to Clean

Cleanup at **session start** (beginning of `ensure_devices`) is the right default for most workflows:

1. Previous test run may have crashed without teardown — stale snapshots linger
2. The new run will install a fresh app build anyway — old snapshots are useless
3. Cleaning at start means you always begin with a known-good disk state
4. No risk of cleaning snapshots that a parallel session is still using (session-owned devices are already claimed)

The flow when `ensure_devices` is called with `config` or `snapshot`:

```
ensure_devices(count=4, config="geocaching-logged-in", session_id="test-run-42")
  │
  ├─ 1. Resolve 4 matching devices (existing logic)
  ├─ 2. Claim devices for session (existing logic)
  ├─ 3. Clean non-safe snapshots on claimed devices ← NEW
  ├─ 4. Restore base snapshot (e.g. "clean-proxy")  ← existing
  └─ 5. Return device list
```

Step 3 only runs when a `snapshot` or `config` is provided — if you're just claiming devices without snapshot work, no cleanup happens.

Explicit cleanup is also available via the API/MCP tool for cases like:
- CI pipeline teardown step
- Manual disk space recovery
- Cleaning a specific device before handing it to another team

### Cleanup Scope

Cleanup deletes all non-safe snapshots on the target devices. It does **not**:
- Delete snapshots on devices not owned by the session (unless `udid` is explicit and no `session_id` is given)
- Delete safe snapshots under any circumstances
- Shut down or modify the device state — only snapshot files are removed

### Session-Scoped Ephemeral Snapshots

For test runs that create mid-run snapshots (e.g. "logged-in state for this specific build"), a naming convention makes cleanup predictable:

```python
# Test setup: create an ephemeral snapshot with a session-scoped name
snapshot_name = f"test-state-{session_id}"
save_snapshot(udid=device_udid, name=snapshot_name)

# ... run tests, restoring this snapshot between test methods ...

# Teardown: cleanup deletes it (not in safe list)
cleanup_snapshots(session_id=session_id)
```

Or skip explicit naming entirely — just let the next `ensure_devices` call clean up whatever the previous run left behind.

### Typical Workflow

```python
class TestGeocachingLogin:
    @classmethod
    def setup_class(cls):
        # This:
        # 1. Claims 4 simulators
        # 2. Cleans stale non-safe snapshots on them
        # 3. Restores "clean-proxy" snapshot on each
        # The test then installs a fresh build and does its thing
        resp = httpx.post(f"{QUERN}/api/v1/devices/ensure", json={
            "count": 4,
            "config": "clean-proxy",
            "session_id": "login-tests",
        })
        cls.devices = resp.json()["devices"]

        # Install fresh build on each device
        for d in cls.devices:
            httpx.post(f"{QUERN}/api/v1/device/app/install", json={
                "udid": d["udid"],
                "app_path": "/path/to/Geocaching-feature-branch.app",
            })

    @classmethod
    def teardown_class(cls):
        # Release devices — snapshots from this run get cleaned on next ensure_devices
        for d in cls.devices:
            httpx.post(f"{QUERN}/api/v1/devices/release", json={
                "udid": d["udid"],
                "session_id": "login-tests",
            })
```

No explicit snapshot cleanup needed — the next test run's `ensure_devices` handles it.

---

## Implementation Phases

### Phase A: Snapshot Primitives

- `SimctlBackend`: `snapshot_save`, `snapshot_restore`, `snapshot_list`, `snapshot_delete`
- API routes: `/snapshot/save`, `/snapshot/restore`, `/snapshot/list`, `/snapshot/delete`, `/snapshot/cleanup`
- MCP tools: `save_snapshot`, `restore_snapshot`, `list_snapshots`, `delete_snapshot`, `cleanup_snapshots`
- Config: `safe_snapshots` list, `safe` flag on configurations
- Pool entry: add `current_snapshot` field
- Tests: unit tests for simctl wrapper, API integration tests, cleanup safe-list tests

### Phase B: Device Tags

- Pool entry: add `tags` field
- API routes: `/tags/set`, `/tags/clear`
- MCP tool: `set_device_tags`
- Resolution: `_match_criteria` gains `tags` filter
- `resolve_device`, `ensure_devices`, `claim_device`: add `tags` parameter
- MCP tools: add `tags` parameter to resolution tools
- Tests: tag filtering, tag persistence across refresh

### Phase C: Named Configurations

- Config: add `configurations` section to `~/.quern/config.json`
- `_resolve_config()` in pool
- `resolve_device`, `ensure_devices`: add `config` parameter
- On resolution with `config`: filter by tags → resolve → restore snapshot → update tags
- MCP tools: add `config` parameter
- Tests: config lookup, end-to-end config resolution

### Phase D: Snapshot in Resolution Flow

- `resolve_device`: add `snapshot` parameter (restore after resolve)
- `ensure_devices`: add `snapshot` parameter (restore on all resolved devices)
- Auto-cleanup of non-safe snapshots on claimed devices when `snapshot` or `config` is provided
- Handle edge cases: snapshot doesn't exist on device, device is shutdown, partial failure rollback
- Tests: resolve-with-restore, ensure-with-restore, auto-cleanup on ensure, rollback on snapshot failure

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Snapshot doesn't exist on device | Error: "Snapshot 'X' not found on device Y. Available: ..." |
| Device is shutdown, restore requested | Auto-boot the device first, then restore |
| Config not found in config.json | Error: "Configuration 'X' not found in ~/.quern/config.json" |
| Save snapshot on physical device | Error: "Snapshots are only supported on simulators" |
| Restore during ensure_devices partially fails | Rollback: release newly claimed devices, but don't undo successful restores (they're harmless) |
| Snapshot name conflicts with config name | Config takes precedence in `restore_snapshot`. Use explicit `snapshot` parameter to bypass config lookup. |
| Tags filter matches no devices | Same diagnostic error pattern as existing resolution — "No device matching tags=['X', 'Y']" |
| Device tags out of sync with actual state | Tags are advisory. `save_snapshot` and `restore_snapshot` update them, but nothing prevents manual drift. A `verify_tags` tool could check actual state, but that's future scope. |
| Cleanup called while device is in use | Cleanup only targets devices owned by the session (or explicitly by UDID). Other sessions' devices are untouched. |
| Safe snapshot list is empty | All snapshots are eligible for cleanup. This is valid — user may not want any permanent snapshots. |
| Cleanup during ensure_devices fails | Non-fatal. Log a warning and continue with the restore. Stale snapshots waste disk but don't block tests. |

---

## Not in Scope

- **Physical device snapshots** — Not supported by Apple tooling. Physical devices use tags only.
- **Snapshot sharing across simulators** — Each simulator has its own snapshots. Use `simctl clone` if you need to duplicate a fully-configured simulator.
- **Automatic snapshot invalidation** — If you install an app update after saving a snapshot, the snapshot still has the old version. Re-save manually.
