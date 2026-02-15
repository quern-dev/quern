# Phase 4b-delta: Testing, Polish & Agent UX — Implementation Spec

**Phase:** 4b-delta
**Status:** Ready for Implementation
**Dependencies:** Phase 4b-alpha (Device Pool), Phase 4b-beta (Session Management), Phase 4b-gamma (Resolution Protocol)

---

## Overview

Phase 4b-delta is the final sub-phase of multi-device support. It focuses on three areas:

1. **Testing** — Comprehensive tests for pool logic, resolution protocol, and multi-device scenarios that go beyond the per-phase unit tests already written. Focus on race conditions, crash recovery, and cross-session interactions.
2. **Agent UX polish** — Fix discoverability gaps observed during real agent testing: MCP guide updates, improved tool descriptions, and a new `clear_text` composite action.
3. **`get_screen_summary` hierarchy option** — Add optional hierarchical output for UI tree queries.

---

## Goals

1. **High confidence in pool correctness** — Race conditions, double-booking, and crash recovery are tested
2. **Agents can self-serve** — MCP guide includes tool→REST mapping, `tap_element` documents `element_type`, `os_version` format is documented
3. **`clear_text` reduces friction** — Pre-filled text fields no longer require agents to manually select-all and delete
4. **Hierarchy option unlocks scoped queries** — "Find the TextArea inside the Post log view" becomes possible
5. **All 470+ existing tests still pass** — No regressions

---

## Part 1: Testing

### 1.1 Pool Concurrency Tests (`tests/test_pool_concurrency.py`)

These tests verify that the file-locking strategy actually prevents double-booking under concurrent access. They go beyond the existing `test_device_pool.py` and `test_resolution_protocol.py` by using real asyncio concurrency.

```python
"""Concurrency and race condition tests for device pool."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.device.pool import DevicePool
from server.models import DeviceClaimStatus, DeviceError, DeviceInfo, DeviceState, DeviceType


@pytest.fixture
def pool_3_devices(tmp_path):
    """Pool with 3 booted, unclaimed devices."""
    from server.device.controller import DeviceController
    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.boot = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(return_value=[
        DeviceInfo(udid=f"DEV-{i}", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                   device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                   runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2", is_available=True)
        for i in range(3)
    ])
    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    return pool


class TestConcurrentClaims:
    """Verify no double-booking under concurrent access."""

    async def test_concurrent_claims_no_double_booking(self, pool_3_devices):
        """Three concurrent claims on 3 devices should each get a unique device."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        results = await asyncio.gather(
            pool.claim_device(session_id="s1", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s2", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s3", name="iPhone 16 Pro"),
        )

        # All should succeed with unique UDIDs
        udids = [r.udid for r in results]
        assert len(set(udids)) == 3, f"Expected 3 unique UDIDs, got {udids}"

    async def test_fourth_concurrent_claim_fails(self, pool_3_devices):
        """Four concurrent claims on 3 devices — one must fail."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        results = await asyncio.gather(
            pool.claim_device(session_id="s1", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s2", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s3", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s4", name="iPhone 16 Pro"),
            return_exceptions=True,
        )

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, DeviceError)]
        assert len(successes) == 3
        assert len(failures) == 1
        assert "claimed" in str(failures[0]).lower()

    async def test_concurrent_resolve_no_double_booking(self, pool_3_devices):
        """Concurrent resolve_device calls with session_id should not double-book."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        results = await asyncio.gather(
            pool.resolve_device(name="iPhone 16 Pro", session_id="s1"),
            pool.resolve_device(name="iPhone 16 Pro", session_id="s2"),
            pool.resolve_device(name="iPhone 16 Pro", session_id="s3"),
        )

        assert len(set(results)) == 3

    async def test_claim_then_release_then_reclaim(self, pool_3_devices):
        """Release makes a device immediately available for re-claiming."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        device = await pool.claim_device(session_id="s1", name="iPhone 16 Pro")
        await pool.release_device(udid=device.udid, session_id="s1")

        # Same device should be reclaimable
        device2 = await pool.claim_device(session_id="s2", udid=device.udid)
        assert device2.udid == device.udid
        assert device2.claimed_by == "s2"


class TestCleanupRecovery:
    """Test session expiry and orphan cleanup."""

    async def test_expired_claim_auto_released(self, pool_3_devices):
        """Claims older than CLAIM_TIMEOUT should be released by cleanup."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        device = await pool.claim_device(session_id="s1", name="iPhone 16 Pro")

        # Manually backdate the claim
        from datetime import timedelta
        with pool._lock_pool_file():
            state = pool._read_state()
            entry = state.devices[device.udid]
            entry.claimed_at = entry.claimed_at - timedelta(minutes=60)
            pool._write_state(state)

        released = await pool.cleanup_stale_claims()
        assert device.udid in released

        # Device should now be available
        state_after = pool._read_state()
        assert state_after.devices[device.udid].claim_status == DeviceClaimStatus.AVAILABLE

    async def test_multiple_sessions_share_pool(self, pool_3_devices):
        """Two sessions can each claim different devices from the same pool."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        d1 = await pool.claim_device(session_id="session-A", name="iPhone 16 Pro")
        d2 = await pool.claim_device(session_id="session-B", name="iPhone 16 Pro")

        assert d1.udid != d2.udid
        assert d1.claimed_by == "session-A"
        assert d2.claimed_by == "session-B"

        # Release one, verify the other is still claimed
        await pool.release_device(udid=d1.udid, session_id="session-A")
        state = pool._read_state()
        assert state.devices[d1.udid].claim_status == DeviceClaimStatus.AVAILABLE
        assert state.devices[d2.udid].claim_status == DeviceClaimStatus.CLAIMED
```

