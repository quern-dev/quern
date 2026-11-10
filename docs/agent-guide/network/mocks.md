# Mocking for Deterministic Testing

*`quern://guide/network/mocks` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

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
