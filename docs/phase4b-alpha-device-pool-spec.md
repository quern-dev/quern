# Phase 4b-alpha: Device Pool - Implementation Spec

**Phase:** 4b-alpha
**Estimated Duration:** 1-2 days
**Status:** Ready for Implementation
**Dependencies:** Phase 3 (Device Control), Phase 4a (Process Lifecycle)

---

## Overview

Implement the core device pool system that tracks all available devices, their states, and which sessions have claimed them. This is the foundation for multi-device support and parallel test execution.

---

## Goals

1. **Track device state** - Know which devices exist, their boot state, and who's using them
2. **Exclusive claiming** - One device can only be claimed by one session at a time
3. **Safe concurrency** - Multiple clients can interact with the pool without conflicts
4. **Automatic cleanup** - Stale claims are detected and released
5. **Persistent state** - Pool state survives server restarts

---

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        Quern Server                         │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐  │
│  │                   DevicePool                         │  │
│  │                                                      │  │
│  │  - list_devices()                                   │  │
│  │  - claim_device()                                   │  │
│  │  - release_device()                                 │  │
│  │  - get_device_state()                              │  │
│  │  - refresh_from_simctl()                           │  │
│  │                                                      │  │
│  └──────────────┬───────────────────────────────────────┘  │
│                 │                                           │
│                 ├─→ DeviceController (existing)            │
│                 │    └─→ SimctlBackend                     │
│                 │                                           │
│                 └─→ device-pool.json (state file)          │
│                      └─→ file locking                       │
└─────────────────────────────────────────────────────────────┘
```

### Data Models

```python
class DeviceClaimStatus(str, enum.Enum):
    """Device claim state."""
    AVAILABLE = "available"    # Not claimed by anyone
    CLAIMED = "claimed"        # Claimed by a session

class DevicePoolEntry(BaseModel):
    """Single device in the pool."""
    udid: str
    name: str
    state: DeviceState         # booted, shutdown, booting
    device_type: DeviceType    # simulator, device
    os_version: str
    runtime: str

    # Claiming info
    claim_status: DeviceClaimStatus
    claimed_by: str | None     # session_id
    claimed_at: datetime | None
    last_used: datetime

    # Metadata
    is_available: bool         # From simctl (hardware availability)

class DevicePoolState(BaseModel):
    """Complete pool state for persistence."""
    version: str = "1.0"
    updated_at: datetime
    devices: dict[str, DevicePoolEntry]  # udid -> entry
