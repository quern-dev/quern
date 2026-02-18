# Quern Agent Guide

**For**: AI agents using Quern MCP server for iOS debugging and testing
**Last Updated**: February 15, 2026
**Status**: Living document based on real-world usage

---

## Philosophy: Eyes, Ears, and Fingers

Quern is designed to be your **sensory and motor interface** to iOS apps. Like humans don't consciously think "rotate eyeballs 15 degrees left" - they just **look** - you shouldn't think about tool mechanics. The tools should feel natural and automatic.

**Your capabilities through Quern**:
- 👀 **Eyes**: See UI state, network traffic, logs
- 👂 **Ears**: Hear app events, crashes, errors
- 👊 **Fingers**: Control UI, intercept network, trigger actions

The more you use these tools, the better you'll get at using them together.

---

## Quick Start Checklist

Every Quern session should start with:

```
1. ensure_server()              # Start/verify Quern is running
2. resolve_device(...)          # Get a device to work with
3. get_screen_summary()         # See what's on screen
4. proxy_status()               # Check if network capture is active
```

From there, use the right tool for your task.

---

## Core Principles

### 1. **Use Structured Data, Not Visual Parsing**

❌ **Don't**:
```python
screenshot()  # Then try to parse pixels?
```

✅ **Do**:
```python
get_screen_summary()  # Curated text description
get_ui_tree()         # Full accessibility hierarchy
```

**Why**: You're an AI agent. Structured data (JSON, text) is your native format. Screenshots are for humans.

**Exception**: Screenshots are great for documentation, bug reports, or showing state to humans.

---

### 2. **Prefer Accessibility Over Coordinates**

❌ **Don't**:
```python
tap(x=300, y=450)  # Brittle, breaks on different devices/orientations
```

✅ **Do**:
```python
tap_element(label="Submit", element_type="Button")
```

**Why**:
- Works across screen sizes
- Survives UI layout changes
- Self-documenting (readable intent)

**When coordinates are OK**: Gestures that aren't tied to specific elements (swipe to refresh, drag to reorder).

---

### 3. **Summarize First, Drill Down Second**

❌ **Don't**:
```python
all_logs = query_logs(limit=10000)        # Overwhelming
all_flows = query_flows(limit=1000)       # Too much data
tree = get_ui_tree()                      # 500+ elements
```

✅ **Do**:
```python
log_summary = get_log_summary(window="5m")     # Overview
flow_summary = get_flow_summary(window="5m")   # By-host aggregation
screen_summary = get_screen_summary(max_elements=20)  # Curated elements

# Then drill down based on what you learned
specific_logs = query_logs(search="Error", limit=50)
failed_flows = query_flows(status_min=400, limit=10)
full_tree = get_ui_tree(children_of="Login Form")
```

**Why**: Summaries are designed for agent consumption - they give you context to make smart decisions about what to investigate.

---

### 4. **Verify State Before Acting**

❌ **Don't**:
```python
# Assume proxy is running
query_flows(method="POST")  # Might be empty!

# Assume element exists
tap_element(label="Submit")  # Might fail!
```

✅ **Do**:
```python
# Check proxy state
status = proxy_status()
if status["status"] != "running":
    start_proxy()

# Check element exists
summary = get_screen_summary()
if "Submit" in summary:
    tap_element(label="Submit")
else:
    # Element not visible, handle accordingly
```

**Why**: State can change. Always verify before acting.

---

### 5. **Use Server-Side Waiting, Not Client-Side Polling**

❌ **Don't**:
```python
# Client-side polling loop
for i in range(10):
    tree = get_ui_tree()
    if find_element(tree, "Success"):
        break
    time.sleep(1)
```

✅ **Do**:
```python
result = wait_for_element(
    label="Success",
    condition="exists",
    timeout=10
)
if result["matched"]:
    # Element appeared
```

**Why**:
- Fewer API round-trips
- More efficient (server polls at sub-second intervals)
- Built-in timeout handling
- Returns immediately on match

---

### 6. **Filter Aggressively**

Logs, network flows, and UI trees can be **huge**. Always filter to what you need.