### 1.2 Controller Fallback Tests (`tests/test_controller_pool_fallback.py`)

Focused tests for the `DeviceController.resolve_udid()` fallback behavior — the highest-risk code path in 4b-gamma. Some of these were sketched in the gamma spec; this section finalizes them.

```python
"""Tests for DeviceController.resolve_udid() pool fallback behavior."""

import pytest
from unittest.mock import AsyncMock

from server.device.controller import DeviceController
from server.device.pool import DevicePool
from server.models import DeviceError, DeviceInfo, DeviceState, DeviceType


@pytest.fixture
def controller_with_pool(tmp_path):
    """Controller with a mock pool attached."""
    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(return_value=[
        DeviceInfo(udid="SOLO", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                   device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                   runtime="...", is_available=True),
    ])

    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    ctrl._pool = pool
    return ctrl, pool


class TestPoolFallback:

    async def test_pool_none_uses_old_logic(self):
        """When _pool is None, behave identically to pre-4b-gamma."""
        ctrl = DeviceController()
        ctrl._pool = None
        ctrl.simctl = AsyncMock()
        ctrl.simctl.list_devices = AsyncMock(return_value=[
            DeviceInfo(udid="ONLY", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                       device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                       runtime="...", is_available=True),
        ])
        udid = await ctrl.resolve_udid()
        assert udid == "ONLY"

    async def test_pool_exception_falls_back_silently(self, controller_with_pool):
        """When pool.resolve_device() raises, fall back without crashing."""
        ctrl, pool = controller_with_pool
        pool.resolve_device = AsyncMock(side_effect=Exception("pool is broken"))

        udid = await ctrl.resolve_udid()
        assert udid == "SOLO"  # Fell back to simple logic

    async def test_pool_success_skips_fallback(self, controller_with_pool):
        """When pool resolves successfully, don't call simctl.list_devices."""
        ctrl, pool = controller_with_pool
        pool.resolve_device = AsyncMock(return_value="POOL-DEVICE")

        udid = await ctrl.resolve_udid()
        assert udid == "POOL-DEVICE"
        ctrl.simctl.list_devices.assert_not_called()

    async def test_explicit_udid_bypasses_pool(self, controller_with_pool):
        """Explicit UDID never touches the pool."""
        ctrl, pool = controller_with_pool
        pool.resolve_device = AsyncMock()

        udid = await ctrl.resolve_udid(udid="EXPLICIT")
        assert udid == "EXPLICIT"
        pool.resolve_device.assert_not_called()

    async def test_active_udid_bypasses_pool(self, controller_with_pool):
        """Stored active UDID never touches the pool."""
        ctrl, pool = controller_with_pool
        ctrl._active_udid = "STORED"
        pool.resolve_device = AsyncMock()

        udid = await ctrl.resolve_udid()
        assert udid == "STORED"
        pool.resolve_device.assert_not_called()
```

