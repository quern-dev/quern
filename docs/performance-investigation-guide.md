# Performance Investigation Guide

## Overview

This document describes the comprehensive performance instrumentation added to diagnose the 17-19 second delay when tapping the Profile button during parallel iOS simulator testing.

## What Was Added

### Server-Side Instrumentation

All timing logs use `[PERF]` prefix and `time.perf_counter()` for high-resolution millisecond timing.

#### 1. **server/device/controller.py**
- `get_ui_elements()` - Tracks cache hits/misses, idb calls, parsing time
- `tap_element()` - Tracks element search, stability checks, tap execution
- `wait_for_element()` - Tracks polling loops and element detection
- Enhanced `get_cache_stats()` - Now includes per-device cache ages

#### 2. **server/device/idb.py**
- `_run()` - Tracks subprocess spawn, communication, total time
- `describe_all()` - Tracks idb subprocess, JSON parsing, tree flattening, container probing
- `tap()` - Tracks tap command execution

#### 3. **server/device/ui_elements.py**
- `parse_elements()` - Tracks parsing time, elements parsed vs skipped

#### 4. **server/api/device.py**
- All device endpoints now have timing instrumentation
- Key endpoints: `/ui/tap-element`, `/ui/tap`, `/ui`, `/ui/wait-for-element`

### Analysis Tools

#### **tools/analyze_perf_logs.py**
Parses `[PERF]` logs and generates:
- Operation-by-operation timeline with cumulative timing
- Event type statistics (counts, averages, totals)
- Slowest operations (top 10 bottlenecks)
- Cache effectiveness analysis

## How to Use

### Step 1: Run Your Test with Instrumentation

Make sure the quern-debug-server is running with the instrumented code:

```bash
# Start the server (if not already running)
.venv/bin/python -m server.main start

# Or restart to ensure new code is loaded
.venv/bin/python -m server.main stop
.venv/bin/python -m server.main start
```

### Step 2: Capture Logs During Test Execution

Run your test and capture ALL logs to a file:

```bash
# Option 1: Redirect test output
python test_login_logout_v2_parallel.py --device "iPhone 16 Pro Max" 2>&1 | tee perf_analysis.log

# Option 2: Monitor server logs (if server is in daemon mode)
tail -f ~/.quern/server.log | grep PERF > perf_analysis.log

# Option 3: Capture both server AND test logs
# Terminal 1: Run test
python test.py 2>&1 > test_output.log

# Terminal 2: Capture server logs
tail -f ~/.quern/server.log > server_output.log

# Then combine:
cat test_output.log server_output.log | grep PERF > perf_analysis.log
```

### Step 3: Analyze the Timeline

```bash
python tools/analyze_perf_logs.py perf_analysis.log
```

This will output:
1. **Timeline Analysis** - Operation-by-operation breakdown with cumulative timing
2. **Summary Statistics** - Event counts, averages, totals
3. **Slowest Operations** - Top 10 bottlenecks
4. **Cache Effectiveness** - Hit rate and cache behavior

### Step 4: Interpret the Results

Look for these patterns in the output:

#### Cache Behavior
```
⚡ CACHE_HIT           +2.3ms    [2.3ms]    age=150.2ms, elements=523
❌ CACHE_EXPIRED      +0.1ms    [2.4ms]    age=350.5ms > ttl=300.0ms
```
- **High cache hit rate (>50%)** with fast fetches → Cache is working well
- **Low cache hit rate (<20%)** → Cache TTL may be too short or cache is being invalidated

#### Subprocess Timing
```
🔧 SUBPROCESS_SPAWN   +5.2ms    [5.2ms]
⏳ SUBPROCESS_WAIT    +1200.5ms [1205.7ms]  subprocess communicate: 1200.5ms, stdout=125000 bytes
```
- **Spawn time > 50ms** → Process creation overhead (unusual, may indicate system load)
- **Wait time > 1000ms** → idb subprocess is slow (this is the likely bottleneck)

#### Parsing Time
```
📄 JSON_PARSE        +15.2ms   [1220.9ms]  JSON parsed 523 items
🔍 PARSE_ELEMENTS    +45.8ms   [1266.7ms]  parsed=5, skipped=518
```
- **Parse time > 100ms** → Pydantic validation overhead (unlikely with filtering)
- **Large skip count** → Filtering is working correctly

#### Stability Checks
```
🎯 STABILITY_CHECK   +420.5ms  [1687.2ms]  stability check complete
```
- **Stability check > 400ms** → Multiple UI tree fetches + sleep delays
- **Skip stability check** to test if this is causing the delay

### Expected Insights

After analysis, you should be able to answer:

1. **Where are the 17-19 seconds spent?**
   - Look at the TOTAL TIME for the tap_element operation
   - Identify which sub-operations contribute the most time

2. **Is it the idb subprocess?**
   - Check SUBPROCESS_WAIT times
   - If consistently >1000ms per call, idb is the bottleneck

