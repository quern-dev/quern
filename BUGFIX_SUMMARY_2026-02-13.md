# Bug Fixes - February 13, 2026

## Issues Discovered During Field Testing

During the first field trial of the Geocaching app automation, two critical issues emerged that prevented Python HTTP API calls from working reliably, while manual MCP tool calls worked fine.

---

## Issue 1: UIElement Pydantic ValidationError ✅ FIXED

### Symptoms
- `ValidationError: 3 validation errors for UIElement` appearing in logs
- Errors on `type`, `role`, `role_description` fields: "Input should be a valid string [type=string_type, input_value=None]"
- `wait_for_element` would fail silently
- `tap_element` would return incorrect coordinates or fail
- Appeared random/intermittent (depended on which UI elements were present)

### Root Cause
The idb `describe-all` command sometimes returns `None` values for string fields (`type`, `role`, `role_description`, `custom_actions`). The parsing code used `.get("field", "default")` which handles *missing* keys but not *None* values.

```python
# Before (broken):
type=item.get("type", "Unknown")  # If type=None, passes None → ValidationError!

# After (fixed):
type_val = item.get("type")
if type_val is None or type_val == "":
    type_val = "Unknown"
```

### Fix Location
**File:** `server/device/ui_elements.py`
**Lines:** 37-50 (in `parse_elements()` function)

### Implementation
Explicit None handling for all string fields:
- `type` → defaults to "Unknown" if None or empty
- `role` → defaults to "" if None
- `role_description` → defaults to "" if None
- `custom_actions` → defaults to [] if None

### Testing
- ✅ All 470 tests pass
- ✅ No more ValidationErrors in production logs
- ✅ Full login/logout test completes successfully

---

## Issue 2: Tap Returns Success But Button Doesn't Respond ✅ FIXED