### 1.3 Integration Tests (`tests/test_multi_device_integration.py`)

API-level tests that exercise the full stack: HTTP request → API route → pool → controller (mocked simctl).

```python
"""Integration tests for multi-device pool API endpoints."""

import pytest
from httpx import AsyncClient


class TestPoolAPIIntegration:

    async def test_claim_resolve_release_cycle(self, app, auth_headers):
        """Full lifecycle: claim → use → release."""
        async with AsyncClient(app=app, base_url="http://test") as client:
            # Claim
            resp = await client.post("/api/v1/devices/claim",
                json={"session_id": "int-test", "name": "iPhone 16 Pro"},
                headers=auth_headers)
            assert resp.status_code == 200
            udid = resp.json()["device"]["udid"]

            # Resolve should find a different device for another session
            resp2 = await client.post("/api/v1/devices/resolve",
                json={"name": "iPhone 16 Pro", "session_id": "int-test-2"},
                headers=auth_headers)
            assert resp2.status_code == 200
            assert resp2.json()["udid"] != udid

            # Release first device
            resp3 = await client.post("/api/v1/devices/release",
                json={"udid": udid, "session_id": "int-test"},
                headers=auth_headers)
            assert resp3.status_code == 200

    async def test_ensure_devices_claims_all(self, app, auth_headers):
        """ensure_devices with session_id claims all returned devices."""
        async with AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.post("/api/v1/devices/ensure",
                json={"count": 2, "name": "iPhone 16 Pro", "session_id": "bulk-test"},
                headers=auth_headers)
            assert resp.status_code == 200
            devices = resp.json()["devices"]
            assert len(devices) == 2

            # Verify all are claimed
            pool_resp = await client.get("/api/v1/devices/pool?claimed=claimed",
                headers=auth_headers)
            claimed = pool_resp.json()["devices"]
            claimed_udids = {d["udid"] for d in claimed}
            for d in devices:
                assert d["udid"] in claimed_udids

    async def test_resolve_with_auto_boot(self, app, auth_headers):
        """resolve with auto_boot=true boots a shutdown device."""
        # (Requires all booted devices to be claimed first, forcing auto-boot)
        async with AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.post("/api/v1/devices/resolve",
                json={"name": "iPhone 16 Pro", "auto_boot": True},
                headers=auth_headers)
            assert resp.status_code == 200

    async def test_resolve_no_match_returns_404(self, app, auth_headers):
        """resolve with impossible criteria returns 404 with diagnostic message."""
        async with AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.post("/api/v1/devices/resolve",
                json={"name": "iPad Pro"},
                headers=auth_headers)
            assert resp.status_code == 404
            assert "iPad Pro" in resp.json()["detail"]
```

### 1.4 Boot-on-Demand Flow Test

Add to `tests/test_resolution_protocol.py`:

