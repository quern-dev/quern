# Phase 4b: Multi-Device Support - Implementation Plan

**Created:** February 13, 2026
**Status:** Planning
**Goal:** Enable parallel test execution across multiple simulators

---

## Strategic Rationale

### The Killer Feature: Parallel Test Execution

XCUITest's test sharding is notoriously flaky and difficult to configure. Quern can provide a vastly superior experience:

```bash
# Future 4c vision:
quern run test-suite.quern \
  --parallel 4 \
  --devices "iPhone 16 Pro" \
  --distribute smart

# Quern automatically:
# - Spins up 4 simulators
# - Shards tests intelligently
# - Runs in parallel
# - Aggregates results
# ✅ 10 minute test suite → 2.5 minutes
```

**Benefits over XCUITest:**
- ✅ Clean isolation (separate simulators, no state sharing)
- ✅ No flaky failures from concurrent access
- ✅ Simple configuration
- ✅ Works seamlessly with AI-driven tests

---

## Architecture Overview

### Core Concepts

1. **Device Pool** - Central registry of all available devices and their states
2. **Device Claiming** - Exclusive reservation of devices for test sessions
3. **Session Management** - Track which sessions own which devices
4. **Resolution Protocol** - Smart rules for selecting devices automatically

### State Model

```json
{
  "devices": {
    "AAAA-1111": {
      "name": "iPhone 16 Pro",
      "state": "booted",
      "claimed_by": null,
      "last_used": "2026-02-13T...",
      "os_version": "iOS 18.2"
    },
    "BBBB-2222": {
      "name": "iPhone 16 Pro",
      "state": "booted",
      "claimed_by": "session-abc123",
      "last_used": "2026-02-13T...",
      "os_version": "iOS 18.2"
    },
    "CCCC-3333": {
      "name": "iPhone 15",
      "state": "shutdown",
      "claimed_by": null,
      "last_used": "2026-02-12T...",
      "os_version": "iOS 17.5"
    }
  },
  "sessions": {
    "session-abc123": {
      "created_at": "2026-02-13T...",
      "claimed_devices": ["BBBB-2222"],
      "logs_cursor": "...",
      "flows_cursor": "..."
    }
  }
}
```

---

## Device Resolution Protocol

From roadmap + enhancements for parallel testing:

### Rules (Priority Order)

1. **Explicit UDID provided** → Use it (claim if available, error if claimed)
2. **One booted, unclaimed device** → Use silently
3. **Multiple unclaimed devices** → Take first matching criteria (or list and ask in interactive mode)
4. **No booted devices matching criteria** → Auto-boot if `auto_boot=True`, else error
5. **Physical device present** → Prefer physical over simulator (future)
6. **All matching devices claimed** → Wait if `wait_if_busy=True`, else error

### API Signature

```python
async def resolve_device(
    self,
    udid: str | None = None,           # Explicit device
    name: str | None = None,            # Device name pattern
    os_version: str | None = None,      # iOS version requirement
    auto_boot: bool = False,            # Boot if shutdown
    wait_if_busy: bool = False,         # Wait for available device
    wait_timeout: float = 30.0,         # Max wait time
    session_id: str | None = None,      # Claim for session
) -> str:
    """Resolve and optionally claim a device. Returns UDID."""
```

---

## Implementation Phases

### Phase 4b-alpha: Device Pool (1-2 days)

**Deliverables:**
- `DevicePool` class with state tracking
- Claim/release logic with exclusive locking
- State persistence (`~/.quern/device-pool.json`)
- API endpoints for pool management
- MCP tools for pool operations
- Unit tests

**Details:** See `phase4b-alpha-device-pool-spec.md`

### Phase 4b-beta: Session Management (1 day)

**Deliverables:**
- `TestSession` model and lifecycle
- Session creation/cleanup API
- Auto-release on session timeout (configurable, default 30 min)
- Session-scoped log/flow queries
- Session tracking in state file

**Key APIs:**
```python
POST /api/v1/sessions/create
  → {"session_id": "...", "expires_at": "..."}

DELETE /api/v1/sessions/{id}
  → Releases all claimed devices

GET /api/v1/logs/query?session_id=abc
  → Only logs from devices in this session
```

### Phase 4b-gamma: Resolution Protocol (1 day)