```

---

## Implementation Details

### File: `server/device/pool.py`

```python
"""Device pool management for multi-device support."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from server.device.controller import DeviceController
from server.models import DeviceClaimStatus, DeviceError, DevicePoolEntry, DevicePoolState

logger = logging.getLogger("quern-debug-server.device-pool")

POOL_FILE = Path.home() / ".quern" / "device-pool.json"
CLAIM_TIMEOUT = timedelta(minutes=30)  # Auto-release after 30 min


class DevicePool:
    """Manages the pool of available devices and their claim states."""

    def __init__(self, controller: DeviceController):
        self.controller = controller
        self._pool_file = POOL_FILE
        self._pool_file.parent.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    async def list_devices(
        self,
        state_filter: str | None = None,      # "booted", "shutdown", None=all
        claimed_filter: str | None = None,    # "claimed", "available", None=all
    ) -> list[DevicePoolEntry]:
        """List all devices in the pool with optional filters."""
        await self.refresh_from_simctl()

        state = self._read_state()
        devices = list(state.devices.values())

        # Apply filters
        if state_filter:
            devices = [d for d in devices if d.state.value == state_filter]

        if claimed_filter == "claimed":
            devices = [d for d in devices if d.claim_status == DeviceClaimStatus.CLAIMED]
        elif claimed_filter == "available":
            devices = [d for d in devices if d.claim_status == DeviceClaimStatus.AVAILABLE]

        return devices

    async def claim_device(
        self,
        session_id: str,
        udid: str | None = None,
        name: str | None = None,
    ) -> DevicePoolEntry:
        """Claim a device for exclusive use by a session.

        Args:
            session_id: Session claiming the device
            udid: Specific device to claim (takes precedence)
            name: Device name pattern to match

        Returns:
            The claimed device entry

        Raises:
            DeviceError: If no matching device found or already claimed
        """
        await self.refresh_from_simctl()

        with self._lock_pool_file():
            state = self._read_state()

            # Find matching device
            if udid:
                device = state.devices.get(udid)
                if not device:
                    raise DeviceError(f"Device {udid} not found in pool", tool="pool")
            else:
                # Find first available matching name
                candidates = [
                    d for d in state.devices.values()
                    if (name is None or name.lower() in d.name.lower())
                    and d.claim_status == DeviceClaimStatus.AVAILABLE
                ]
                if not candidates:
                    raise DeviceError(
                        f"No available device found matching name='{name}'",
                        tool="pool"
                    )
                device = candidates[0]

            # Check if already claimed
            if device.claim_status == DeviceClaimStatus.CLAIMED:
                raise DeviceError(
                    f"Device {device.udid} ({device.name}) is already claimed by session {device.claimed_by}",
                    tool="pool"
                )

            # Claim it
            device.claim_status = DeviceClaimStatus.CLAIMED
            device.claimed_by = session_id
            device.claimed_at = datetime.utcnow()
            device.last_used = datetime.utcnow()

            state.devices[device.udid] = device
            state.updated_at = datetime.utcnow()
            self._write_state(state)

            logger.info(
                "Device claimed: %s (%s) by session %s",
                device.udid, device.name, session_id
            )

            return device

    async def release_device(
        self,
        udid: str,
        session_id: str | None = None,
    ) -> None:
        """Release a claimed device back to the pool.

        Args:
            udid: Device to release
            session_id: Session releasing (validated if provided)

        Raises:
            DeviceError: If device not found or not claimed by session
        """
        with self._lock_pool_file():
            state = self._read_state()

            device = state.devices.get(udid)
            if not device:
                raise DeviceError(f"Device {udid} not found in pool", tool="pool")

            if device.claim_status != DeviceClaimStatus.CLAIMED:
                logger.warning("Device %s was not claimed, ignoring release", udid)
                return

            # Validate session owns this device
            if session_id and device.claimed_by != session_id:
                raise DeviceError(
                    f"Device {udid} is claimed by session {device.claimed_by}, "
                    f"cannot release by session {session_id}",
                    tool="pool"
                )

            # Release it
            device.claim_status = DeviceClaimStatus.AVAILABLE
            device.claimed_by = None
            device.claimed_at = None
            device.last_used = datetime.utcnow()

            state.devices[device.udid] = device
            state.updated_at = datetime.utcnow()
            self._write_state(state)

            logger.info("Device released: %s (%s)", device.udid, device.name)

    async def get_device_state(self, udid: str) -> DevicePoolEntry | None:
        """Get the current state of a specific device."""
        state = self._read_state()
        return state.devices.get(udid)

    async def cleanup_stale_claims(self) -> list[str]:
        """Release devices with expired claims. Returns list of released UDIDs."""
        with self._lock_pool_file():
            state = self._read_state()
            now = datetime.utcnow()
            released = []

            for udid, device in state.devices.items():
                if device.claim_status == DeviceClaimStatus.CLAIMED and device.claimed_at:
                    age = now - device.claimed_at
                    if age > CLAIM_TIMEOUT:
                        logger.warning(
                            "Releasing stale claim: %s (%s) claimed %s ago by session %s",
                            device.udid, device.name, age, device.claimed_by
                        )
                        device.claim_status = DeviceClaimStatus.AVAILABLE
                        device.claimed_by = None
                        device.claimed_at = None
                        device.last_used = now
                        released.append(udid)

            if released:
                state.updated_at = now
                self._write_state(state)

            return released

    async def refresh_from_simctl(self) -> None:
        """Refresh pool state from simctl (discover new devices, update boot states)."""
        # Get current devices from simctl
        simctl_devices = await self.controller.list_devices()

        with self._lock_pool_file():
            state = self._read_state()
            now = datetime.utcnow()

            # Update or add devices
            for device_info in simctl_devices:
                if device_info.udid in state.devices:
                    # Update existing entry (preserve claim info)
                    entry = state.devices[device_info.udid]
                    entry.name = device_info.name
                    entry.state = device_info.state
                    entry.os_version = device_info.os_version
                    entry.runtime = device_info.runtime
                    entry.is_available = device_info.is_available
                else:
                    # New device discovered
                    entry = DevicePoolEntry(
                        udid=device_info.udid,
                        name=device_info.name,
                        state=device_info.state,
                        device_type=device_info.device_type,
                        os_version=device_info.os_version,
                        runtime=device_info.runtime,
                        claim_status=DeviceClaimStatus.AVAILABLE,
                        claimed_by=None,
                        claimed_at=None,
                        last_used=now,
                        is_available=device_info.is_available,
                    )
                    state.devices[device_info.udid] = entry

            state.updated_at = now
            self._write_state(state)

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _read_state(self) -> DevicePoolState:
        """Read pool state from disk."""
        if not self._pool_file.exists():
            return DevicePoolState(updated_at=datetime.utcnow(), devices={})

        try:
            data = json.loads(self._pool_file.read_text())
            return DevicePoolState.model_validate(data)
        except Exception as e:
            logger.error("Failed to parse pool state file: %s", e)
            return DevicePoolState(updated_at=datetime.utcnow(), devices={})

    def _write_state(self, state: DevicePoolState) -> None:
        """Write pool state to disk."""
        self._pool_file.write_text(
            state.model_dump_json(indent=2, exclude_none=True)
        )

    def _lock_pool_file(self):
        """Context manager for exclusive file locking."""
        # This will be a proper implementation using fcntl
        # For now, return a dummy context manager
        from contextlib import nullcontext
        return nullcontext()
```

### File Locking Implementation

Use `fcntl` for proper advisory file locking:

```python
import fcntl
from contextlib import contextmanager

@contextmanager
def _lock_pool_file(self):
    """Context manager for exclusive file locking."""
    lock_file = self._pool_file.parent / "device-pool.lock"
    lock_file.touch(exist_ok=True)

    with open(lock_file, "r") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

---

## API Endpoints

### File: `server/api/device_pool.py`

```python
"""API routes for device pool management."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from server.models import DeviceError

