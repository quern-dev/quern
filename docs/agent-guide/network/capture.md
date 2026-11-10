# Capturing traffic

*`quern://guide/network/capture` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

1. Check `proxy_status` — start the proxy if it isn't running
2. Get a baseline with `get_flow_summary` to see current traffic patterns
3. Trigger the issue (tap a button, navigate to a screen)
4. Query for relevant flows — filter by method, path, status code, or `simulator_udid`
5. Use `get_flow_detail` on the specific flow to inspect headers, request body, and response body
6. To re-send a captured request (e.g., after fixing a backend issue), use `replay_flow` with the flow ID

**Key insight**: Start with summary, trigger action, drill down to specific flows.

**Capture sessions for test fixtures**: To isolate the API calls for a specific UI action, use `start_capture_session` before the action and `stop_capture_session` after. This returns only the flows from that time window, with built-in host filtering and compact summaries. Use `exclude_hosts` to filter out analytics/SDK noise, or `hosts` to capture only specific API domains.

**Per-simulator flow filtering**: When local capture is enabled, each flow is tagged with the originating simulator's UDID. Use `simulator_udid` in `query_flows`, `get_flow_summary`, and `start_capture_session` to see only traffic from a specific simulator — essential when running parallel tests.

**Proxy modes for simulators**:

- **Local capture (recommended)**: Uses mitmproxy's macOS System Extension to transparently capture simulator traffic without configuring a system proxy. Each simulator's flows are tagged with its UDID. Check `proxy_status` — if `local_capture` is non-empty, simulator traffic is already being captured. The user configures which processes to capture via `quern enable-local-capture <process_name>` (the process name is typically the Xcode target name). `set_local_capture` changes this Mac-wide list (owner only) at runtime and restarts the proxy: mocks, bypass patterns and the intercept are kept, but open connections reset and held flows are released (`restart` in the response says so). To capture an app on one simulator you hold without any restart -- and from a client key, the only way -- use `add_session_capture` (see `quern://guide/sharing`).
- **System proxy**: Configures macOS-wide proxy settings. Use `configure_system_proxy` to start capturing and `unconfigure_system_proxy` when done. Affects all Mac traffic — always unconfigure when finished.

**`set_local_capture` sets the list, but a minimum is always kept.** Naming one app no longer drops `MobileSafari` and `com.apple.WebKit.Networking` — a webview's requests leave through WebKit and an OAuth hand-off goes through Safari, so losing them silently stopped capturing the traffic you were probably looking for. The response says what was added for you (`capture_added`) and what your list removed (`capture_removed`); read both. Everything else is still replaced rather than merged, so read `local_capture` from `proxy_status` first and pass entries you want kept. `only: true` on the HTTP endpoint captures exactly your list for the running server. The **flag** is not stored but the **list is**, so it replaces whatever was configured before, and start-up widens that stored list again because nothing on disk records you meant it narrowly. Treat `only` as a one-shot narrowing, not a temporary view.

**Entries are mitmproxy local-mode syntax, not only process names.** Each is passed through unchanged. A name matches as a case-sensitive *substring* of the process's full executable path — the same path flows report as `source_process`, so any fragment of it works. A bare number is a PID, and a leading `!` excludes either form. Entries apply in order, and **the first one sets the starting point**: a list that begins with an exclusion captures every process on the Mac except those excluded. The minimum is appended after your entries, so it does not narrow that, and such a list is **refused with 400** unless you pass `whole_mac: true`. To drop one process from a capture, put the exclusion after an include — `["MyApp", "!12345"]`, never `["!12345"]`. And to work with one simulator, don't change this list at all: filter by `simulator_udid` (see *Per-simulator flow filtering* above). If the app's process isn't in the list, ask for `add_session_capture` on that simulator rather than widening the Mac-wide list.

Name the process that actually makes the requests. Safari's traffic leaves through `com.apple.WebKit.Networking`, not `MobileSafari`, and so does every in-app web view, so a list with `MobileSafari` alone captures nothing.
