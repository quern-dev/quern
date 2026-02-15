# Phase 4b-gamma: Resolution Protocol - Implementation Spec

**Phase:** 4b-gamma
**Status:** Ready for Implementation
**Dependencies:** Phase 4b-alpha (Device Pool), Phase 4b-beta (Session Management)

---

## Overview

Implement the smart device resolution protocol that replaces the simple `DeviceController.resolve_udid()` with a pool-aware, criteria-matching, auto-booting resolution system. This is the intelligence layer that makes multi-device support practical — agents and test runners ask for "a device matching X" and the system figures out the rest.

---

## The Problem

Today there are **two parallel resolution systems** that don't talk to each other:

1. **`DeviceController.resolve_udid()`** (controller.py:68) — Simple 5-step resolution:
   - Explicit UDID → stored active → auto-detect single booted → error
   - Has no concept of claims, name matching, OS version, or auto-boot
   - Breaks when multiple devices are booted (errors with "Multiple simulators booted")

2. **`DevicePool.claim_device()`** (pool.py:63) — Claim-aware but dumb:
   - Matches by UDID or name substring
   - No OS version matching, no auto-boot, no wait-for-available
   - Separate API surface from the device operations

**Result:** An agent must manually orchestrate: list pool → pick device → claim it → pass UDID to every operation. The resolution protocol collapses this into a single smart call.

---

## Goals

1. **Unified resolution** — One method (`DevicePool.resolve_device()`) replaces both systems
2. **Criteria matching** — Select devices by name pattern, OS version, boot state
3. **Auto-boot** — Boot matching shutdown devices on demand
4. **Wait-for-available** — Block until a claimed device is released (with timeout)
5. **Ensure N devices** — Guarantee N devices matching criteria are booted and available
6. **Backwards compatible** — Existing `resolve_udid()` callers continue to work

---

## Architecture

### Resolution Flow

```
resolve_device(name="iPhone 16 Pro", os_version="18", auto_boot=True)
    │
    ├── 1. Refresh pool from simctl
    │
    ├── 2. Build candidate list (apply criteria filters)
    │       └── name match + os_version match + is_available
    │
    ├── 3. Narrow by availability
    │       ├── Booted + unclaimed → prefer these
    │       ├── Booted + claimed → skip (or wait if wait_if_busy)
    │       └── Shutdown + unclaimed → auto-boot candidates
    │
    ├── 4. Select best candidate
    │       ├── Prefer booted over shutdown
    │       ├── Prefer unclaimed over claimed
    │       ├── Prefer most recently used (warm caches)
    │       └── Break ties by name (alphabetical stability)
    │
    ├── 5. Boot if needed (auto_boot=True)
    │       └── simctl boot → poll until state=booted
    │
    ├── 6. Claim if session_id provided
    │
    └── 7. Return UDID
```

### Component Changes

```
DevicePool (enhanced)
  ├── resolve_device()         ← NEW: smart resolution
  ├── ensure_devices()         ← NEW: guarantee N devices
  ├── _match_criteria()        ← NEW: filter by name/OS
  ├── _wait_for_available()    ← NEW: poll for release
  ├── _boot_and_wait()         ← NEW: boot + poll for ready
  │
  ├── claim_device()           (existing, unchanged)
  ├── release_device()         (existing, unchanged)
  ├── list_devices()           (existing, unchanged)
  └── refresh_from_simctl()    (existing, unchanged)

DeviceController (modified)
  └── resolve_udid()           ← UPDATED: delegates to pool when available
```

---

## Implementation Details

### 1. Criteria Matching: `DevicePool._match_criteria()`

```python
def _match_criteria(
    self,
    device: DevicePoolEntry,
    name: str | None = None,
    os_version: str | None = None,
    device_type: DeviceType | None = None,
) -> bool:
    """Check if a device matches the given criteria.

    Matching rules:
    - name: Case-insensitive substring match
      "iPhone 16 Pro" matches "iPhone 16 Pro", "iPhone 16 Pro Max"
      "iphone 16" matches "iPhone 16", "iPhone 16 Plus", "iPhone 16 Pro"
    - os_version: Prefix match (dot-separated)
      "18" matches "iOS 18.0", "iOS 18.2", "iOS 18.6"
      "18.2" matches "iOS 18.2" only
      "17.5" matches "iOS 17.5"
    - device_type: Exact match (simulator or device)
    - Only matches is_available=True devices (not corrupted/unavailable)
    """
```

**OS version matching detail:** The `os_version` field in `DevicePoolEntry` stores the full string from simctl (e.g., `"iOS 18.2"`). The criteria matcher extracts the numeric portion and does prefix matching:

```python
def _os_version_matches(device_os: str, requested: str) -> bool:
    """Check if device OS version matches the requested version prefix.

    Examples:
        _os_version_matches("iOS 18.2", "18") → True
        _os_version_matches("iOS 18.2", "18.2") → True
        _os_version_matches("iOS 18.2", "18.6") → False
        _os_version_matches("iOS 17.5", "18") → False
    """
    # Extract numeric part: "iOS 18.2" → "18.2"
    import re
    device_nums = re.search(r"[\d.]+", device_os)
    if not device_nums:
        return False
    device_version = device_nums.group()
    # Prefix match on dot-separated components
    return device_version == requested or device_version.startswith(requested + ".")
```

### 2. Smart Resolution: `DevicePool.resolve_device()`

```python
async def resolve_device(
    self,
    udid: str | None = None,
    name: str | None = None,
    os_version: str | None = None,
    device_type: DeviceType | None = None,
    auto_boot: bool = False,
    wait_if_busy: bool = False,
    wait_timeout: float = 30.0,
    session_id: str | None = None,
) -> str:
    """Resolve a device matching criteria, optionally boot and/or claim it.

    Returns the UDID of the resolved device.

    Resolution priority:
    1. Explicit UDID → use directly (verify exists, check claim if session)
    2. Booted + unclaimed + matching criteria → use best match
    3. Booted + claimed + wait_if_busy → wait for release
    4. Shutdown + unclaimed + auto_boot → boot best match
    5. All matching claimed + wait_if_busy → wait for any release
    6. No matches → error with helpful message

    If session_id is provided, the resolved device is claimed for that session.
    """
```

**Priority rules in detail:**