```python
class TestBootOnDemand:
    """Test the full auto-boot flow end-to-end."""

    async def test_boot_timeout_raises(self, pool_with_devices):
        """Boot that never completes should raise DeviceError."""
        await pool_with_devices.refresh_from_simctl()

        # Claim both booted devices
        await pool_with_devices.claim_device(session_id="s1", udid="AAAA")
        await pool_with_devices.claim_device(session_id="s2", udid="BBBB")

        # Mock boot that succeeds but device never becomes booted
        pool_with_devices.controller.simctl.boot = AsyncMock()
        pool_with_devices.controller.simctl.list_devices = AsyncMock(return_value=[
            DeviceInfo(udid="CCCC", name="iPhone 16 Pro", state=DeviceState.SHUTDOWN,
                       os_version="iOS 18.2", runtime="...", is_available=True),
        ])

        with pytest.raises(DeviceError, match="did not boot"):
            await pool_with_devices.resolve_device(
                name="iPhone 16 Pro",
                auto_boot=True,
                # Use tiny timeout to avoid slow test
            )
            # Note: _boot_and_wait default is 30s — override in test via direct call
```

### 1.5 Run Existing Suite

After all new tests are added, the full suite must pass:

```bash
.venv/bin/pytest -v
```

**Expectation:** All 470+ existing tests pass, plus ~25-30 new tests from this phase.

---

## Part 2: Agent UX Polish

### 2.1 MCP Guide Update — Tool→REST Path Mapping Table

**Problem:** Agents guess incorrect REST paths (e.g., `/device/swipe` when the actual path is `/device/ui/swipe`). The MCP guide resource has a "Quick Reference" table mapping intent→tool, but not tool→REST path.

**Fix:** Add a REST path mapping section to the `GUIDE_CONTENT` constant in `mcp/src/index.ts`.

**New section to add after the Quick Reference table:**

```markdown
## REST API Path Reference

When calling the HTTP API directly (without MCP), use these paths:

| MCP Tool             | HTTP Method | REST Path                              |
|----------------------|-------------|----------------------------------------|
| `ensure_server`      | GET         | `/health`                              |
| `tail_logs`          | GET         | `/api/v1/logs/query`                   |
| `query_logs`         | GET         | `/api/v1/logs/query`                   |
| `get_log_summary`    | GET         | `/api/v1/logs/summary`                 |
| `get_errors`         | GET         | `/api/v1/logs/errors`                  |
| `get_build_result`   | GET         | `/api/v1/builds/latest`                |
| `get_latest_crash`   | GET         | `/api/v1/crashes/latest`               |
| `set_log_filter`     | POST        | `/api/v1/logs/filter`                  |
| `list_log_sources`   | GET         | `/api/v1/logs/sources`                 |
| `query_flows`        | GET         | `/api/v1/proxy/flows`                  |
| `get_flow_detail`    | GET         | `/api/v1/proxy/flows/{id}`             |
| `get_flow_summary`   | GET         | `/api/v1/proxy/flows/summary`          |
| `proxy_status`       | GET         | `/api/v1/proxy/status`                 |
| `start_proxy`        | POST        | `/api/v1/proxy/start`                  |
| `stop_proxy`         | POST        | `/api/v1/proxy/stop`                   |
| `set_intercept`      | POST        | `/api/v1/proxy/intercept`              |
| `clear_intercept`    | DELETE      | `/api/v1/proxy/intercept`              |
| `list_held_flows`    | GET         | `/api/v1/proxy/intercept/held`         |
| `release_flow`       | POST        | `/api/v1/proxy/intercept/release`      |
| `replay_flow`        | POST        | `/api/v1/proxy/replay/{id}`            |
| `set_mock`           | POST        | `/api/v1/proxy/mock`                   |
| `list_mocks`         | GET         | `/api/v1/proxy/mock`                   |
| `clear_mocks`        | DELETE      | `/api/v1/proxy/mock`                   |
| `list_devices`       | GET         | `/api/v1/device/list`                  |
| `boot_device`        | POST        | `/api/v1/device/boot`                  |
| `shutdown_device`    | POST        | `/api/v1/device/shutdown`              |
| `install_app`        | POST        | `/api/v1/device/app/install`           |
| `launch_app`         | POST        | `/api/v1/device/app/launch`            |
| `terminate_app`      | POST        | `/api/v1/device/app/terminate`         |
| `list_apps`          | GET         | `/api/v1/device/app/list`              |
| `take_screenshot`    | GET         | `/api/v1/device/screenshot`            |
| `get_ui_tree`        | GET         | `/api/v1/device/ui`                    |
| `get_element_state`  | GET         | `/api/v1/device/ui/element`            |
| `wait_for_element`   | POST        | `/api/v1/device/ui/wait-for-element`   |
| `get_screen_summary` | GET         | `/api/v1/device/screen-summary`        |
| `tap`                | POST        | `/api/v1/device/ui/tap`                |
| `tap_element`        | POST        | `/api/v1/device/ui/tap-element`        |
| `swipe`              | POST        | `/api/v1/device/ui/swipe`              |
| `type_text`          | POST        | `/api/v1/device/ui/type`               |
| `clear_text`         | POST        | `/api/v1/device/ui/clear`              |
| `press_button`       | POST        | `/api/v1/device/ui/press`              |
| `set_location`       | POST        | `/api/v1/device/location`              |
| `grant_permission`   | POST        | `/api/v1/device/permission`            |
| `list_device_pool`   | GET         | `/api/v1/devices/pool`                 |
| `claim_device`       | POST        | `/api/v1/devices/claim`                |
| `release_device`     | POST        | `/api/v1/devices/release`              |
| `resolve_device`     | POST        | `/api/v1/devices/resolve`              |
| `ensure_devices`     | POST        | `/api/v1/devices/ensure`               |
```

