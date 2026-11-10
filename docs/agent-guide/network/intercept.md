# Intercept-Modify-Release for Testing Edge Cases

*`quern://guide/network/intercept` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

Test error handling without breaking the backend:

1. Check first: the server has **one** intercept pattern, and `set_intercept` replaces whatever is set. Read `proxy_status`'s `active_intercept` and `list_held_flows`, and if someone else's intercept is active, ask before replacing it.
2. Set up an intercept pattern matching the target endpoint (e.g., `~d api.example.com & ~m POST`)
3. Trigger the action in the app
4. Wait for the request to be held with `list_held_flows` (use the timeout parameter)
5. Release the flow with modifications to the request — its headers, body, URL or method — or unchanged
6. Observe how the app handles the result via `get_screen_summary` and `query_logs`
7. Clear the intercept with `clear_intercept` when done. Releasing one flow leaves the pattern in place, so later matching requests go on being held (each for up to 30 seconds); `clear_intercept` releases every held flow, so do it once you have what you need.

`release_flow` changes the request, not the response. To see how the app handles an error status or a malformed body, mock the response with `set_mock` (`quern://guide/network/mocks`); to see how it copes with a slow network, hold the flow before releasing it, or on a physical iPhone set a network condition (`quern://guide/performance`).
