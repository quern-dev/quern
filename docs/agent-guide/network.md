# Network: capture, mocks and intercept

*`quern://guide/network` -- part of the Quern agent guide. Start from `quern://guide`.*

Seeing, changing and faking an app's network traffic.

**On this page:** Capturing traffic · Filtering flows · Tools for network work · Intercept-Modify-Release for Testing Edge Cases · Slowing the network on a simulator · Mocking for Deterministic Testing · Mistakes to avoid · When something is wrong.

## Capturing traffic

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

**Refusals worth recognising.** These are answers, not faults, and retrying
them wastes a turn: a `launch_app` failure on an app that cannot start (see
`quern://guide/ui`), and the proxy refusals below.

**These tools can refuse, and that refusal is not an error to retry.** `configure_system_proxy`, and `start_proxy` **when called with `system_proxy: true`**, return **428** when a booted simulator does not trust the mitmproxy CA. Capturing in that state fails every HTTPS request from that device and the symptom points nowhere near the proxy — a blank screen, or an app that appears to have no network. The response names the devices and the ways out: install the certificate, set `auto_install_cert` so Quern handles it from now on, capture with `set_local_capture` instead (which passes such a simulator through), or shut the simulator down. (`skip_cert_check`, which proceeded anyway, is deprecated: a response to a request that uses it carries a `deprecations` note.)

**`set_local_capture` does not refuse — it passes untrusting simulators through.** With `auto_install_cert` set it first installs the CA on any booted simulator that doesn't trust it, and a successful install means that simulator's new connections are decrypted; `simulator_tls` says what is in effect now. Tell the user that turning capture on can install the CA, and say so when it did -- `simulator_tls: decrypted` alone doesn't prove this call installed it, since the simulator may have trusted the CA already. Otherwise, local capture spans every simulator, so a simulator that does not trust the CA has its TLS passed through untouched rather than decrypted: its apps keep working, but its HTTPS requests never appear as flows. `simulator_tls` on the response and on `proxy_status` lists each booted simulator as `decrypted` or `passed_through`, with the reason and the fix, and any flow query filtered to a passed-through simulator carries `simulator_tls_note` — as does `start_capture_session`, so you hear it before driving the app rather than after. **Read those before concluding an app made no requests.** Installing the CA on that simulator (`install_proxy_cert`) decrypts its new connections within seconds, with no restart (connections already open stay undecrypted until they close) — but installing a root CA is the user's decision, so ask first, unless `proxy_status` shows `auto_install_cert: true`, which is their standing yes: then install it and tell them you did.

**Starting a recording or capture session for a simulator checks it first.** `start_recording` with flows, and `start_capture_session` with `simulator_udid`, ask that simulator whether it trusts the CA before they say started. If it does not, they install the CA when `auto_install_cert` is set, and otherwise refuse with **428** -- a run that records no HTTPS looks fine and holds nothing. Pass `allow_passthrough` only when the user accepts recording without that simulator's HTTPS -- and, under the system proxy, accepts that its HTTPS requests fail for the whole run. `start_recording` returns `simulator_tls`, what is actually in effect for that simulator; and if it ever refuses the proxy's certificate, the recording's warnings and `simulator_tls_note` on flow results say so, because those requests failed in the app and are not flows.

**Do not route around it.** The two share one gate, so reaching the configured state by another tool recreates exactly the failure the refusal exists to prevent. In particular, `start_proxy {system_proxy: true}` after `configure_system_proxy` refused is the same action by another name — it used to work, and that was the bug. Starting the proxy *without* that flag is never refused, because binding a listener routes nothing; nor is disabling local capture with an empty `processes` list, since that is how you leave the broken state rather than enter it.

Ask the user which resolution they want. Installing a certificate authority persists across sessions and outlives the capture window, so it needs their say-so — the same way `update_quern` does.


**Certificate verification**: If no flows are captured, verify the proxy certificate is installed on the simulator:
1. Call `verify_proxy_setup` — performs a ground-truth check by querying the simulator's TrustStore database. Defaults to **booted simulators only**; pass `state="all"` or `device_type="device"` to check shutdown sims or physical devices
2. Returns per-device `status`: `installed`, `not_installed`, `never_booted`, or `error`
3. Returns `erased_devices` — UDIDs where a previously installed cert is now missing (probable device erase)
4. If the cert is missing, install it with `install_proxy_cert` — **after asking the user**, unless `proxy_status` shows `auto_install_cert: true`, which is their standing yes: then install it and tell them you did. Do not shell out to `xcrun simctl keychain add-root-cert` directly: that installs a root certificate authority with no record in Quern's cert state, so `proxy_status` and the capture preflight both go on believing the device is untrusted, and nothing tracks it for removal.