### 2.2 MCP Tool Description Improvements

Update existing tool descriptions in `mcp/src/index.ts`:

#### `tap_element` — Document `element_type` as a narrowing parameter

**Current description:**
```
Find a UI element by label or accessibility identifier and tap its center.
Returns "ambiguous" with match list if multiple elements match.
```

**Updated description:**
```
Find a UI element by label or accessibility identifier and tap its center.
Returns "ambiguous" with match list if multiple elements match — use
element_type (e.g., "Button", "TextField", "StaticText") to narrow results.
Requires idb.
```

#### `resolve_device` — `os_version` format already documented (verify)

The `os_version` field in `resolve_device` and `ensure_devices` already includes the description `Accepts both '18.2' and 'iOS 18.2'`. Verify this is present — if not, add it. (Status: already present in current code at lines 2349 and 2426.)

### 2.3 MCP Guide — Device Control Workflow Update

Update the "Device Control Workflow" section in the guide to recommend `resolve_device` as the preferred starting point:

**Updated section:**

```markdown
### 9. Device Control Workflow

Use device tools to inspect and interact with the simulator:

1. `resolve_device` — find the best available device matching your criteria (preferred over manual listing)
2. `boot_device` — boot a simulator if needed (or use `resolve_device` with `auto_boot: true`)
3. `install_app` / `launch_app` — deploy and start the app
4. `get_screen_summary` — understand what's on screen (text description)
5. `take_screenshot` — see the actual screen image
6. `tap_element` — interact with UI elements by label/identifier
7. `type_text` — enter text into focused fields
8. `clear_text` — clear a pre-filled text field before typing new content
9. `get_screen_summary` — verify the result

**Tip**: `tap_element` is preferred over `tap` (coordinates) because it finds
elements by label, handling layout differences. If multiple elements match, it
returns "ambiguous" with a list — narrow by `element_type` or `identifier`.

**Tip**: For parallel testing, use `ensure_devices` to boot and claim N devices,
then pass each device's `udid` to subsequent tool calls for isolation.
```

---

## Part 3: `clear_text` Composite Action

### 3.1 Problem

When an agent encounters a pre-filled text field (e.g., a search bar with existing text), it currently has to:
1. Triple-tap to select all (or Cmd+A via key events)
2. Press delete
3. Type new text

This is fragile and platform-specific. A `clear_text` composite action encapsulates this.

### 3.2 Implementation

#### IdbBackend — Add `key_sequence` support

The `clear_text` action needs to send Cmd+A (select all) followed by Delete. idb supports key events via `idb ui key-sequence`. Add a helper to `server/device/idb.py`:

