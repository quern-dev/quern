# Network Debugging Patterns

Quern's proxy isn't just for watching traffic — it can mock responses, intercept and modify live requests, replay captured traffic, and give you structured summaries. These patterns work the same regardless of whether traffic comes from an iOS simulator, Android emulator, or physical device.

## Mocking Responses

Replace real API responses with synthetic ones. Useful for testing error states, empty data, slow responses, or APIs that don't exist yet.

### Basic Mock

```
set_mock(
    pattern="~d api.example.com & ~u /users",
    status_code=200,
    body='{"users": []}',
    headers={"Content-Type": "application/json"}
)
```

Every request matching the pattern gets the mock response immediately — the real server is never contacted.

### Filter Syntax

Mocks use mitmproxy's filter syntax. The most useful operators:

| Operator | Meaning | Example |
|---|---|---|
| `~d` | Domain | `~d api.example.com` |
| `~u` | URL (path + query) | `~u /api/v1/users` |
| `~m` | HTTP method | `~m POST` |
| `~c` | Status code | `~c 500` (for intercepts) |
| `~b` | Body contains | `~b "error"` |
| `&` | AND | `~d api.example.com & ~m POST` |
| `\|` | OR | `~d api.example.com \| ~d api.backup.com` |
| `!` | NOT | `!~d analytics.example.com` |

**Note:** `~p` (path) does not exist in mitmproxy's filter syntax, despite what you might expect. Use `~u` for path matching — it matches against the full URL path.

### Managing Mocks

```
list_mocks          # See all active mocks
update_mock(...)    # Modify an existing mock
clear_mocks         # Remove all mocks
```

### Mock Priority

When multiple mocks match a request, the most recently added mock wins. If a mock and an intercept both match, the mock takes priority — the request never reaches the intercept.

## Intercepting Live Traffic

Hold a request in-flight, inspect it, optionally modify it, then release it. This is the debugger breakpoint equivalent for network traffic.

### Setting Up an Intercept

```
set_intercept(pattern="~d api.example.com & ~m POST")
```

Now every POST to `api.example.com` will be held. The app's request hangs until you release it.

### Waiting for Intercepted Flows

```
list_held_flows(timeout=30)
```

This long-polls until a matching flow is intercepted (or the timeout expires). Returns the full request details — method, URL, headers, body.

### Releasing a Flow

Release unchanged (pass through to server):
```
release_flow(flow_id="abc123")
```

Release with modifications:
```
release_flow(
    flow_id="abc123",
    modify_headers={"Authorization": "Bearer expired-token"},
    modify_body='{"injected": true}'
)
```

You can modify the method, URL, headers, and body before releasing. The modified request is what the server receives.

### Auto-Release

Held flows are automatically released after 30 seconds to prevent the app from hanging indefinitely. If you need more time, release and re-intercept.

### Clearing Intercepts

```
clear_intercept
```

Removes the intercept pattern and releases any currently held flows.

## Replaying Requests

Re-send a previously captured request, optionally with modifications.

```
replay_flow(
    flow_id="abc123",
    modify_headers={"Cache-Control": "no-cache"}
)
```

The replayed request goes through the proxy, so it appears in the flow list as a new entry. This is useful for:
- Retrying a failed request after fixing the server
- Testing idempotency (does the same POST produce the same result?)
- Comparing responses over time

## Flow Summaries

Don't read raw flows one by one. Start with the summary:

```
get_flow_summary
```

This returns a structured breakdown:
- Flows grouped by host
- Success/error counts per host
- Slow requests (> 1 second)
- Error patterns (`POST /api/users -> 500`)
- Total traffic volume

### Cursor-Based Delta Updates

The summary includes a `cursor` (timestamp-based). Pass it back to get only new flows:

```
get_flow_summary(cursor="2024-01-15T10:30:00.000Z")
```

This is critical for AI agents working in loops — each iteration only processes new traffic, keeping token usage low.

### Filtering Summaries

```
get_flow_summary(simulator_udid="...")       # One simulator's traffic
get_flow_summary(host="api.example.com")     # One host
get_flow_summary(client_ip="192.168.1.42")   # One physical device
```

## Common Patterns

### Testing Error Handling

```
# Mock a 500 error
set_mock(pattern="~d api.example.com & ~u /login", status_code=500, body='{"error": "Internal Server Error"}')

# Use the app, observe how it handles the error
# Check logs for error handling behavior
query_logs(search="login", level="error")

# Clean up
clear_mocks
```

### Simulating Slow Responses

```
# Intercept the request, wait, then release
set_intercept(pattern="~d api.example.com & ~u /feed")

# Wait for the request
list_held_flows(timeout=30)
# ... wait a few seconds ...
release_flow(flow_id="...")

# The app experienced the delay as a slow API response
```

### Debugging Authentication Flows

```
# Watch all auth-related traffic
query_flows(host="auth.example.com")

# Check for token refresh patterns
query_flows(url_contains="/token/refresh")

# If a request failed, replay it with a fresh token
replay_flow(flow_id="...", modify_headers={"Authorization": "Bearer <new-token>"})
```

### Comparing Request/Response Pairs

```
# Get the flow summary to find interesting flows
get_flow_summary(host="api.example.com")

# Drill into specific flows
get_flow_detail(flow_id="abc123")  # Full headers, body, timing
get_flow_detail(flow_id="def456")  # Compare with another flow
```

## Tips

- **Filter aggressively.** A busy app generates hundreds of flows per minute. Always filter by host, method, or status code.
- **Use summaries first.** `get_flow_summary` gives you the shape of the traffic. `query_flows` and `get_flow_detail` are for drilling in.
- **Mock early.** If you're testing against an API that's flaky or rate-limited, mock it before you start. You'll get consistent, fast responses.
- **Don't forget cleanup.** Mocks persist until cleared. An old mock from a previous debugging session can cause confusing behavior. `list_mocks` to check, `clear_mocks` to reset.
- **Intercepts block the app.** Don't set a broad intercept pattern and walk away. The app will hang on every matching request.