### Symptoms
- `tap_element` returns `{"status": "ok"}` with correct coordinates
- Button/element never responds to tap (stays visible, action doesn't trigger)
- Particularly noticeable with dialog confirmation buttons (e.g., "OK" button)
- Server-side `wait_for_element` with `condition: "not_exists"` times out after 30+ seconds
- Element remains at same position with same state throughout polling

### Root Cause
UI elements appear immediately in the accessibility tree but undergo animations/transitions before they're ready to receive tap input. The script was tapping **immediately** (0.0 seconds, 1 poll) after detecting elements, before animations completed.

**Example from logs:**
```
[DEBUG] wait_for_element: element found (elapsed: 0.0s, polls: 1)
[DEBUG] tap_element: tapped OK button at (268.67, 491.33)
[DEBUG] wait_for_element: button still present (elapsed: 30.2s, polls: 46)
```

### Solution: Adaptive Stability Check

Instead of a hardcoded delay, we implemented a smart stability check that:
1. Gets initial element position/bounds
2. Waits 100ms
3. Re-fetches UI and checks if element moved
4. **If position unchanged:** Tap immediately (fast path)
5. **If position changed:** Element is animating, wait 300ms more and get final position
6. Tap at final stable position

### Fix Location
**File:** `server/device/controller.py`
**Method:** `tap_element()` (lines ~378-405)

### Implementation Details

```python
# Get initial position
initial_frame = el.frame
await asyncio.sleep(0.1)

# Re-fetch and check for movement
elements_check, _ = await self.get_ui_elements(resolved)
matches_check = find_element(elements_check, label=label, identifier=identifier, element_type=element_type)

if matches_check:
    current_frame = matches_check[0].frame
    if current_frame != initial_frame:
        # Element is animating!
        logger.debug("Element animating, waiting for stability: %s -> %s", initial_frame, current_frame)
        await asyncio.sleep(0.3)
        # Get final position
        elements_final, _ = await self.get_ui_elements(resolved)
        matches_final = find_element(elements_final, label=label, identifier=identifier, element_type=element_type)
        if matches_final:
            cx, cy = get_center(matches_final[0])

await self.idb.tap(resolved, cx, cy)
```

### Benefits
- **Adaptive timing:** Fast elements tapped after ~100ms, animated elements wait longer
- **Accurate coordinates:** Always uses final stable position, even if element moved
- **No assumptions:** Checks actual state instead of guessing animation duration
- **Observable:** Logs when animations detected for debugging
- **Efficient:** Doesn't wait unnecessarily for already-stable elements

### Testing
- ✅ Full login/logout test completes successfully
- ✅ All dialog taps work (server picker, onboarding, permissions, logout confirmation)
- ✅ No timeouts or stuck dialogs
- ✅ Test runs from start to finish without manual intervention

### Manual Verification
Tested with 1-second hardcoded delay first to confirm diagnosis:
```bash
# With 1s delay: ✅ OK button dismissed successfully
# With 0s delay: ❌ OK button stuck visible for 30+ seconds
# With stability check: ✅ OK button dismissed successfully (~100-400ms adaptive)
```

---

## Future Enhancement Ideas (Added as Comments)

The following enhancements were documented as code comments for future consideration:

### 1. Post-Tap Verification
```python
# tap_element(..., verify_disappears=True)
# After tapping, verify the expected outcome occurred
# Useful for: confirming dialogs dismissed, buttons triggered actions
```

### 2. Retry Logic
```python
# tap_element(..., retry_attempts=3)
# If tap doesn't work, retry with fresh coordinates
# Useful for: handling transient failures, UI race conditions
```

### 3. Configurable Stability Timing
```python
# tap_element(..., stability_check_ms=150)
# Allow tuning the stability check interval
# Useful for: apps with very fast/slow animations
```

**Location:** `server/device/controller.py` - `tap_element()` method
**Lines:** ~350-365 (function signature), ~405-420 (implementation sketches)

---

## Minor Issue: Occasional 422 Validation Error

### Symptoms
Occasional `422 Unprocessable Entity` on `wait-for-element` requests. Does not break test flow - subsequent requests succeed.

### Probable Cause
Invalid request body from Python script (e.g., wrong condition enum value, missing required field, type mismatch).

### Status
**Not blocking** - test completes successfully despite occasional 422s. Can investigate further if needed by capturing the actual request body and validation error details.

---

## Test Results

### Before Fixes
- ❌ ValidationErrors in logs
- ❌ Taps return success but don't work
- ❌ Test fails at server picker (OK button never dismisses)
- ❌ Manual intervention required

### After Fixes
- ✅ No ValidationErrors
- ✅ All taps work reliably
- ✅ Full test completes: launch → login → onboarding → permissions → navigate → logout → landing
- ✅ **TEST COMPLETED SUCCESSFULLY** message
- ✅ Zero manual intervention required

### Performance
- Total test duration: ~45-60 seconds
- Tap latency: 100-400ms (adaptive based on animations)
- API calls: Significantly reduced vs. original estimates

---

## Files Modified

1. **`server/device/ui_elements.py`**
   - Fixed None handling in `parse_elements()` function
   - Lines: 37-50

2. **`server/device/controller.py`**
   - Added stability check to `tap_element()` method
   - Added documentation for future enhancements
   - Lines: ~343-420

3. **`tests/test_device_api.py`**
   - Updated one test expectation for new `max_elements` parameter
   - Line: 406

---

## Deployment

### Server Restart Required
```bash
# Stop old server
pkill -f "python.*server.main"

# Start with fixes
.venv/bin/python -m server.main start
```

### Verification
```bash
# Check logs for no ValidationErrors
tail -f /Users/jerimiah/.quern/server.log | grep -i error

# Run full test suite
.venv/bin/pytest -v  # All 470 tests should pass

# Run field trial
cd /Volumes/sourcecode/ios/iphone-intro/quern
python3 test_login_logout_v2.py  # Should complete successfully
```

---

## Lessons Learned

1. **idb output is not always well-formed** - Always handle None values explicitly for Pydantic models
2. **UI animations are real** - Never tap immediately after detection, always verify stability
3. **Adaptive timing beats hardcoded delays** - Check actual state instead of guessing durations
4. **Field testing is essential** - Manual MCP calls hid timing issues that only appeared in scripts
5. **Logging is critical** - The stability check logs will help diagnose future timing issues

---

## Related Documents

- Original issue report: User conversation (February 13, 2026)
- API spec: `docs/quern-api-additions-spec.md`
- Implementation summary: `IMPLEMENTATION_SUMMARY.md`
- Architecture: `docs/phase3-architecture.md`