**Deliverables:**
- Implement resolution rules (priority order)
- Auto-boot integration (uses existing simctl backend)
- Wait-for-available with timeout
- Physical device preference (when Phase 3d is done)
- Device criteria matching (name, OS version, etc.)

**Key Logic:**
```python
# Example: Smart resolution
pool.resolve_device(
    name="iPhone 16 Pro",
    os_version="18.2",
    auto_boot=True,
    wait_if_busy=True,
    session_id="my-session"
)
# → Finds matching device, boots if needed, claims for session
```

### Phase 4b-delta: Testing & Polish (0.5 day)

**Deliverables:**
- Unit tests for pool logic (claim/release, race conditions)
- Integration tests with multiple simulators
- Documentation (architecture, API reference)
- MCP guide updates (new tools, best practices)
- Example scripts

**Test Scenarios:**
- Concurrent claims (ensure no double-booking)
- Release on crash/timeout
- Multiple sessions sharing pool
- Boot-on-demand flow

---

## API Surface

### New Endpoints

```
GET  /api/v1/devices/pool
  Query params: state (booted/shutdown/all), claimed (true/false/all)
  Response: List of devices with claim status

POST /api/v1/devices/claim
  Body: {udid?, name?, os_version?, auto_boot?, wait_if_busy?, session_id?}
  Response: {udid, name, state, claimed_at}

POST /api/v1/devices/release
  Body: {udid, session_id?}
  Response: {status: "released"}

POST /api/v1/devices/ensure
  Body: {count, name?, os_version?, auto_boot?}
  Response: {devices: [{udid, name, state}...]}

GET  /api/v1/sessions
  Response: List of active sessions

POST /api/v1/sessions/create
  Body: {device_count?, device_spec?}
  Response: {session_id, expires_at, claimed_devices}

DELETE /api/v1/sessions/{id}
  Response: {status: "cleaned_up", devices_released}
```

### Updated Endpoints

All existing device endpoints gain optional parameters:
- `session_id` - Associate action with session
- Device resolution now respects claimed state

```
# Examples:
GET /api/v1/device/ui?session_id=abc&udid=...
POST /api/v1/device/ui/tap?session_id=abc&udid=...
GET /api/v1/logs/query?session_id=abc
GET /api/v1/proxy/flows?session_id=abc
```

---

## MCP Tools

### New Tools

```typescript
list_device_pool()
  → See all devices and their claim status

claim_device({udid?, name?, auto_boot?, session_id?})
  → Reserve a device for exclusive use

release_device({udid, session_id?})
  → Return device to pool

ensure_devices({count, name?, os_version?, auto_boot?})
  → Boot N devices matching criteria, return UDIDs

create_session({device_count?, device_spec?})
  → Create session and claim devices

cleanup_session({session_id})
  → End session and release all devices
```

### Updated Tools

All existing device tools gain optional `session_id` parameter:
- `boot_device`
- `shutdown_device`
- `launch_app`
- `tap_element`
- `get_screen_summary`
- etc.

---

## State Persistence

### File: `~/.quern/device-pool.json`

```json
{
  "version": "1.0",
  "updated_at": "2026-02-13T...",
  "devices": {
    "UDID": {
      "name": "iPhone 16 Pro",
      "state": "booted",
      "claimed_by": "session-id or null",
      "claimed_at": "timestamp or null",
      "last_used": "timestamp",
      "os_version": "18.2",
      "device_type": "simulator"
    }
  },
  "sessions": {
    "session-id": {
      "created_at": "timestamp",
      "expires_at": "timestamp",
      "claimed_devices": ["UDID1", "UDID2"],
      "logs_cursor": "...",
      "flows_cursor": "..."
    }
  }
}
```

### Cleanup Strategy

- **Active sessions:** Check `expires_at`, auto-cleanup stale sessions
- **Orphaned claims:** If session doesn't exist, release the device
- **Startup check:** Verify all claimed PIDs still exist, release if not

---

## How This Enables 4c

With Phase 4b complete, Phase 4c (headless CLI runner) becomes straightforward:

```bash
quern run my-tests.quern --parallel 4 --devices "iPhone 16 Pro"
```