**Logs**:
```python
# Too broad
all_logs = query_logs(limit=1000)

# Better
errors = query_logs(level="error", limit=50)
app_logs = query_logs(process="MyApp", search="timeout")
recent = query_logs(since="2026-02-15T10:00:00Z")
```

**Flows**:
```python
# Too broad
all_flows = query_flows(limit=500)

# Better
api_calls = query_flows(path_contains="/api/", limit=20)
failures = query_flows(status_min=400)
slow_requests = query_flows(...)  # Use flow_summary to find slow patterns first
```

**UI Tree**:
```python
# Too broad (500+ elements)
full_tree = get_ui_tree()

# Better
summary = get_screen_summary(max_elements=20)  # Curated
login_form = get_ui_tree(children_of="Login Form")  # Scoped
```

---

## Common Workflows

### Workflow 1: Debugging Network Issues

```python
# 1. Ensure proxy is running
ensure_server()
status = proxy_status()
if status["status"] != "running":
    start_proxy()

# 2. Get baseline
summary = get_flow_summary(window="1m")  # See current traffic

# 3. Trigger the issue
get_screen_summary()
tap_element(label="Submit")

# 4. Query for relevant flows
flows = query_flows(
    method="POST",
    path_contains="/api/submit",
    limit=5
)

# 5. Examine specific flow
if flows["flows"]:
    detail = get_flow_detail(flow_id=flows["flows"][0]["id"])
    # Check request/response bodies, headers, status
```

**Key insight**: Start with summary, trigger action, drill down to specific flows.

---

### Workflow 2: Debugging UI Issues

```python
# 1. See current state
summary = get_screen_summary()

# 2. Trigger the issue
tap_element(label="Delete")

# 3. Check new state
after_summary = get_screen_summary()

# 4. If unexpected, get full tree for details
if "Confirm" not in after_summary:
    tree = get_ui_tree()
    # Inspect full hierarchy to find what actually appeared
```

**Key insight**: Use summary for quick checks, full tree when you need details.

---

### Workflow 3: Debugging Crashes

```python
# 1. Check recent crashes
crashes = get_latest_crash(limit=5)

if crashes["crashes"]:
    latest = crashes["crashes"][0]

    # 2. Get logs around crash time
    crash_time = latest["timestamp"]
    logs = query_logs(
        since=crash_time - 30,  # 30 seconds before
        until=crash_time,
        level="error"
    )

    # 3. Check network activity before crash
    flows = query_flows(...)  # Around crash time

    # Correlate: crash + logs + network = full picture
```

**Key insight**: Crashes leave traces in multiple places. Correlate logs + network + crash report.

---

### Workflow 4: Reproducing Bug Reports

```python
# 1. Navigate to starting screen
navigate_to_screen("Profile")

# 2. Perform reported steps
tap_element(label="Settings")
tap_element(label="Edit Profile")
type_text("New Name")
tap_element(label="Save")

# 3. Capture state at each step
screenshots = []
logs = []

# Take snapshot
screenshots.append(take_screenshot())
logs.append(query_logs(since="5s ago"))

# 4. Check for expected vs actual
summary = get_screen_summary()
if "Success" not in summary:
    # Bug reproduced! Capture diagnostic bundle:
    # - Screenshots
    # - Logs
    # - Network flows
    # - UI tree
```

**Key insight**: Capture state at each step for a complete reproduction.

---

## Tool Selection Guide

**"I need to see what's on screen"**
- Quick overview → `get_screen_summary()`
- Full detail → `get_ui_tree()`
- Visual for humans → `take_screenshot()`

**"I need to tap/interact with UI"**
- Known element → `tap_element(label="...", element_type="...")`
- Coordinates (rare) → `tap(x, y)`
- Gesture → `swipe(start_x, start_y, end_x, end_y)`
- Text input → Focus element, then `type_text("...")`

**"I need to see network traffic"**
- Overview → `get_flow_summary(window="5m")`
- Specific requests → `query_flows(method="...", path_contains="...")`
- Full detail → `get_flow_detail(flow_id="...")`
- Modify traffic → `set_intercept(pattern="...")` + `release_flow(modifications=...)`
- Mock responses → `set_mock(pattern="...", status_code=200, body="...")`