```python
async def key_sequence(self, udid: str, keys: list[int]) -> None:
    """Send a sequence of key events.

    Args:
        udid: Target device
        keys: List of key codes (HID usage IDs)
    """
    binary = self._resolve_binary()
    args = [binary, "ui", "key-sequence"]
    for key in keys:
        args.append(str(key))
    args.extend(["--udid", udid])
    await self._run(args)
```

**Key codes for select-all + delete:**
- Cmd+A = modifier key (0xe3 = left GUI/Cmd) + 0x04 (A)
- Delete = 0x2a (Backspace)

**Alternative approach (simpler, more reliable):** Use `idb ui text` with a special sequence, or use `simctl` keyboard input. After investigation, the most reliable approach for simulators is:

```python
async def select_all_and_delete(self, udid: str) -> None:
    """Select all text in focused field and delete it.

    Uses simctl keyboard shortcut (Cmd+A) followed by idb delete key.
    This is more reliable than raw HID key codes across iOS versions.
    """
    # Cmd+A via simctl keychain (most reliable for simulators)
    binary = self._resolve_binary()
    # idb ui key 4 --modifier 8 = Cmd+A (HID key A=4, modifier Cmd=8)
    await self._run([binary, "ui", "key", "4", "--modifier", "8", "--udid", udid])
    # Small delay for selection to take effect
    import asyncio
    await asyncio.sleep(0.1)
    # Delete the selection
    await self._run([binary, "ui", "key", "42", "--udid", udid])  # 42 = Backspace
```

#### DeviceController — Add `clear_text` method

```python
async def clear_text(self, udid: str | None = None) -> str:
    """Clear text in the currently focused text field.

    Sends Cmd+A (select all) followed by Delete.
    Returns the resolved udid.
    """
    resolved = await self.resolve_udid(udid)
    await self.idb.select_all_and_delete(resolved)
    self._invalidate_ui_cache(resolved)
    return resolved
```

#### API Endpoint

Add to `server/api/device.py`:

```python
class ClearTextRequest(BaseModel):
    udid: str | None = None
```

```python
@router.post("/ui/clear")
async def clear_text(request: Request, body: ClearTextRequest):
    """Clear text in the currently focused text field (select-all + delete)."""
    controller = _get_controller(request)
    try:
        resolved = await controller.clear_text(udid=body.udid)
        return {"status": "ok", "udid": resolved}
    except DeviceError as e:
        raise _handle_device_error(e)
```

#### Request Model

Add to `server/models.py`:

```python
class ClearTextRequest(BaseModel):
    """Request body for POST /api/v1/device/ui/clear."""
    udid: str | None = None
```

#### MCP Tool

Add to `mcp/src/index.ts`:

```typescript
server.tool(
  "clear_text",
  `Clear all text in the currently focused input field (select-all + delete).
Use this before type_text when a field has pre-existing content you want to replace.
Requires idb.`,
  {
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/clear",
        undefined,
        body
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

#### Tests

```python
"""Tests for clear_text composite action."""