router = APIRouter(prefix="/api/v1/devices", tags=["device-pool"])


def _get_pool(request: Request):
    """Get the DevicePool from app state."""
    pool = request.app.state.device_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="Device pool not initialized")
    return pool


def _handle_pool_error(e: DeviceError) -> HTTPException:
    """Map a DeviceError to an appropriate HTTPException."""
    msg = str(e)
    if "not found" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    if "already claimed" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


# Request models
class ClaimDeviceRequest(BaseModel):
    session_id: str
    udid: str | None = None
    name: str | None = None


class ReleaseDeviceRequest(BaseModel):
    udid: str
    session_id: str | None = None


# Routes
@router.get("/pool")
async def list_device_pool(
    request: Request,
    state: str | None = Query(default=None, pattern="^(booted|shutdown)$"),
    claimed: str | None = Query(default=None, pattern="^(claimed|available)$"),
):
    """List all devices in the pool with optional filters.

    Query params:
    - state: Filter by boot state (booted, shutdown)
    - claimed: Filter by claim status (claimed, available)
    """
    pool = _get_pool(request)
    try:
        devices = await pool.list_devices(state_filter=state, claimed_filter=claimed)
        return {
            "devices": [d.model_dump() for d in devices],
            "total": len(devices),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/claim")
async def claim_device(request: Request, body: ClaimDeviceRequest):
    """Claim a device for exclusive use by a session.

    Returns 200 with device info on success.
    Returns 409 if device already claimed.
    Returns 404 if device not found.
    """
    pool = _get_pool(request)
    try:
        device = await pool.claim_device(
            session_id=body.session_id,
            udid=body.udid,
            name=body.name,
        )
        return {
            "status": "claimed",
            "device": device.model_dump(),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/release")
async def release_device(request: Request, body: ReleaseDeviceRequest):
    """Release a claimed device back to the pool."""
    pool = _get_pool(request)
    try:
        await pool.release_device(udid=body.udid, session_id=body.session_id)
        return {"status": "released", "udid": body.udid}
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/cleanup")
async def cleanup_stale_claims(request: Request):
    """Manually trigger cleanup of stale device claims."""
    pool = _get_pool(request)
    try:
        released = await pool.cleanup_stale_claims()
        return {
            "status": "cleaned",
            "devices_released": released,
            "count": len(released),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/refresh")
async def refresh_pool(request: Request):
    """Refresh pool state from simctl (discover new devices)."""
    pool = _get_pool(request)
    try:
        await pool.refresh_from_simctl()
        devices = await pool.list_devices()
        return {
            "status": "refreshed",
            "device_count": len(devices),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)
```

---

## MCP Tools

### File: `mcp/src/index.ts` (additions)

```typescript
server.tool(
  "list_device_pool",
  `List all devices in the pool with their claim status. Useful for seeing which devices are available for claiming.`,
  {
    state: z
      .enum(["booted", "shutdown"])
      .optional()
      .describe("Filter by boot state"),
    claimed: z
      .enum(["claimed", "available"])
      .optional()
      .describe("Filter by claim status"),
  },
  async ({ state, claimed }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/devices/pool", {
        state,
        claimed,
      });

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "claim_device",
  `Claim a device for exclusive use. Once claimed, no other session can use this device until it's released. Use this when running parallel tests to ensure device isolation.`,
  {
    session_id: z.string().describe("Session ID claiming the device"),
    udid: z.string().optional().describe("Specific device UDID to claim"),
    name: z
      .string()
      .optional()
      .describe("Device name pattern (e.g., 'iPhone 16 Pro')"),
  },
  async ({ session_id, udid, name }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/devices/claim",
        undefined,
        { session_id, udid, name }
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "release_device",
  `Release a claimed device back to the pool, making it available for other sessions. Always release devices when done to avoid resource exhaustion.`,
  {
    udid: z.string().describe("Device UDID to release"),
    session_id: z
      .string()
      .optional()
      .describe("Session ID releasing (for validation)"),
  },
  async ({ udid, session_id }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/devices/release",
        undefined,
        { udid, session_id }
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  }
);
```

---

## Integration with Existing Code

### 1. Server Startup (`server/main.py`)

```python
from server.device.pool import DevicePool

async def lifespan(app: FastAPI):
    # ... existing startup ...

    # Initialize device pool
    device_pool = DevicePool(device_controller)
    app.state.device_pool = device_pool

    # Refresh pool state on startup
    await device_pool.refresh_from_simctl()

    # Cleanup stale claims from previous runs
    released = await device_pool.cleanup_stale_claims()
    if released:
        logger.info("Cleaned up %d stale device claims on startup", len(released))

    # ... existing shutdown ...
```

### 2. Router Registration

```python
from server.api import device_pool

app.include_router(device_pool.router)
```

### 3. Models (`server/models.py`)

Add the new models at the end of the file:

```python
class DeviceClaimStatus(str, enum.Enum):
    """Device claim state."""
    AVAILABLE = "available"
    CLAIMED = "claimed"

class DevicePoolEntry(BaseModel):
    """Single device in the pool."""
    udid: str
    name: str
    state: DeviceState
    device_type: DeviceType
    os_version: str
    runtime: str

    claim_status: DeviceClaimStatus
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    last_used: datetime
    is_available: bool

class DevicePoolState(BaseModel):
    """Complete pool state for persistence."""
    version: str = "1.0"
    updated_at: datetime
    devices: dict[str, DevicePoolEntry]
```

---

## Testing

### Unit Tests (`tests/test_device_pool.py`)

```python
"""Tests for device pool management."""

import pytest
from datetime import datetime, timedelta

from server.device.pool import DevicePool
from server.models import DeviceClaimStatus


@pytest.fixture
def mock_pool(tmp_path, mock_controller):
    """DevicePool with mocked controller and temp state file."""
    pool = DevicePool(mock_controller)
    pool._pool_file = tmp_path / "device-pool.json"
    return pool


class TestListDevices:
    async def test_list_all_devices(self, mock_pool):
        # Setup: refresh from simctl
        await mock_pool.refresh_from_simctl()

        devices = await mock_pool.list_devices()
        assert len(devices) > 0
        assert all(d.claim_status == DeviceClaimStatus.AVAILABLE for d in devices)

    async def test_filter_by_state(self, mock_pool):
        await mock_pool.refresh_from_simctl()

        booted = await mock_pool.list_devices(state_filter="booted")
        assert all(d.state.value == "booted" for d in booted)


class TestClaimDevice:
    async def test_claim_available_device(self, mock_pool):
        await mock_pool.refresh_from_simctl()

        device = await mock_pool.claim_device(
            session_id="test-session",
            name="iPhone 16 Pro"
        )

        assert device.claim_status == DeviceClaimStatus.CLAIMED
        assert device.claimed_by == "test-session"
        assert device.claimed_at is not None

    async def test_claim_already_claimed_device_errors(self, mock_pool):
        await mock_pool.refresh_from_simctl()

        # Claim once
        device1 = await mock_pool.claim_device(session_id="session1", name="iPhone")

        # Try to claim again
        with pytest.raises(DeviceError, match="already claimed"):
            await mock_pool.claim_device(session_id="session2", udid=device1.udid)

    async def test_claim_by_udid(self, mock_pool):
        await mock_pool.refresh_from_simctl()
        devices = await mock_pool.list_devices()
        udid = devices[0].udid

        device = await mock_pool.claim_device(session_id="test", udid=udid)
        assert device.udid == udid


class TestReleaseDevice:
    async def test_release_claimed_device(self, mock_pool):
        await mock_pool.refresh_from_simctl()

        # Claim
        device = await mock_pool.claim_device(session_id="test", name="iPhone")

        # Release
        await mock_pool.release_device(udid=device.udid, session_id="test")

        # Verify released
        state = await mock_pool.get_device_state(device.udid)
        assert state.claim_status == DeviceClaimStatus.AVAILABLE
        assert state.claimed_by is None

    async def test_release_wrong_session_errors(self, mock_pool):
        await mock_pool.refresh_from_simctl()

        device = await mock_pool.claim_device(session_id="session1", name="iPhone")

        with pytest.raises(DeviceError, match="claimed by session"):
            await mock_pool.release_device(udid=device.udid, session_id="session2")


class TestStaleClaimCleanup:
    async def test_cleanup_stale_claims(self, mock_pool):
        await mock_pool.refresh_from_simctl()

        # Claim a device
        device = await mock_pool.claim_device(session_id="test", name="iPhone")

        # Manually set claimed_at to 31 minutes ago
        state = mock_pool._read_state()
        state.devices[device.udid].claimed_at = datetime.utcnow() - timedelta(minutes=31)
        mock_pool._write_state(state)

        # Run cleanup
        released = await mock_pool.cleanup_stale_claims()

        assert device.udid in released
        assert len(released) == 1
```

### Integration Tests (`tests/test_device_pool_api.py`)

```python
"""Integration tests for device pool API endpoints."""

import pytest
from httpx import AsyncClient


class TestDevicePoolAPI:
    async def test_list_pool(self, app, auth_headers):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/devices/pool", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "devices" in data
        assert "total" in data

    async def test_claim_and_release(self, app, auth_headers):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Claim
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "test", "name": "iPhone"}
            )
            assert resp.status_code == 200
            udid = resp.json()["device"]["udid"]

            # Release
            resp = await client.post(
                "/api/v1/devices/release",
                headers=auth_headers,
                json={"udid": udid, "session_id": "test"}
            )
            assert resp.status_code == 200
```

---

## Success Criteria

✅ DevicePool class implemented with all core methods
✅ File locking prevents race conditions
✅ State persists to `~/.quern/device-pool.json`
✅ API endpoints functional (list, claim, release, cleanup, refresh)
✅ MCP tools added and working
✅ Unit tests pass (claim, release, stale cleanup)
✅ Integration tests pass (API endpoints)
✅ All 470+ existing tests still pass
✅ Manual testing with multiple devices succeeds

---

## Deliverables Checklist

- [ ] `server/device/pool.py` - DevicePool implementation
- [ ] `server/api/device_pool.py` - API routes
- [ ] `server/models.py` - New models (DeviceClaimStatus, DevicePoolEntry, DevicePoolState)
- [ ] `server/main.py` - Integration (startup, router registration)
- [ ] `mcp/src/index.ts` - MCP tools (list_device_pool, claim_device, release_device)
- [ ] `tests/test_device_pool.py` - Unit tests
- [ ] `tests/test_device_pool_api.py` - Integration tests
- [ ] All tests passing

---

## Next Steps

After Phase 4b-alpha completes:
- **Phase 4b-beta:** Session Management (wrap claims in sessions)
- **Phase 4b-gamma:** Resolution Protocol (smart device selection)
- **Phase 4b-delta:** Testing & Polish (docs, examples)

Then Phase 4c can leverage the pool for parallel test execution.