3. **Is it stability checks?**
   - Check STABILITY_CHECK total time
   - If ~400-600ms and happening multiple times, this adds up

4. **Is it cache misses?**
   - Check cache hit rate
   - If <20%, every operation is doing a full idb fetch

5. **Is it container probing?**
   - Check for "probing N containers" messages
   - If probing >10 containers per fetch, this adds overhead

6. **Is it parsing?**
   - Check PARSE_ELEMENTS times
   - Should be <50ms with filtering, >100ms without

## Client-Side Instrumentation (Optional)

For complete end-to-end analysis, add timing to your test script:

```python
import time

def navigate_to_profile(self):
    """Navigate to Profile tab."""
    start = time.perf_counter()
    print(f"[TEST PERF] navigate_to_profile START")

    # Check if on map
    t1 = time.perf_counter()
    print(f"[TEST PERF] checking if on map screen (+{(t1-start)*1000:.1f}ms)")

    if not self.is_on_map_screen():
        # navigation logic
        pass

    t2 = time.perf_counter()
    print(f"[TEST PERF] confirmed on map screen (+{(t2-start)*1000:.1f}ms)")

    # Tap Profile button
    t3 = time.perf_counter()
    print(f"[TEST PERF] tapping Profile button (+{(t3-start)*1000:.1f}ms)")

    result = self.tap_element(identifier="_Profile button in tab bar", skip_stability_check=True)

    t4 = time.perf_counter()
    print(f"[TEST PERF] tap_element returned: {result} (+{(t4-t3)*1000:.1f}ms)")

    end = time.perf_counter()
    print(f"[TEST PERF] navigate_to_profile COMPLETE: total={(end-start)*1000:.1f}ms")
```

This will show if there are delays on the client side (network, serialization, etc.).

## Next Steps

Once you've identified the bottleneck:

1. **If idb subprocess is slow (>1000ms per call)**:
   - Profile idb itself (is it the iOS simulator being slow?)
   - Check system load (CPU, memory)
   - Try on a different device to rule out simulator-specific issues

2. **If stability checks are the issue (>400ms per check)**:
   - Use `skip_stability_check=True` for static elements (tab bars)
   - Reduce stability check sleep times
   - Consider using a different detection strategy

3. **If cache is ineffective (<20% hit rate)**:
   - Increase cache TTL (currently 300ms)
   - Review when cache is being invalidated
   - Consider different caching strategies

4. **If container probing is slow**:
   - Review which containers are being probed
   - Consider reducing probe resolution
   - Disable probing for specific container types

5. **If parsing is slow (>100ms)**:
   - Verify filtering is working correctly
   - Profile Pydantic validation overhead
   - Consider lazy parsing strategies

## Example Timeline Output

```
================================================================================
PERFORMANCE TIMELINE ANALYSIS
================================================================================

Found 1 major operation(s)

────────────────────────────────────────────────────────────────────────────────
Operation #1: tap_element
Started: 2024-02-13 10:23:45.123
Details: START (label=None, id=_Profile button in tab bar, skip_stability=True)
────────────────────────────────────────────────────────────────────────────────

⏱️  TOTAL TIME: 1245.3ms

  • DETAIL              +0.5ms     [0.5ms]  fetching UI elements
    ⚡ CACHE_HIT        +2.1ms     [2.6ms]  age=120.3ms, elements=523
  • DETAIL              +0.2ms     [2.8ms]  got 5 elements
  • DETAIL              +0.1ms     [2.9ms]  found 1 matches
  • DETAIL              +0.1ms     [3.0ms]  executing tap at (375,812)
    📱 IDB_DONE         +1240.5ms  [1243.5ms]  subprocess returned
  ✓ COMPLETE            +1.8ms     [1245.3ms]  total=1245.3ms

================================================================================
SUMMARY STATISTICS
================================================================================

Slowest Operations (top 10):
  1240.5ms  idb.tap                     SUBPROCESS_WAIT      subprocess communicate: 1240.5ms, stdout=0 bytes
     2.1ms  get_ui_elements             CACHE_HIT            age=120.3ms, elements=523
```

This would clearly show that **the idb tap subprocess is taking 1.24 seconds**, which is the real bottleneck.

## Important Notes

- All `[PERF]` logs are at INFO level, so they'll appear in standard server logs
- The instrumentation has minimal performance overhead (<1% in most cases)
- After investigation, you can reduce/remove instrumentation if desired
- Keep the analyzer tool - it's useful for future performance debugging

## Troubleshooting

**Q: I don't see any [PERF] logs**
- Make sure the server is running the new instrumented code
- Check the log level is set to INFO or lower
- Verify you're looking at the correct log file

**Q: The analyzer says "No [PERF] events found"**
- Make sure you're filtering for [PERF] in the log file
- Check that the log format matches the expected pattern
- Try running the analyzer on the raw server logs

**Q: I see [PERF] logs but they're incomplete**
- Some operations may not have completed
- Check for errors or exceptions that interrupted execution
- Capture a longer test run to get complete operations