class TestClearText:

    async def test_clear_text_sends_select_all_delete(self, mock_controller):
        """clear_text should call idb select_all_and_delete."""
        controller = mock_controller
        controller.idb.select_all_and_delete = AsyncMock()
        controller._active_udid = "AAAA"

        udid = await controller.clear_text()
        assert udid == "AAAA"
        controller.idb.select_all_and_delete.assert_called_once_with("AAAA")

    async def test_clear_text_invalidates_cache(self, mock_controller):
        """clear_text should invalidate UI cache."""
        controller = mock_controller
        controller.idb.select_all_and_delete = AsyncMock()
        controller._active_udid = "AAAA"
        controller._ui_cache["AAAA"] = ([], 0)

        await controller.clear_text()
        assert "AAAA" not in controller._ui_cache

    async def test_clear_text_api_endpoint(self, app, auth_headers):
        """POST /ui/clear should return ok status."""
        # Standard text field — the expected case
        async with AsyncClient(app=app, base_url="http://test") as client:
            resp = await client.post("/api/v1/device/ui/clear",
                json={}, headers=auth_headers)
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
```

#### Known Limitation: Secure Text Fields

Secure text fields (e.g., password inputs) may not respond to Cmd+A (select-all) in the same way as standard text fields — iOS intentionally restricts programmatic selection of password content. The `clear_text` action is designed for standard `UITextField` / `UITextView` fields. For secure fields, agents should use triple-tap to position the cursor, then hold-delete, or simply clear the field via the app's own clear button if available. Add a note to the MCP tool description:

```
Note: Secure text fields (passwords) may not support select-all. For those,
use tap to focus the field, then hold the delete key or use the app's clear button.
```

---

## Part 4: `get_screen_summary` Hierarchy Option

### 4.1 Problem

The current `get_screen_summary` and `get_ui_tree` endpoints return a flat list of elements. This is token-efficient but loses parent-child relationships. An agent seeing "TextArea" and "Post log view" in a flat list can't determine that the TextArea is *inside* the Post log view.

### 4.2 Implementation

#### API Change — `GET /api/v1/device/ui`

Add optional query parameter:

```
GET /api/v1/device/ui?include_hierarchy=true
```

When `include_hierarchy=true`, the response includes a nested `children` field on each element instead of a flat list.

#### API Change — `GET /api/v1/device/screen-summary`

Add optional query parameter:

```
GET /api/v1/device/screen-summary?include_hierarchy=true
```

When true, the summary includes parent context for each element:
```
- Button "Submit" (inside: Form > Card > ScrollView)
```

#### Alternative: `children_of` query parameter

Instead of (or in addition to) full hierarchy, support scoped queries:

```
GET /api/v1/device/ui?children_of=PostLogView
```

Returns only elements that are children of the element with that identifier. This is more token-efficient than the full hierarchy and directly addresses the "find X inside Y" use case.

**Recommendation:** Implement `children_of` first — it's simpler, more token-efficient, and directly addresses the real-world need. Full hierarchy can be added later if needed.

#### `ui_elements.py` Changes

The current `parse_elements()` function flattens the idb output. To support hierarchy:

1. During parsing, build a parent map: `{element_id: parent_element_id}`
2. Add a `parent_identifier` or `parent_label` field to UIElement (optional, only populated when hierarchy is requested)
3. For `children_of`, filter elements whose parent chain includes the target identifier

```python
def find_children_of(
    elements: list[UIElement],
    parent_identifier: str | None = None,
    parent_label: str | None = None,
) -> list[UIElement]:
    """Find elements that are children of the specified parent.

    Searches by identifier first (exact match), falls back to label (case-insensitive).
    Returns direct and nested children.
    """
```

**Important:** The raw idb `describe-all` output already includes nesting (indentation-based). The current parser strips this. To support hierarchy, we need to preserve the tree structure during parsing and add a way to query it.

**Robustness note:** idb's output format is not formally specified and has varied across versions. The indentation-based nesting can be inconsistent (e.g., mixed tab/space indentation, varying indent depths between idb versions, extra blank lines). The hierarchy parser must handle this gracefully:
- Normalize whitespace (treat tabs as N spaces)
- Use relative indentation changes (increase = child, decrease = pop up) rather than absolute column positions
- If indentation is ambiguous or unparseable, fall back to a flat list and log a warning rather than crashing
- Add a regression test with at least two different indentation styles captured from real idb output

#### MCP Tool Update — `get_ui_tree`

Add `children_of` parameter:

```typescript
server.tool(
  "get_ui_tree",
  `Get the UI accessibility tree from the current screen. Optionally scope to children of a specific element. Requires idb.`,
  {
    udid: z.string().optional().describe("Target device UDID"),
    children_of: z.string().optional()
      .describe("Only return children of the element with this identifier or label"),
  },
  // ...
);
```

#### Tests

```python
class TestChildrenOf:

    async def test_children_of_returns_nested_elements(self):
        """children_of should return only descendants of the target."""
        # Build a tree with known hierarchy
        # Verify children_of returns correct subset

    async def test_children_of_unknown_parent_returns_empty(self):
        """children_of with nonexistent parent returns empty list."""

    async def test_children_of_by_label_case_insensitive(self):
        """children_of should match parent label case-insensitively."""