| Priority | State | Claimed | Action | Condition |
|----------|-------|---------|--------|-----------|
| 1 | any | any | Use directly | Explicit UDID provided |
| 2 | booted | unclaimed | Use immediately | Default path |
| 3 | shutdown | unclaimed | Boot, then use | `auto_boot=True` |
| 4 | booted | claimed | Wait for release | `wait_if_busy=True` |
| 5 | shutdown | claimed | Wait + boot | `wait_if_busy=True` AND `auto_boot=True` |
| 6 | - | - | Error | No viable candidates |

**Tie-breaking when multiple candidates match:**

```python
def _rank_candidate(self, device: DevicePoolEntry) -> tuple:
    """Return sort key for candidate ranking (lower = better).

    Priority:
    1. Booted before shutdown (avoid boot cost)
    2. Unclaimed before claimed (available now)
    3. Most recently used (warm simulator state, caches)
    4. Name alphabetical (deterministic tie-breaking)
    """
    return (
        0 if device.state == DeviceState.BOOTED else 1,
        0 if device.claim_status == DeviceClaimStatus.AVAILABLE else 1,
        -device.last_used.timestamp(),  # Negative = prefer recent
        device.name,
    )
```

### 3. Auto-Boot: `DevicePool._boot_and_wait()`

```python
async def _boot_and_wait(
    self,
    udid: str,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> None:
    """Boot a device and wait for it to reach 'booted' state.

    Args:
        udid: Device to boot
        timeout: Max seconds to wait for boot
        poll_interval: Seconds between state checks

    Raises:
        DeviceError: If boot fails or times out
    """
    await self.controller.simctl.boot(udid)

    # Poll until booted or timeout
    start = time.time()
    while time.time() - start < timeout:
        await asyncio.sleep(poll_interval)

        # Refresh and check state
        devices = await self.controller.simctl.list_devices()
        for d in devices:
            if d.udid == udid and d.state == DeviceState.BOOTED:
                # Update pool state
                await self.refresh_from_simctl()
                logger.info("Device %s booted in %.1fs", udid[:8], time.time() - start)
                return

    raise DeviceError(
        f"Device {udid} did not boot within {timeout}s",
        tool="simctl",
    )
```

**Boot timeout:** Default 30s. Simulator cold boot is typically 5-15s; warm boot (previously booted runtime) is 2-5s. The 30s default handles slow machines with plenty of margin.

### 4. Wait-for-Available: `DevicePool._wait_for_available()`

```python
async def _wait_for_available(
    self,
    criteria: dict,
    timeout: float = 30.0,
    poll_interval: float = 1.0,
) -> DevicePoolEntry | None:
    """Wait for a matching device to become available.

    On each poll iteration, this method:
    1. Calls refresh_from_simctl() to pick up external state changes
       (new simulators booted via Xcode, devices appearing/disappearing)
    2. Re-reads the pool file to pick up claim state changes
       (another process releasing a device)

    This dual-check is important: a device becomes available either when
    someone releases it (pool file change) OR when someone boots a new
    matching simulator externally (simctl state change). Checking only
    the pool file would miss the second case.

    The 2-second cache on refresh_from_simctl() already prevents
    redundant subprocess calls, so calling it each iteration is cheap.

    Args:
        criteria: Dict of filter kwargs for _match_criteria()
                  (name, os_version, device_type)
        timeout: Max seconds to wait
        poll_interval: Seconds between checks

    Returns:
        The first matching unclaimed device found, or None on timeout
    """
    start = time.time()

    while True:
        remaining = timeout - (time.time() - start)
        if remaining <= 0:
            return None
        # Sleep for poll_interval or remaining time, whichever is shorter.
        # This ensures short timeouts are respected — if wait_timeout=0.5
        # with poll_interval=1.0, we sleep 0.5s, check once, and bail.
        await asyncio.sleep(min(poll_interval, remaining))

        # Refresh from simctl to detect externally-booted devices
        # (cached for 2s, so this is cheap on rapid iterations)
        await self.refresh_from_simctl()

        # Re-read state (picks up both simctl refresh AND external releases)
        state = self._read_state()
        for device in state.devices.values():
            if (
                device.claim_status == DeviceClaimStatus.AVAILABLE
                and device.state == DeviceState.BOOTED
                and self._match_criteria(device, **criteria)
            ):
                return device

    return None
```

**Why polling instead of events:** The pool state is file-based (shared across processes), so we can't use in-process events. Polling at 1s intervals is cheap and correct. The pool file is small (~2KB typically) so reads are sub-millisecond.

**Why refresh_from_simctl() on each iteration:** A device becomes available in two ways: (1) another process releases a claimed device (pool file change), or (2) someone boots a new simulator externally via Xcode or `xcrun simctl` (simctl state change). If we only watch the pool file, we miss case 2. The existing 2-second TTL cache on `refresh_from_simctl()` means we don't spawn a subprocess on every 1-second poll — at most every other iteration actually calls simctl.

### 5. Ensure Devices: `DevicePool.ensure_devices()`

```python
async def ensure_devices(
    self,
    count: int,
    name: str | None = None,
    os_version: str | None = None,
    device_type: DeviceType | None = None,
    auto_boot: bool = True,
    session_id: str | None = None,
) -> list[str]:
    """Ensure N devices matching criteria are booted and available.

    This is the primary entry point for parallel test execution setup.
    Finds available devices, boots additional ones if needed, and
    optionally claims them all for a session.

    Args:
        count: Number of devices needed
        name: Device name pattern to match
        os_version: OS version prefix to match
        device_type: Device type filter
        auto_boot: Boot shutdown devices if not enough are booted
        session_id: Claim all devices for this session

    Returns:
        List of UDIDs for the ready devices

    Raises:
        DeviceError: If not enough matching devices can be made available

    Strategy:
    1. Find all matching devices in the pool
    2. Separate into: booted+unclaimed, shutdown+unclaimed, claimed
    3. Use booted+unclaimed first (up to count)
    4. If still need more, boot shutdown+unclaimed devices
    5. If still not enough, error with helpful message
    6. If session_id, claim all selected devices
    """
```

**Example flow for `ensure_devices(count=4, name="iPhone 16 Pro")`:**

```
Pool state:
  iPhone 16 Pro (AAA) - booted, unclaimed     → select
  iPhone 16 Pro (BBB) - booted, claimed        → skip
  iPhone 16 Pro (CCC) - shutdown, unclaimed    → boot candidate
  iPhone 16 Pro (DDD) - shutdown, unclaimed    → boot candidate
  iPhone 16 Pro Max (EEE) - booted, unclaimed  → no match (name)

Result: Use AAA (already booted), boot CCC and DDD
Error: Only 3 available, need 4
```