**"I need to see logs"**
- Recent activity → `tail_logs(count=50)`
- Overview → `get_log_summary(window="5m")`
- Specific search → `query_logs(search="...", level="...")`
- Errors only → `get_errors(limit=50)`

**"I need to control the device"**
- Boot → `boot_device(name="...")` or `resolve_device(auto_boot=True)`
- Install app → `install_app(app_path="...")`
- Launch app → `launch_app(bundle_id="...")`
- Screenshot → `take_screenshot()`
- Location → `set_location(lat=..., lon=...)`
- Permissions → `grant_permission(bundle_id="...", permission="...")`

---

## Advanced Patterns

### Pattern 1: Correlation (The Superpower)

Humans struggle to correlate millisecond-level timing across logs, network, and UI. You don't.

**Example**: Finding the exact moment a request fails
```python
# Trigger action
tap_element(label="Submit")
timestamp = time.time()

# Get everything that happened in the next 2 seconds
logs = query_logs(since=timestamp, until=timestamp+2)
flows = query_flows(...)  # Filter by recent
ui_state_after = get_screen_summary()

# Correlate
for flow in flows:
    for log in logs:
        if abs(flow["timestamp"] - log["timestamp"]) < 0.1:  # Within 100ms
            # These are related!
```

**Why this matters**: The 26ms gap between POST and DELETE in today's debugging session? You'll catch these patterns instantly.

---

### Pattern 2: Intercept-Modify-Release for Testing Edge Cases

Test error handling without breaking the backend:

```python
# Set up intercept
set_intercept(pattern="~d api.example.com & ~m POST")

# Trigger action
tap_element(label="Submit")

# Wait for request to be held
held = list_held_flows(timeout=5)

if held["flows"]:
    # Release with 500 error instead of real response
    release_flow(
        flow_id=held["flows"][0]["id"],
        modifications={
            "status_code": 500,
            "body": '{"error": "Server error"}'
        }
    )

    # See how app handles it
    summary = get_screen_summary()
    logs = query_logs(search="error", limit=10)
```

**Use case**: Test error handling, slow networks, malformed responses.

---

### Pattern 3: Mock for Deterministic Testing

Create reliable test scenarios:

```python
# Mock a specific endpoint
set_mock(
    pattern="~d api.example.com & ~u /users/me",
    status_code=200,
    body='{"name": "Test User", "id": "123"}'
)

# Now every request to /users/me gets this response
# Test flows that depend on user data
```

**Use case**: Onboarding flows, user-specific features, consistent test data.

---

### Pattern 4: Device Pool for Parallel Testing

```python
# Claim multiple devices
devices = ensure_devices(
    count=3,
    name="iPhone 16 Pro",
    session_id="my-session"
)

# Run tests in parallel on each
for device in devices:
    # Each device is isolated
    # Run different test scenarios simultaneously

# Clean up
for device in devices:
    release_device(udid=device["udid"], session_id="my-session")
```

**Use case**: Faster test execution, testing on multiple OS versions.

---

## Common Mistakes

### Mistake 1: Not Calling `ensure_server` First

**Problem**: Tools fail with connection errors.

**Solution**: Always start with `ensure_server()`.

---

### Mistake 2: Using Screenshots to Understand UI State

**Problem**: You try to parse pixels or describe images.

**Solution**: Use `get_screen_summary()` or `get_ui_tree()` - they return structured text.

---

### Mistake 3: Forgetting Element Type When Label is Ambiguous

**Problem**: `tap_element(label="Cancel")` matches a StaticText instead of the Button.

**Solution**: Always specify type when label might not be unique:
```python
tap_element(label="Cancel", element_type="Button")
```

---

### Mistake 4: Not Filtering Logs/Flows

**Problem**: Query returns 10,000 logs, overwhelming context.

**Solution**: Always filter:
```python
query_logs(process="MyApp", level="error", limit=50)
```

---

### Mistake 5: Hardcoding Device UDIDs

**Problem**: Scripts break when run on different machines.