## Filtering flows

**Flows**: Filter by `host` (single), `hosts` (list), `exclude_hosts` (list), `method`, `path_contains`, and `status_min`/`status_max`. Use `detail="summary"` to get compact results (method/url/status/timing only) that stay within token limits. Use `get_flow_summary` first to identify which hosts or patterns to investigate.

## Tools for network work

**"I need to see network traffic"**
- Overview: `get_flow_summary` (use `simulator_udid` to filter by simulator)
- Specific requests: `query_flows` with filters (`hosts`, `exclude_hosts`, `detail="summary"`)
- Bracket a UI action: `start_capture_session` → action → `stop_capture_session`
- Capture an app by process name while you hold a simulator, without changing the Mac-wide list or restarting the proxy: `add_session_capture(simulator_udid, processes)`, then `remove_session_capture` (or let your hold end). It is not limited to your simulator: it captures those process names on every simulator, not only yours, and in shared visibility mode (the default) other callers can see the flows, so filter by `simulator_udid`. It works on the owner key and a client key alike; its refusals are listed in `quern://guide/sharing`
- Full detail: `get_flow_detail`
- Modify traffic: `set_intercept` + `release_flow` with modifications
- Mock responses: `set_mock`
- Replay a request: `replay_flow`
- Check capture mode: `proxy_status` — look at `local_capture` field

## Intercept-Modify-Release for Testing Edge Cases

Test error handling without breaking the backend:

1. Set up an intercept pattern matching the target endpoint (e.g., `~d api.example.com & ~m POST`)
2. Trigger the action in the app
3. Wait for the request to be held with `list_held_flows` (use the timeout parameter)
4. Release the flow with modifications to the request — its headers, body, URL or method — or unchanged
5. Observe how the app handles the result via `get_screen_summary` and `query_logs`

`release_flow` changes the request, not the response. To see how the app handles an error status or a malformed body, mock the response with `set_mock` (below); to see how it copes with a slow network, hold the flow before releasing it, or on a physical iPhone set a network condition (`quern://guide/performance`).

## Slowing the network on a simulator

`set_device_condition`'s network profiles are for physical iPhones only (`quern://guide/performance`). A simulator uses the Mac's network, so there are two ways, and neither is a quern setting:

- **One endpoint:** intercept it (`set_intercept`, owner key only), wait for the request with `list_held_flows`, and release it with `release_flow` once the delay you want has passed. The hold is the latency, and every other request is untouched. The server has **one** intercept pattern: `set_intercept` replaces whatever is set, and `clear_intercept` releases every held flow. So check `proxy_status` (its `active_intercept`) and `list_held_flows` first, and if someone else's intercept is active, ask before replacing it. Clear it when done.
- **Everything:** Network Link Conditioner, from Apple's Additional Tools for Xcode. It slows the whole Mac, every app and every simulator on it, so ask the user before turning it on, and turn it off afterwards.

## Mocking for Deterministic Testing

Use `set_mock` to return synthetic responses for specific endpoints. This lets you create reliable, repeatable test scenarios — fixed user data, specific error conditions, or edge-case payloads — without depending on backend state. Use `update_mock` to modify an existing rule's pattern or response without deleting and recreating it. Only the fields you name change: a body-only update keeps the status code and headers. `headers`, when given, replaces all headers.

**Mocking one simulator only.** A mock applies to every device whose request matches, so on a Mac where other simulators are running too (a CI machine running XCUITests, another agent) an unscoped mock answers their requests as well. Pass `simulator_udid` to `set_mock` to mock only that simulator: every other device's matching requests reach the real server and are captured as usual. No filter pattern can do this, because every simulator's traffic arrives from `127.0.0.1`; quern decides from the process that opened the connection, which also covers that simulator's web views. A request quern cannot attribute to a simulator is not mocked by a scoped rule. Scoped rules are checked before unscoped ones, so a catch-all on the same route never shadows a simulator's own mock; `update_mock` keeps a rule's place. Use local capture: with only the system proxy, the connection's process may not be known when the request arrives. A simulator whose HTTPS is passed through (it does not trust the CA) cannot have its HTTPS mocked, and `replay_flow` traffic comes from quern itself, so a scoped rule never mocks a replay. `set_mock`'s `warning` says when any of these applies.