### 6. Controller Integration: Update `DeviceController.resolve_udid()`

**This is the highest-risk change in the entire phase.** Every existing device operation — screenshot, tap, swipe, type, launch, install — flows through `resolve_udid()`. A regression here breaks everything. The implementation MUST follow the "silent upgrade" pattern: pool delegation is a performance/feature enhancement, not a hard dependency switch.

#### Design Principle: Pool Failure = Invisible Fallback

The pool is an optional enhancement. If `_pool` is `None` (not initialized), or if pool resolution raises an unexpected exception, the old logic must still work identically. No user should ever see a regression from this change.

```python
async def resolve_udid(self, udid: str | None = None) -> str:
    """Resolve which device to target.

    If a DevicePool is attached, attempts pool-based resolution for
    claim-aware, multi-device-friendly behavior. If pool resolution
    fails for any reason (pool not initialized, pool error, etc.),
    silently falls back to the original simple resolution logic.

    This ensures that:
    - Existing single-device workflows are completely unaffected
    - Pool initialization failures don't break device operations
    - Tests that don't set up a pool continue to pass unchanged

    Resolution order:
    1. Explicit udid → use, update active (same as before, no pool involved)
    2. Stored active_udid → use (same as before, no pool involved)
    3. Pool resolution (if pool attached) → best available booted device
    4. Fallback: simple auto-detect (original logic, unchanged)
       a. Exactly 1 booted → use, update active
       b. 0 booted → error "No booted simulator"
       c. 2+ booted → error "Multiple simulators booted, specify udid"
    """
    # Steps 1-2: unchanged, no pool involvement
    if udid:
        self._active_udid = udid
        return udid

    if self._active_udid:
        return self._active_udid

    # Step 3: try pool-based resolution (silent upgrade)
    if self._pool is not None:
        try:
            resolved = await self._pool.resolve_device()
            self._active_udid = resolved
            return resolved
        except Exception as e:
            # Pool resolution failed — fall through to original logic.
            # This catches: pool state file corruption, unexpected bugs,
            # simctl failures during refresh, etc.
            # Log at debug, not warning — this is expected during tests
            # and when pool is in a transitional state.
            logger.debug(
                "Pool resolution failed, falling back to simple resolution: %s", e
            )

    # Step 4: fallback — original logic, completely unchanged
    devices = await self.simctl.list_devices()
    booted = [d for d in devices if d.state == DeviceState.BOOTED]

    if len(booted) == 0:
        raise DeviceError("No booted simulator found", tool="simctl")
    if len(booted) > 1:
        names = ", ".join(f"{d.name} ({d.udid[:8]})" for d in booted)
        raise DeviceError(
            f"Multiple simulators booted ({names}), specify udid",
            tool="simctl",
        )

    self._active_udid = booted[0].udid
    return self._active_udid
```

#### Controller `__init__` change

```python
def __init__(self) -> None:
    self.simctl = SimctlBackend()
    self.idb = IdbBackend()
    self._active_udid: str | None = None
    self._pool = None  # Set by main.py after pool is created; None = no pool
    # ... existing cache fields ...
```

**Key:** `_pool` defaults to `None`. It is ONLY set externally by `server/main.py` after both controller and pool are successfully initialized. If pool initialization fails (e.g., file permission error), the controller continues to work with `_pool = None` — identical to today's behavior.

#### Wiring in `server/main.py`

```python
# In server/main.py lifespan:
controller = DeviceController()
pool = DevicePool(controller)
controller._pool = pool  # Enable pool-aware resolution

# If pool init fails, controller still works:
# controller = DeviceController()
# try:
#     pool = DevicePool(controller)
#     controller._pool = pool
# except Exception as e:
#     logger.warning("Device pool initialization failed: %s", e)
#     pool = None
#     # controller._pool remains None — old behavior preserved
```

#### Testing Requirements for Step 6

This step needs its own focused test coverage beyond the resolution protocol tests:

```python
class TestControllerPoolFallback:
    """Verify resolve_udid() fallback behavior when pool fails."""

    async def test_pool_none_uses_old_logic(self, controller):
        """When _pool is None, behave identically to pre-4b-gamma."""
        controller._pool = None
        # ... test single-booted auto-detect works ...

    async def test_pool_exception_falls_back_silently(self, controller, pool):
        """When pool.resolve_device() raises, fall back without crashing."""
        controller._pool = pool
        pool.resolve_device = AsyncMock(side_effect=Exception("pool is broken"))
        # Should NOT raise — should fall through to simple logic
        udid = await controller.resolve_udid()
        assert udid is not None

    async def test_pool_success_skips_fallback(self, controller, pool):
        """When pool resolves successfully, don't call simctl.list_devices."""
        controller._pool = pool
        pool.resolve_device = AsyncMock(return_value="AAAA")
        controller.simctl.list_devices = AsyncMock()

        udid = await controller.resolve_udid()
        assert udid == "AAAA"
        controller.simctl.list_devices.assert_not_called()  # Pool handled it

    async def test_explicit_udid_bypasses_pool(self, controller, pool):
        """Explicit UDID never touches the pool."""
        controller._pool = pool
        pool.resolve_device = AsyncMock()

        udid = await controller.resolve_udid(udid="XXXX")
        assert udid == "XXXX"
        pool.resolve_device.assert_not_called()

    async def test_active_udid_bypasses_pool(self, controller, pool):
        """Stored active UDID never touches the pool."""
        controller._pool = pool
        controller._active_udid = "YYYY"
        pool.resolve_device = AsyncMock()

        udid = await controller.resolve_udid()
        assert udid == "YYYY"
        pool.resolve_device.assert_not_called()
```

---

## New Data Models

### Request Models (add to `server/models.py`)

```python
class ResolveDeviceRequest(BaseModel):
    """Request body for POST /api/v1/devices/resolve."""
    udid: str | None = None
    name: str | None = None
    os_version: str | None = None
    auto_boot: bool = False
    wait_if_busy: bool = False
    wait_timeout: float = Field(default=30.0, ge=1.0, le=120.0)
    session_id: str | None = None


class EnsureDevicesRequest(BaseModel):
    """Request body for POST /api/v1/devices/ensure."""
    count: int = Field(ge=1, le=10)
    name: str | None = None
    os_version: str | None = None
    auto_boot: bool = True
    session_id: str | None = None
```