**Solution**: Use device resolution:
```python
resolve_device(name="iPhone 16 Pro", auto_boot=True)
```

---

### Mistake 6: Client-Side Polling Instead of Server-Side Waiting

**Problem**: Slow, wasteful API calls in a loop.

**Solution**: Use `wait_for_element(condition="exists", timeout=10)`.

---

## Performance Tips

### Tip 1: Use Summaries Before Full Queries

Summaries are cheap and curated. Use them to decide what to investigate:
```python
summary = get_flow_summary()  # Fast
# See errors in summary for api.example.com
flows = query_flows(host="api.example.com", status_min=400)  # Targeted
```

---

### Tip 2: Limit Result Counts

Don't fetch more than you need:
```python
query_logs(limit=50)  # Not limit=10000
```

---

### Tip 3: Use Cursors for Pagination

If you need to process large result sets:
```python
summary1 = get_log_summary()
# Later, get only new activity
summary2 = get_log_summary(since_cursor=summary1["cursor"])
```

---

### Tip 4: Scope UI Tree Queries

Don't fetch 500 elements when you need 5:
```python
get_ui_tree(children_of="Settings Panel")  # Scoped
get_screen_summary(max_elements=20)  # Curated
```

---

## Troubleshooting

### "No element found matching label"

**Causes**:
1. Element doesn't exist (check with `get_screen_summary`)
2. Label is wrong (check actual label in UI tree)
3. Multiple matches, need `element_type` to disambiguate

**Solution**:
```python
summary = get_screen_summary()  # See what's actually there
tree = get_ui_tree()  # Get exact labels
tap_element(label="Exact Label", element_type="Button")
```

---

### "Proxy not running"

**Cause**: Proxy crashed or wasn't started.

**Solution**:
```python
status = proxy_status()
if status["status"] != "running":
    start_proxy()
```

---

### "No flows captured"

**Causes**:
1. Proxy not running
2. Device not configured to use proxy
3. Traffic is HTTPS pinned (can't be intercepted)

**Solution**:
```python
# 1. Check proxy status
proxy_status()

# 2. Get setup instructions
guide = proxy_setup_guide()
# Follow instructions to configure device

# 3. Check if traffic is visible at all
get_flow_summary()  # Any traffic?
```

---

### "Wait for element timed out"

**Causes**:
1. Element never appeared (bug or wrong expectation)
2. Timeout too short
3. Element has different label than expected

**Solution**:
```python
# Try longer timeout
wait_for_element(label="Success", timeout=30)

# Or check what actually appeared
summary = get_screen_summary()
```

---

## Future Features (Roadmap Preview)

These patterns will become available as Quern evolves:

### App Graph (Coming in Phase 3.1)
Navigate by intent, not manual steps:
```python
# Instead of manual navigation
tap_element(label="Profile")
tap_element(label="Settings")
tap_element(label="Edit Profile")

# Future: Graph-based navigation
navigate_to(screen="Edit Profile")  # Quern figures out the path
```

### Test DSL (Coming in Phase 3.2)
Write tests in natural language:
```quern
test "delete post preserves on cancel":
  navigate_to "post detail"
  tap "Delete & re-draft"
  verify "compose editor open"
  tap "Cancel"
  verify "original post exists"
```

### Self-Validation (Coming in Phase 3.3)
Agents verify their own code changes:
```python
# After making code change
result = quern_run(
    prompt="Verify the delete & redraft flow works",
    device="iPhone 16 Pro"
)

if result["status"] == "pass":
    # Change validated!
else:
    # Fix based on diagnostic bundle
```

---

## Summary: The Quern Mindset

1. **Think in structured data**, not visuals
2. **Verify state before acting**
3. **Summarize first, drill down second**
4. **Filter aggressively** (logs, flows, UI)
5. **Use accessibility over coordinates**
6. **Correlate across sources** (logs + network + UI = full picture)
7. **Let the server wait**, don't poll client-side

The more you use Quern, the more natural it becomes. These tools are your eyes, ears, and fingers. Use them like part of your body.

---

**Next**: See `quick-reference.md` for a scannable cheat sheet of common patterns.