**Mocking one Android device.** Pass its serial as `simulator_udid` (the name stays): `emulator-5554`, or a phone's serial. An emulator's requests are told by the emulator process that opened the connection; a phone's by its Wi-Fi address, which quern reads over adb, or from the record `record_device_proxy_config` leaves -- a phone whose address it cannot find is refused, rather than given a mock that never fires. The device has to be routed through the proxy (`record_device_proxy_config` with `apply=true`), and `set_mock`'s `warning` says when it is not. The first request on an emulator's connection may be held for a moment while quern works out which emulator it is; one it cannot tell goes through unmocked. A client key may scope mocks to the Android devices it holds, and they end with its hold. A phone's rule is matched by the address the phone had when the rule was set, shown as `client_ip` in `list_mocks`: if the phone's address changes, re-scope the rule (`update_mock` naming its serial again), or another device that takes the old address would get the mock.

A mocked request is recorded once, with `mock_rule_id` set and the tag `mocked`, so captured traffic distinguishes synthetic responses from real ones.

Mock rules take priority over intercept rules. Clear them with `clear_mocks` when done.
Clearing one rule by id now **errors with 404** if no such rule exists, where it used to
report success — so teardown can tell "removed it" from "it was never there", and a rule
you failed to remove does not go on quietly matching traffic. Do not retry that error: the
rule is absent, which is the state you wanted. Clearing *all* rules still succeeds on an
empty set, and its `count` says how many went.

**Filter pattern syntax:** Mock and intercept patterns use mitmproxy filter expressions. Common operators: `~d` (domain), `~u` (URL/path — use this for path matching), `~m` (method), `~c` (status code), `~h` (header), `~t` (content-type), `~b` (body). Combine with `&` (and), `|` (or), `!` (not). Examples: `"~d api.example.com & ~u /v1/users"`, `"~m POST & ~d api.example.com & ~u /v1/login"`. Note: `~p` is not a valid operator — use `~u` for path matching.

## Mistakes to avoid

**Using mock when you need intercept (or vice versa)** — Mocks return instant synthetic responses for stable test fixtures. Intercept pauses real requests for ad-hoc inspection and modification. Mock rules take priority over intercept.

**Not using per-simulator flow filtering** — When local capture is enabled, flows are tagged with the originating simulator's UDID. Always pass `simulator_udid` when querying flows during parallel testing — otherwise you'll see traffic from all simulators mixed together.

**Leaving system proxy configured** — If you use `configure_system_proxy`, always call `unconfigure_system_proxy` when done. Forgetting this breaks the user's browser. With local capture enabled, you typically don't need the system proxy for simulator traffic at all.

**Holding flows too long** — Held flows auto-release after 30 seconds to prevent hanging clients. Use `list_held_flows` with `timeout` for long-polling instead of rapid polling.

## When something is wrong

**"Proxy not running"** — Check with `proxy_status` and call `start_proxy` if needed.

**"No flows captured"** — Check `proxy_status` first, in this order:

1. `tls_rejections` — clients that refused the certificate we offered, with the host, the resolved process and simulator UDID, and the TLS alert verbatim. This is the direct evidence, and it distinguishes the two causes that look identical from the outside: `unknown ca` means the device does not trust the CA, while any other alert on a device that does usually means the app pins its certificate. Don't reinstall a certificate to fix pinning.
2. A `capture_without_cert` warning, and `cert_trust_stale` on a device in `cert_setup` — the second means that device's recorded `cert_installed: true` is contradicted by the device itself, which is what an erase leaves behind.
3. Otherwise: if `local_capture` is non-empty, simulator traffic should be captured automatically — verify certs with `verify_proxy_setup`. Check that the list actually contains the process making the requests — the web-view minimum is kept for you, but anything else you previously named is replaced rather than merged. If local capture is not enabled, the device may not be configured to route through the proxy; check `proxy_setup_guide` for device configuration steps.

Check that the flows weren't evicted first: `truncated` and `complete_after` on the answer say whether the window is whole, and `since` narrows the query to when the app made its requests. Under local capture, check `simulator_tls` next: a simulator that doesn't trust the CA has its HTTPS passed through, which produces neither flows nor TLS rejections. Otherwise, an empty `tls_rejections` with no flows usually means the traffic is not reaching the proxy at all, rather than being refused once it does — a routing problem, not a certificate one. Check `list_pending_requests` first: a request the server never answers has reached the proxy, but it only appears in `query_flows` once its response arrives or it fails.