**Internal flow:**
1. Create session via `POST /sessions/create` with `device_count=4`
2. Session claims 4 matching devices (auto-boots if needed)
3. Shard tests across 4 devices
4. Run tests in parallel, each using their claimed device
5. Aggregate results (logs, flows, screenshots)
6. Cleanup session (releases all devices)

**Key benefits:**
- Clean isolation (no state sharing between parallel tests)
- Automatic resource management (claim/release)
- Session-scoped queries (each test sees only its own logs/flows)
- Crash recovery (expired sessions auto-release devices)

---

## Edge Cases & Considerations

### Race Conditions

**Problem:** Two clients try to claim the same device simultaneously

**Solution:** File locking on `device-pool.json` writes
```python
with file_lock("~/.quern/device-pool.json"):
    # Read state
    # Check if device available
    # Claim device
    # Write state
```

### Zombie Sessions

**Problem:** Session crashes, devices remain claimed forever

**Solution:**
- Sessions have `expires_at` timestamp (default 30 min)
- Background cleanup task checks for expired sessions every 60s
- `quern status` shows stale sessions
- `quern cleanup` forces release of expired sessions

### Device Crashes

**Problem:** Simulator crashes mid-test, claimed device is broken

**Solution:**
- Health check on claim: verify device is actually booted
- Release includes device health verification
- Option to `--force-release` if device is unresponsive

### Multiple Quern Servers

**Problem:** User runs multiple Quern servers (different projects)

**Solution:**
- State file includes server PID
- Device claims tied to server PID
- Startup check: if owning server not running, release the claim

---

## Testing Strategy

### Unit Tests

```python
# test_device_pool.py
def test_claim_available_device()
def test_claim_already_claimed_device_errors()
def test_release_device()
def test_release_unclaimed_device_errors()
def test_concurrent_claims_no_double_booking()
def test_session_expiry_releases_devices()
def test_orphaned_claim_cleanup()
```

### Integration Tests

```python
# test_multi_device_integration.py
async def test_parallel_tap_on_different_devices()
async def test_session_scoped_log_queries()
async def test_auto_boot_and_claim()
async def test_session_cleanup_releases_all()
```

### Manual Testing

```bash
# Boot 3 simulators
quern-debug-server start

# Terminal 1: Claim device 1
curl -X POST http://localhost:9100/api/v1/devices/claim \
  -d '{"name":"iPhone 16 Pro","session_id":"session1"}'

# Terminal 2: Claim device 2 (should get different UDID)
curl -X POST http://localhost:9100/api/v1/devices/claim \
  -d '{"name":"iPhone 16 Pro","session_id":"session2"}'

# Verify both claimed
curl http://localhost:9100/api/v1/devices/pool

# Release and verify
curl -X POST http://localhost:9100/api/v1/devices/release \
  -d '{"udid":"AAAA-1111","session_id":"session1"}'
```

---

## Timeline

**Phase 4b-alpha:** 1-2 days (Device Pool core)
**Phase 4b-beta:** 1 day (Session Management)
**Phase 4b-gamma:** 1 day (Resolution Protocol)
**Phase 4b-delta:** 0.5 day (Testing & Polish)

**Total: 3.5 - 4.5 days**

---

## Success Criteria

✅ Multiple simultaneous clients can claim different devices
✅ Claimed devices cannot be claimed by others
✅ Released devices immediately available for re-claiming
✅ Sessions auto-cleanup on expiry
✅ No race conditions (tested with concurrent claims)
✅ All 470+ existing tests still pass
✅ New integration tests for multi-device scenarios pass
✅ MCP tools work for claiming/releasing devices

---

## Future Enhancements (Post-4b)

- **Device pools by label** - Tag devices for different purposes ("fast", "slow", "nfc-capable")
- **Priority queuing** - High-priority sessions can preempt lower priority
- **Device warmup** - Keep N devices booted and ready (reduce boot time)
- **Health monitoring** - Track device responsiveness, auto-reboot unhealthy devices
- **Usage analytics** - Which devices are used most, average session duration
- **Physical device support** - Extend pool to include physical devices (Phase 3d)

---

## Related Documents

- `phase4b-alpha-device-pool-spec.md` - Detailed spec for 4b-alpha implementation
- `docs/phase3-architecture.md` - Device control foundation
- `docs/phase4a-architecture.md` - Process lifecycle (state.json pattern)
- `docs/quern-roadmap.md` - Overall project roadmap