No new response models needed — responses use inline dicts matching the existing pool API style.

---

## API Endpoints

### New Endpoints (add to `server/api/device_pool.py`)

#### `POST /api/v1/devices/resolve`

Smart device resolution. The primary endpoint for agents that need a device.

```
POST /api/v1/devices/resolve
Body: {
    "name": "iPhone 16 Pro",     // optional: name pattern
    "os_version": "18",          // optional: OS version prefix
    "auto_boot": true,           // optional: boot if needed (default false)
    "wait_if_busy": false,       // optional: wait if all claimed (default false)
    "wait_timeout": 30,          // optional: max wait seconds (default 30)
    "session_id": "abc123"       // optional: claim for session
}

Response (200):
{
    "udid": "AAAA-1111-...",
    "name": "iPhone 16 Pro",
    "state": "booted",
    "os_version": "iOS 18.2",
    "claimed_by": "abc123",      // present if session_id was provided
    "was_booted": false,         // true if we had to boot it
    "waited_seconds": 0          // >0 if we waited for release
}

Errors:
- 404: No device matching criteria found
- 409: Device claimed and wait_if_busy=false
- 408: Timed out waiting for available device
- 400: Invalid criteria combination
```

#### `POST /api/v1/devices/ensure`

Ensure N devices are booted and ready. Critical for parallel test setup.

```
POST /api/v1/devices/ensure
Body: {
    "count": 4,                  // required: number of devices needed
    "name": "iPhone 16 Pro",     // optional: name pattern
    "os_version": "18",          // optional: OS version prefix
    "auto_boot": true,           // optional: boot if needed (default true)
    "session_id": "test-run-1"   // optional: claim all for session
}

Response (200):
{
    "devices": [
        {"udid": "AAAA-...", "name": "iPhone 16 Pro", "state": "booted", "was_booted": false},
        {"udid": "BBBB-...", "name": "iPhone 16 Pro", "state": "booted", "was_booted": false},
        {"udid": "CCCC-...", "name": "iPhone 16 Pro", "state": "booted", "was_booted": true},
        {"udid": "DDDD-...", "name": "iPhone 16 Pro", "state": "booted", "was_booted": true}
    ],
    "total_available": 4,
    "total_booted": 2,           // how many were already booted
    "total_newly_booted": 2,     // how many we had to boot
    "session_id": "test-run-1"   // echoed if provided
}

Errors:
- 400: count < 1 or count > 10
- 404: Not enough matching devices exist (e.g., only 3 "iPhone 16 Pro" simulators)
- 409: Some matching devices are claimed (and not enough unclaimed to satisfy count)
- 503: Boot failed for one or more devices
```

### Updated Endpoints

#### `POST /api/v1/devices/claim` (enhanced)

Add `os_version` parameter to existing claim endpoint for criteria-based claiming:

```
POST /api/v1/devices/claim
Body: {
    "session_id": "abc123",
    "udid": null,                  // explicit device (takes precedence)
    "name": "iPhone 16 Pro",       // name pattern
    "os_version": "18"             // NEW: OS version prefix
}
```

---

## MCP Tools

### New Tools

#### `resolve_device`

```typescript
server.tool(
  "resolve_device",
  `Smartly find and optionally claim a device matching criteria. This is the
preferred way to get a device — it handles booting, waiting, and claiming
automatically. Use this instead of manually listing the pool and claiming.`,
  {
    name: z.string().optional()
      .describe("Device name pattern (e.g., 'iPhone 16 Pro')"),
    os_version: z.string().optional()
      .describe("OS version prefix (e.g., '18' matches 18.x)"),
    auto_boot: z.boolean().optional().default(false)
      .describe("Boot a matching shutdown device if no booted ones available"),
    wait_if_busy: z.boolean().optional().default(false)
      .describe("Wait for a claimed device to be released"),
    wait_timeout: z.number().optional().default(30)
      .describe("Max seconds to wait if wait_if_busy is true"),
    session_id: z.string().optional()
      .describe("Claim the device for this session"),
  },
  async (params) => { /* POST /api/v1/devices/resolve */ }
);
```

#### `ensure_devices`

```typescript
server.tool(
  "ensure_devices",
  `Ensure N devices matching criteria are booted and ready. Use this to set up
parallel test execution — it finds available devices, boots more if needed,
and optionally claims them all for a session.`,
  {
    count: z.number().min(1).max(10)
      .describe("Number of devices needed"),
    name: z.string().optional()
      .describe("Device name pattern (e.g., 'iPhone 16 Pro')"),
    os_version: z.string().optional()
      .describe("OS version prefix (e.g., '18' matches 18.x)"),
    auto_boot: z.boolean().optional().default(true)
      .describe("Boot shutdown devices if not enough booted ones"),
    session_id: z.string().optional()
      .describe("Claim all devices for this session"),
  },
  async (params) => { /* POST /api/v1/devices/ensure */ }
);
```

### Updated Tools

No existing MCP tools need changes. The new `resolve_device` tool is additive — agents can use it or continue using `claim_device` directly.

---

## Error Messages

Resolution errors must be **diagnostic**, not just actionable. When someone's criteria are slightly off — wrong OS version, name typo, unexpected claim state — the error should explain *what was found and why it didn't match*. This saves a huge amount of debugging time.

### Error construction pattern

The `resolve_device()` method should build a diagnostic context as it filters candidates, then use that context to compose the error:

