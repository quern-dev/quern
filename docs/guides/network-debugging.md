# Network Debugging Patterns

Quern's proxy isn't just for watching traffic — it can fake API responses, pause requests mid-flight for inspection, and replay captured traffic. These patterns work the same regardless of whether traffic comes from an iOS simulator, Android emulator, or physical device.

## Watching Traffic

The most basic use case. Once proxy capture is set up (see the proxy setup guides for [iOS simulators](ios-proxy-simulators.md), [physical devices](ios-proxy-physical-devices.md), or [Android](android-proxy.md)):

> "Show me what API calls my app is making"
> "What network traffic happened in the last minute?"

Your agent shows you a summary grouped by host, with success/error counts, slow requests, and error patterns. From there, drill down:

> "Show me the failed requests"
> "What's going to api.example.com?"
> "Show me the full request/response for that 500 error"

### Summary-First Approach

Your agent starts with summaries, not raw traffic. This matters because a busy app generates hundreds of requests per minute, and raw flow-by-flow listing burns through context. Summaries give you the shape of the traffic; your agent drills in when something looks interesting.

Summaries also support **cursors** — your agent can ask "what's new since last time?" and get only delta updates. This keeps long debugging sessions efficient.

## Mocking Responses

Replace real API responses with fakes. Useful for testing error states, empty data, or APIs that don't exist yet.

> "Mock the /users endpoint to return an empty list"
> "Make the login API return a 500 error"
> "Mock api.example.com/config to return this JSON: {...}"

Your agent sets up the mock, and every matching request gets the fake response immediately — the real server is never contacted.

### What You Should Know

- **Mocks persist until cleared.** If you set up a mock in one debugging session and forget about it, it's still active. Ask your agent to list or clear mocks if you're seeing unexpected behavior.
- **Multiple mocks can coexist.** You can mock different endpoints simultaneously.
- **Mocks take priority over intercepts.** If both match a request, the mock wins.

### Filter Syntax

Behind the scenes, mocks use mitmproxy's filter syntax. Your agent knows this, but it's useful to know the building blocks:

| What | Filter | Example |
|---|---|---|
| Domain | `~d` | `~d api.example.com` |
| URL path | `~u` | `~u /api/v1/users` |
| HTTP method | `~m` | `~m POST` |
| Body contains | `~b` | `~b "error"` |
| Combine (AND) | `&` | `~d api.example.com & ~m POST` |

**Note:** There's no path-only filter (`~p` doesn't exist in mitmproxy). Use `~u` for path matching.

## Intercepting Live Traffic

Pause a request in-flight, inspect it, optionally modify it, then let it continue. This is the network equivalent of a debugger breakpoint.

> "Intercept all POST requests to api.example.com"

Now every matching request hangs until your agent releases it. You can:

> "Show me the intercepted request"
> "Change the auth header to an expired token and release it"
> "Just let it through unchanged"

### Be Careful

Intercepted requests block the app. Don't set a broad intercept pattern and walk away — everything matching will hang. Flows are auto-released after 30 seconds as a safety net, but it's better to be specific:

> "Intercept just the next login request" (not "intercept everything")

When you're done:

> "Clear the intercept"

## Replaying Requests

Re-send a previously captured request, optionally with modifications:

> "Replay that failed request"
> "Replay it but with a fresh auth token"

The replayed request goes through the proxy like a normal request, so it appears in the traffic list. Useful for:
- Retrying a failed request after fixing the server
- Testing idempotency
- Comparing responses over time

## Common Debugging Workflows

### "My app shows an error but I don't know why"

1. Ask your agent to show recent network errors
2. Look at the failing request's response body — what did the server actually say?
3. Cross-reference with app logs around the same timestamp

### "I want to test how my app handles a server error"

1. Ask your agent to mock the relevant endpoint with a 500 response
2. Trigger the flow in your app
3. Watch logs for how your error handling behaves
4. Clear the mock when done

### "The API response looks wrong"

1. Ask your agent for the full request/response detail
2. Compare headers, body, status code with what you expect
3. If needed, replay the request to see if it's consistent
4. Or intercept the next request, modify it, and see how the server responds

### "I need to test against a flaky API"

Mock it before you start. You'll get consistent, fast responses instead of waiting for timeouts and dealing with rate limits.

### "Which simulator is making this request?"

If you're running multiple simulators, your agent can filter traffic by simulator. Each flow is automatically tagged with its originating simulator — no app-side changes needed.

## Tips

- **Ask for summaries first, details second.** Summaries show the shape of traffic; raw flows are for drilling in.
- **Filter aggressively.** A busy app generates a lot of traffic. Always narrow by host, method, or status when possible.
- **Clear mocks when done.** Stale mocks from previous sessions cause confusing behavior.
- **Intercepts block the app.** Be specific with intercept patterns and clear them when you're done investigating.