```

---

## File Changes

### New Files

| File | Purpose |
|------|---------|
| `tests/test_pool_concurrency.py` | Concurrent claim/release tests |
| `tests/test_controller_pool_fallback.py` | Controller fallback behavior tests |
| `tests/test_multi_device_integration.py` | API-level integration tests |

### Modified Files

| File | Changes |
|------|---------|
| `server/device/idb.py` | Add `select_all_and_delete()` method |
| `server/device/controller.py` | Add `clear_text()` method |
| `server/device/ui_elements.py` | Add `find_children_of()` for hierarchy queries |
| `server/api/device.py` | Add `POST /ui/clear` endpoint, add `children_of` param to `GET /ui` |
| `server/models.py` | Add `ClearTextRequest` |
| `mcp/src/index.ts` | Add `clear_text` tool, update `tap_element` description, update `get_ui_tree` with `children_of`, add REST path mapping table to guide, update device workflow section |
| `tests/test_resolution_protocol.py` | Add boot-on-demand timeout test |

---

## Implementation Order

Execute in this order to maintain a working system at each step:

### Step 1: Tests for existing pool behavior (~1.5 hours)

Create all three test files from Part 1. Run the full suite to verify everything passes before making any code changes.

- `tests/test_pool_concurrency.py`
- `tests/test_controller_pool_fallback.py`
- `tests/test_multi_device_integration.py` (may need test fixture setup)
- Add boot timeout test to `tests/test_resolution_protocol.py`

### Step 2: `clear_text` implementation (~1 hour)

1. Add `select_all_and_delete()` to `server/device/idb.py`
2. Add `clear_text()` to `server/device/controller.py`
3. Add `ClearTextRequest` to `server/models.py`
4. Add `POST /ui/clear` to `server/api/device.py`
5. Add `clear_text` tool to `mcp/src/index.ts`
6. Add tests for clear_text
7. Run test suite

### Step 3: `children_of` hierarchy query (~1.5 hours)

1. Update `server/device/ui_elements.py` — add `find_children_of()` and preserve parent info during parsing
2. Update `GET /api/v1/device/ui` in `server/api/device.py` — add `children_of` query param
3. Update `get_ui_tree` MCP tool — add `children_of` parameter
4. Add tests
5. Run test suite

### Step 4: MCP guide and tool description updates (~1 hour)

1. Add REST path mapping table to `GUIDE_CONTENT`
2. Update `tap_element` tool description
3. Update device control workflow section
4. Verify `os_version` documentation is present on `resolve_device` and `ensure_devices`

### Step 5: Final verification (~15 min)

```bash
.venv/bin/pytest -v
```

All tests must pass. Count should be ~495-500 (470 existing + 25-30 new).

---

## Deferred (Not 4b-delta)

Per the multi-device plan:

- **Off-screen element enumeration** — Platform limitation, idb only returns visible accessibility tree. Scroll-and-collect is the correct pattern.
- **Dismiss-all-modals helper** — App-specific, not generalizable. Better addressed by test DSL layer.

---

## Success Criteria

- All existing 470+ tests pass with no regressions
- New concurrency tests confirm no double-booking under `asyncio.gather`
- Controller fallback tests verify pool failure is invisible to callers
- `clear_text` works end-to-end: MCP tool → HTTP API → idb key sequence
- `children_of` parameter returns scoped element subsets
- MCP guide includes complete tool→REST path mapping
- `tap_element` description mentions `element_type` for narrowing ambiguous results
- Agent workflow section recommends `resolve_device` and `clear_text`