```python
def _build_resolution_error(
    self,
    criteria: dict,
    all_devices: list[DevicePoolEntry],
) -> DeviceError:
    """Build a diagnostic error explaining why resolution failed.

    Analyzes all devices against each criterion independently to show
    the user exactly which criteria eliminated which devices.
    """
    name = criteria.get("name")
    os_version = criteria.get("os_version")

    # Count what matched each criterion independently
    name_matched = [d for d in all_devices if not name or name.lower() in d.name.lower()]
    os_matched = [d for d in all_devices if not os_version or self._os_version_matches(d.os_version, os_version)]
    both_matched = [d for d in all_devices if self._match_criteria(d, **criteria)]

    # Build the criteria description
    criteria_parts = []
    if name:
        criteria_parts.append(f"name='{name}'")
    if os_version:
        criteria_parts.append(f"os_version='{os_version}'")
    criteria_str = ", ".join(criteria_parts) if criteria_parts else "no criteria"

    if not both_matched:
        # Nothing matched all criteria — explain what partially matched
        parts = [f"No device matching {criteria_str}."]

        if name and os_version:
            # Show cross-match diagnostics
            name_only = [d for d in name_matched if d not in os_matched]
            os_only = [d for d in os_matched if d not in name_matched]

            if name_only:
                versions = set(d.os_version for d in name_only)
                parts.append(
                    f"{len(name_only)} matched name but were {', '.join(sorted(versions))}"
                )
            if os_only:
                names = set(d.name for d in os_only)
                parts.append(
                    f"{len(os_only)} matched OS but were {', '.join(sorted(names))}"
                )
            if not name_only and not os_only:
                parts.append(f"No devices matched either criterion.")
        elif name and not name_matched:
            available_names = sorted(set(d.name for d in all_devices))
            parts.append(f"Available device names: {', '.join(available_names)}")
        elif os_version and not os_matched:
            available_versions = sorted(set(d.os_version for d in all_devices))
            parts.append(f"Available OS versions: {', '.join(available_versions)}")

        parts.append(
            f"Pool has {len(all_devices)} total devices."
        )
        return DeviceError(" ".join(parts), tool="pool")

    # Devices matched but none are usable (all claimed, all shutdown, etc.)
    # ... (see specific cases below)
```

### Example error messages

```python
# No matching devices — name+OS cross-mismatch (the most confusing case)
"No device matching name='iPhone 16 Pro', os_version='18'. "
"2 matched name but were iOS 17.5. 1 matched OS but was iPhone 15. "
"Pool has 5 total devices."

# No matching devices — name typo
"No device matching name='iPad Pro'. "
"Available device names: iPhone 15, iPhone 16 Pro. "
"Pool has 5 total devices."

# No matching devices — wrong OS version
"No device matching os_version='19'. "
"Available OS versions: iOS 17.5, iOS 18.2. "
"Pool has 5 total devices."

# Matches exist but all are claimed
"All 2 devices matching name='iPhone 16 Pro' are claimed. "
"Use wait_if_busy=true to wait, or release a device first. "
"Claimed by: session-abc (iPhone 16 Pro, AAAA), session-def (iPhone 16 Pro, BBBB)"

# Matches exist but all are shutdown and auto_boot is false
"Found 2 matching devices but all are shutdown: "
"iPhone 16 Pro (CCCC), iPhone 16 Pro (DDDD). "
"Use auto_boot=true to boot one, or boot manually with boot_device."

# Timeout waiting for available device
"Timed out after 30.0s waiting for a device matching name='iPhone 16 Pro' "
"to become available. 2 matching devices are claimed by: session-abc, session-def"

# ensure_devices: not enough matching devices
"Need 4 devices matching name='iPhone 16 Pro' but only 3 exist "
"(2 booted, 1 shutdown, 0 available). "
"Create more simulators with: xcrun simctl create 'iPhone 16 Pro' ..."

# Boot failed
"Failed to boot device AAAA-1111 (iPhone 16 Pro): simctl boot timed out after 30s. "
"The simulator may be corrupted — try: xcrun simctl delete AAAA-1111 && xcrun simctl create ..."
```

### Testing error messages

Add specific assertions on error message content. These are part of the public contract — agents parse these messages for next-step decisions:

```python
async def test_error_shows_partial_matches(self, pool_with_devices):
    """Error for cross-criteria mismatch should list what partially matched."""
    await pool_with_devices.refresh_from_simctl()
    with pytest.raises(DeviceError) as exc_info:
        await pool_with_devices.resolve_device(name="iPhone 16 Pro", os_version="17")

    msg = str(exc_info.value)
    # Should explain that name matched but OS didn't
    assert "matched name" in msg
    assert "iOS 17.5" in msg or "17.5" in msg

async def test_error_lists_available_names(self, pool_with_devices):
    """Error for name mismatch should list available device names."""
    await pool_with_devices.refresh_from_simctl()
    with pytest.raises(DeviceError) as exc_info:
        await pool_with_devices.resolve_device(name="iPad Pro")

    msg = str(exc_info.value)
    assert "iPhone 16 Pro" in msg
    assert "iPhone 15" in msg
```

---

## File Changes

### Modified Files

| File | Changes |
|------|---------|
| `server/device/pool.py` | Add `resolve_device()`, `ensure_devices()`, `_match_criteria()`, `_boot_and_wait()`, `_wait_for_available()`, `_rank_candidate()`, `_os_version_matches()`, `_build_resolution_error()` |
| `server/device/controller.py` | Add `_pool = None` attribute, update `resolve_udid()` with try/except fallback to pool |
| `server/api/device_pool.py` | Add `POST /resolve` and `POST /ensure` endpoints, add `os_version` to claim |
| `server/models.py` | Add `ResolveDeviceRequest`, `EnsureDevicesRequest` |
| `server/main.py` | Wire `controller._pool = pool` after both are created |
| `mcp/src/index.ts` | Add `resolve_device` and `ensure_devices` tools |

### New Files

| File | Purpose |
|------|---------|
| `tests/test_resolution_protocol.py` | Unit tests for resolution logic |
| `tests/test_resolution_api.py` | API endpoint tests |

---

## Testing Strategy

### Unit Tests (`tests/test_resolution_protocol.py`)

All tests use mocked simctl — no real subprocess calls.

