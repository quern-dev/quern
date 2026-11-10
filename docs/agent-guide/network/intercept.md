# Intercept-Modify-Release for Testing Edge Cases

*`quern://guide/network/intercept` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

Test error handling without breaking the backend:

1. Set up an intercept pattern matching the target endpoint (e.g., `~d api.example.com & ~m POST`)
2. Trigger the action in the app
3. Wait for the request to be held with `list_held_flows` (use the timeout parameter)
4. Release the flow with modifications to the request — its headers, body, URL or method — or unchanged
5. Observe how the app handles the result via `get_screen_summary` and `query_logs`

`release_flow` changes the request, not the response. To see how the app handles an error status or a malformed body, mock the response with `set_mock` (`quern://guide/network/mocks`); to see how it copes with a slow network, hold the flow before releasing it, or on a physical iPhone set a network condition (`quern://guide/performance`).