```python
"""Tests for the device resolution protocol (Phase 4b-gamma)."""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from server.device.pool import DevicePool
from server.models import (
    DeviceClaimStatus, DeviceError, DeviceInfo,
    DevicePoolEntry, DeviceState, DeviceType,
)


# Fixture: pool with predictable device set
@pytest.fixture
def pool_with_devices(tmp_path, mock_controller):
    """Pool pre-loaded with a realistic device set."""
    pool = DevicePool(mock_controller)
    pool._pool_file = tmp_path / "device-pool.json"

    # Mock controller.list_devices to return predictable set
    mock_controller.list_devices = AsyncMock(return_value=[
        DeviceInfo(udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                   os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
        DeviceInfo(udid="BBBB", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                   os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
        DeviceInfo(udid="CCCC", name="iPhone 16 Pro", state=DeviceState.SHUTDOWN,
                   os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
        DeviceInfo(udid="DDDD", name="iPhone 15", state=DeviceState.BOOTED,
                   os_version="iOS 17.5", runtime="...iOS-17-5", is_available=True),
        DeviceInfo(udid="EEEE", name="iPhone 15", state=DeviceState.SHUTDOWN,
                   os_version="iOS 17.5", runtime="...iOS-17-5", is_available=True),
    ])
    return pool


class TestCriteriaMatching:
    """Test _match_criteria() filtering logic."""

    async def test_match_by_name_substring(self, pool_with_devices):
        await pool_with_devices.refresh_from_simctl()
        state = pool_with_devices._read_state()
        device = state.devices["AAAA"]
        assert pool_with_devices._match_criteria(device, name="iPhone 16 Pro")
        assert pool_with_devices._match_criteria(device, name="iPhone 16")
        assert pool_with_devices._match_criteria(device, name="iphone 16")  # case insensitive
        assert not pool_with_devices._match_criteria(device, name="iPhone 15")

    async def test_match_by_os_version_prefix(self, pool_with_devices):
        await pool_with_devices.refresh_from_simctl()
        state = pool_with_devices._read_state()
        device = state.devices["AAAA"]  # iOS 18.2
        assert pool_with_devices._match_criteria(device, os_version="18")
        assert pool_with_devices._match_criteria(device, os_version="18.2")
        assert not pool_with_devices._match_criteria(device, os_version="18.6")
        assert not pool_with_devices._match_criteria(device, os_version="17")

    async def test_match_combined_criteria(self, pool_with_devices):
        await pool_with_devices.refresh_from_simctl()
        state = pool_with_devices._read_state()
        device = state.devices["AAAA"]
        assert pool_with_devices._match_criteria(device, name="iPhone 16", os_version="18")
        assert not pool_with_devices._match_criteria(device, name="iPhone 16", os_version="17")

    async def test_no_criteria_matches_all(self, pool_with_devices):
        await pool_with_devices.refresh_from_simctl()
        state = pool_with_devices._read_state()
        for device in state.devices.values():
            assert pool_with_devices._match_criteria(device)


class TestResolveDevice:
    """Test resolve_device() smart resolution."""

    async def test_explicit_udid(self, pool_with_devices):
        """Explicit UDID bypasses all criteria matching."""
        await pool_with_devices.refresh_from_simctl()
        udid = await pool_with_devices.resolve_device(udid="AAAA")
        assert udid == "AAAA"

    async def test_explicit_udid_not_found(self, pool_with_devices):
        """Error when explicit UDID doesn't exist."""
        await pool_with_devices.refresh_from_simctl()
        with pytest.raises(DeviceError, match="not found"):
            await pool_with_devices.resolve_device(udid="ZZZZ")

    async def test_prefer_booted_unclaimed(self, pool_with_devices):
        """Should pick a booted, unclaimed device over shutdown ones."""
        await pool_with_devices.refresh_from_simctl()
        udid = await pool_with_devices.resolve_device(name="iPhone 16 Pro")
        # AAAA and BBBB are both booted — should get one of them
        assert udid in ("AAAA", "BBBB")

    async def test_skip_claimed_devices(self, pool_with_devices):
        """Should skip claimed devices and pick unclaimed ones."""
        await pool_with_devices.refresh_from_simctl()
        # Claim AAAA
        await pool_with_devices.claim_device(session_id="other", udid="AAAA")

        udid = await pool_with_devices.resolve_device(name="iPhone 16 Pro")
        assert udid == "BBBB"  # AAAA claimed, so should get BBBB

    async def test_auto_boot_when_no_booted(self, pool_with_devices):
        """Should boot a shutdown device when auto_boot=True and no booted ones."""
        await pool_with_devices.refresh_from_simctl()

        # Claim both booted iPhone 16 Pro devices
        await pool_with_devices.claim_device(session_id="s1", udid="AAAA")
        await pool_with_devices.claim_device(session_id="s2", udid="BBBB")

        # Mock boot to succeed immediately
        pool_with_devices.controller.simctl.boot = AsyncMock()
        # After boot, simctl returns CCCC as booted
        pool_with_devices.controller.simctl.list_devices = AsyncMock(return_value=[
            DeviceInfo(udid="CCCC", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                       os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
        ])

        udid = await pool_with_devices.resolve_device(
            name="iPhone 16 Pro", auto_boot=True
        )
        assert udid == "CCCC"
        pool_with_devices.controller.simctl.boot.assert_called_once_with("CCCC")

    async def test_error_when_all_claimed_no_wait(self, pool_with_devices):
        """Should error when all matching devices are claimed and wait_if_busy=False."""
        await pool_with_devices.refresh_from_simctl()
        await pool_with_devices.claim_device(session_id="s1", udid="AAAA")
        await pool_with_devices.claim_device(session_id="s2", udid="BBBB")

        with pytest.raises(DeviceError, match="claimed"):
            await pool_with_devices.resolve_device(
                name="iPhone 16 Pro",
                auto_boot=False,
                wait_if_busy=False,
            )

    async def test_os_version_filtering(self, pool_with_devices):
        """Should only return devices matching OS version prefix."""
        await pool_with_devices.refresh_from_simctl()
        udid = await pool_with_devices.resolve_device(os_version="17")
        assert udid == "DDDD"  # Only iPhone 15 has iOS 17.x

    async def test_claim_on_resolve(self, pool_with_devices):
        """Should claim device when session_id is provided."""
        await pool_with_devices.refresh_from_simctl()
        udid = await pool_with_devices.resolve_device(
            name="iPhone 16 Pro",
            session_id="my-session",
        )
        state = await pool_with_devices.get_device_state(udid)
        assert state.claimed_by == "my-session"

    async def test_no_matching_devices(self, pool_with_devices):
        """Should error with helpful message when no devices match."""
        await pool_with_devices.refresh_from_simctl()
        with pytest.raises(DeviceError, match="No simulator found"):
            await pool_with_devices.resolve_device(name="iPad Pro")

    async def test_wait_for_available_on_release(self, pool_with_devices):
        """Should wait and succeed when a claimed device is released."""
        await pool_with_devices.refresh_from_simctl()

        # Claim both booted devices
        await pool_with_devices.claim_device(session_id="s1", udid="AAAA")
        await pool_with_devices.claim_device(session_id="s2", udid="BBBB")

        # Release AAAA after a short delay (simulate another process)
        async def delayed_release():
            await asyncio.sleep(0.5)
            await pool_with_devices.release_device(udid="AAAA", session_id="s1")

        release_task = asyncio.create_task(delayed_release())

        udid = await pool_with_devices.resolve_device(
            name="iPhone 16 Pro",
            wait_if_busy=True,
            wait_timeout=5.0,
            auto_boot=False,
        )
        assert udid == "AAAA"
        await release_task

    async def test_wait_detects_externally_booted_device(self, pool_with_devices):
        """Wait loop should detect devices booted outside of Quern via simctl refresh.

        This tests the critical behavior: _wait_for_available() calls
        refresh_from_simctl() on each iteration, not just reads the pool file.
        A new simulator booted via Xcode or 'xcrun simctl boot' should be
        detected and returned.
        """
        await pool_with_devices.refresh_from_simctl()

        # Claim all existing iPhone 16 Pro devices
        await pool_with_devices.claim_device(session_id="s1", udid="AAAA")
        await pool_with_devices.claim_device(session_id="s2", udid="BBBB")
        # CCCC is shutdown — and we're not using auto_boot

        # After 0.5s, simulate an external simctl boot of a new device
        # by changing what controller.list_devices returns
        original_list = pool_with_devices.controller.list_devices

        async def delayed_external_boot():
            await asyncio.sleep(0.5)
            # Simulate: someone booted a new iPhone 16 Pro externally
            pool_with_devices.controller.list_devices = AsyncMock(return_value=[
                DeviceInfo(udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                           os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
                DeviceInfo(udid="BBBB", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                           os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
                DeviceInfo(udid="CCCC", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                           os_version="iOS 18.2", runtime="...iOS-18-2", is_available=True),
                # ... other devices ...
            ])
            # Force cache expiry so next refresh_from_simctl() actually calls simctl
            pool_with_devices._last_refresh_at = None

        boot_task = asyncio.create_task(delayed_external_boot())

        udid = await pool_with_devices.resolve_device(
            name="iPhone 16 Pro",
            wait_if_busy=True,
            wait_timeout=5.0,
            auto_boot=False,  # NOT using auto_boot — device was booted externally
        )
        # Should find CCCC (now booted and unclaimed) via simctl refresh
        assert udid == "CCCC"
        await boot_task

    async def test_wait_respects_short_timeout(self, pool_with_devices):
        """Wait with timeout shorter than poll_interval should still check once.

        If wait_timeout=0.5 and poll_interval=1.0, the loop should sleep 0.5s
        (not a full 1.0s), check once, then bail. This tests the
        min(poll_interval, remaining) logic in _wait_for_available().
        """
        await pool_with_devices.refresh_from_simctl()

        # Claim both booted iPhone 16 Pro devices — nothing available
        await pool_with_devices.claim_device(session_id="s1", udid="AAAA")
        await pool_with_devices.claim_device(session_id="s2", udid="BBBB")

        start = time.time()
        with pytest.raises(DeviceError, match="claimed"):
            await pool_with_devices.resolve_device(
                name="iPhone 16 Pro",
                wait_if_busy=True,
                wait_timeout=0.5,
                auto_boot=False,
            )
        elapsed = time.time() - start

        # Should have taken ~0.5s, NOT 1.0s (the default poll interval)
        assert elapsed < 0.9, f"Expected ~0.5s timeout, took {elapsed:.1f}s"


class TestEnsureDevices:
    """Test ensure_devices() bulk provisioning."""

    async def test_enough_booted_devices(self, pool_with_devices):
        """Should return already-booted devices without booting more."""
        await pool_with_devices.refresh_from_simctl()

        udids = await pool_with_devices.ensure_devices(
            count=2, name="iPhone 16 Pro"
        )
        assert len(udids) == 2
        assert set(udids) == {"AAAA", "BBBB"}

    async def test_boot_additional_devices(self, pool_with_devices):
        """Should boot shutdown devices to meet the count."""
        await pool_with_devices.refresh_from_simctl()

        # Mock boot
        pool_with_devices.controller.simctl.boot = AsyncMock()

        udids = await pool_with_devices.ensure_devices(
            count=3, name="iPhone 16 Pro", auto_boot=True
        )
        assert len(udids) == 3
        assert "CCCC" in udids  # shutdown device was booted
        pool_with_devices.controller.simctl.boot.assert_called_once_with("CCCC")

    async def test_not_enough_devices_error(self, pool_with_devices):
        """Should error when not enough matching devices exist."""
        await pool_with_devices.refresh_from_simctl()
        with pytest.raises(DeviceError, match="Need 5"):
            await pool_with_devices.ensure_devices(
                count=5, name="iPhone 16 Pro"
            )

    async def test_ensure_with_session_claims_all(self, pool_with_devices):
        """Should claim all ensured devices when session_id provided."""
        await pool_with_devices.refresh_from_simctl()

        udids = await pool_with_devices.ensure_devices(
            count=2, name="iPhone 16 Pro", session_id="test-run"
        )
        for udid in udids:
            state = await pool_with_devices.get_device_state(udid)
            assert state.claimed_by == "test-run"

    async def test_ensure_skips_claimed_devices(self, pool_with_devices):
        """Should not include claimed devices in the result."""
        await pool_with_devices.refresh_from_simctl()
        await pool_with_devices.claim_device(session_id="other", udid="AAAA")

        udids = await pool_with_devices.ensure_devices(
            count=2, name="iPhone 16 Pro", auto_boot=True
        )
        assert "AAAA" not in udids
        assert len(udids) == 2  # BBBB (booted) + CCCC (auto-booted)


class TestControllerIntegration:
    """Test that DeviceController.resolve_udid() delegates to pool."""

    async def test_resolve_uses_pool_when_available(self, pool_with_devices):
        """Controller should use pool resolution when pool is set."""
        controller = pool_with_devices.controller
        controller._pool = pool_with_devices
        await pool_with_devices.refresh_from_simctl()

        udid = await controller.resolve_udid()
        # Should resolve to a booted device without error
        assert udid in ("AAAA", "BBBB", "DDDD")  # any booted device

    async def test_resolve_explicit_udid_still_works(self, pool_with_devices):
        """Explicit UDID should bypass pool resolution."""
        controller = pool_with_devices.controller
        controller._pool = pool_with_devices

        udid = await controller.resolve_udid(udid="DDDD")
        assert udid == "DDDD"
```

### API Tests (`tests/test_resolution_api.py`)

```python
"""API tests for resolution protocol endpoints."""

class TestResolveEndpoint:
    async def test_resolve_by_name(self, app, auth_headers):
        """POST /devices/resolve with name returns a matching UDID."""

    async def test_resolve_with_auto_boot(self, app, auth_headers):
        """POST /devices/resolve with auto_boot boots a shutdown device."""

    async def test_resolve_timeout_returns_408(self, app, auth_headers):
        """POST /devices/resolve with expired wait returns 408."""

    async def test_resolve_no_match_returns_404(self, app, auth_headers):
        """POST /devices/resolve with impossible criteria returns 404."""


class TestEnsureEndpoint:
    async def test_ensure_enough_booted(self, app, auth_headers):
        """POST /devices/ensure with available devices returns immediately."""

    async def test_ensure_boots_additional(self, app, auth_headers):
        """POST /devices/ensure boots shutdown devices to meet count."""

    async def test_ensure_not_enough_returns_error(self, app, auth_headers):
        """POST /devices/ensure with impossible count returns 404."""

    async def test_ensure_with_session_claims(self, app, auth_headers):
        """POST /devices/ensure with session_id claims all devices."""
```

---

## Implementation Order

Execute in this order to maintain a working system at each step:

### Step 1: Criteria matching helpers (~30 min)

Add to `server/device/pool.py`:
- `_os_version_matches()` static method
- `_match_criteria()` method
- `_rank_candidate()` method

These are pure functions with no side effects — easy to test in isolation.

### Step 2: Boot and wait helpers (~30 min)

Add to `server/device/pool.py`:
- `_boot_and_wait()` — boot a device and poll until ready
- `_wait_for_available()` — poll pool state for unclaimed device

These need `import time` (already present) and `import asyncio` (already present).

### Step 3: `resolve_device()` method (~1 hour)

Add the main resolution method to `DevicePool`. This composes the helpers from steps 1-2. Test thoroughly before moving to the API layer.

### Step 4: `ensure_devices()` method (~30 min)

Add bulk provisioning. Builds on `resolve_device()` and `_boot_and_wait()`.

### Step 5: Request models and API endpoints (~30 min)

- Add `ResolveDeviceRequest` and `EnsureDevicesRequest` to `server/models.py`
- Add `POST /resolve` and `POST /ensure` to `server/api/device_pool.py`
- Add `os_version` to `ClaimDeviceRequest`

### Step 6: Controller integration (~45 min) *** HIGHEST RISK ***

This is the riskiest step — every device operation flows through `resolve_udid()`. Take extra care:

- Add `_pool = None` attribute to `DeviceController.__init__()`
- Update `resolve_udid()` with try/except fallback pattern (pool failure → silent fallback to old logic)
- Wire `controller._pool = pool` in `server/main.py`
- Write focused fallback tests BEFORE modifying `resolve_udid()`:
  - `test_pool_none_uses_old_logic` — verify pre-4b-gamma behavior is identical
  - `test_pool_exception_falls_back_silently` — pool crash doesn't break anything
  - `test_pool_success_skips_fallback` — pool resolution prevents redundant simctl call
  - `test_explicit_udid_bypasses_pool` — explicit UDID never touches pool
  - `test_active_udid_bypasses_pool` — stored active UDID never touches pool
- Run the FULL existing test suite after this step before moving on

### Step 7: MCP tools (~30 min)

- Add `resolve_device` and `ensure_devices` tools to `mcp/src/index.ts`

### Step 8: Tests (~1 hour)

- `tests/test_resolution_protocol.py` — unit tests (criteria, resolve, ensure)
- `tests/test_resolution_api.py` — API endpoint tests
- Run full suite to verify no regressions

---

## Edge Cases & Considerations

### Race between resolve and claim

Two agents call `resolve_device(name="iPhone 16 Pro", session_id=...)` at the same time. Both might select the same device.

**Solution:** `resolve_device()` wraps the select-and-claim in `_lock_pool_file()`. File locking serializes concurrent claims — second caller finds the device already claimed and falls through to the next candidate.

### Device disappears during boot

A simulator is deleted or becomes corrupted while we're booting it.

**Solution:** `_boot_and_wait()` has a timeout. If the device never reaches "booted" state, we raise `DeviceError` with a helpful message suggesting `simctl delete` and recreate.

### Stale pool state after external simctl operations

A user boots/shuts down simulators directly via Xcode or `xcrun simctl` outside of Quern.

**Solution:** `resolve_device()` always calls `refresh_from_simctl()` first (cached for 2s), so the pool state is never more than 2s stale. This is the same strategy already used by `list_devices()`.

### ensure_devices with session_id is not atomic

If `ensure_devices(count=4, session_id=...)` boots 2 devices successfully but the 3rd fails, we've already claimed the first 2.

**Solution:** On failure, release any devices we've already claimed in this call. Wrap the operation in a try/finally that cleans up partial claims.

```python
claimed_udids = []
try:
    for udid in selected_udids:
        # boot if needed, then claim
        claimed_udids.append(udid)
    return claimed_udids
except Exception:
    # Rollback: release anything we claimed
    for udid in claimed_udids:
        try:
            await self.release_device(udid, session_id=session_id)
        except Exception:
            pass  # Best effort cleanup
    raise
```

### wait_if_busy with auto_boot

If `wait_if_busy=True` and `auto_boot=True`, and there are shutdown+unclaimed devices available, we should boot those rather than waiting for a claimed device to be released.

**Solution:** The priority order handles this correctly. Auto-boot candidates (shutdown+unclaimed, priority 3) are checked before wait-for-release candidates (claimed, priority 4). We only wait if there are literally no unclaimed devices matching criteria.

---

## Success Criteria

- `resolve_device()` resolves by name, OS version, or both with correct priority ordering
- Auto-boot works: shutdown devices are booted on demand when `auto_boot=True`
- Wait-for-available works: blocks until a claimed device is released
- `ensure_devices()` guarantees N devices are booted and optionally claimed
- Error messages are actionable (tell the user exactly what's wrong and how to fix it)
- `DeviceController.resolve_udid()` delegates to pool when available
- Backwards compatible: existing single-device workflows are unaffected
- All existing 470+ tests pass
- New resolution tests cover all priority paths and edge cases
- MCP tools `resolve_device` and `ensure_devices` work end-to-end

---

## Next Steps

After Phase 4b-gamma:
- **Phase 4b-delta:** Testing & polish (integration tests with real simulators, docs, MCP guide updates)
- **Phase 4c:** Headless CLI runner (parallel test execution using `ensure_devices`)
