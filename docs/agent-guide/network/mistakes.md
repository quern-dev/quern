# Mistakes to avoid

*`quern://guide/network/mistakes` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

**Using mock when you need intercept (or vice versa)** — Mocks return instant synthetic responses for stable test fixtures. Intercept pauses real requests for ad-hoc inspection and modification. Mock rules take priority over intercept.

**Not using per-simulator flow filtering** — When local capture is enabled, flows are tagged with the originating simulator's UDID. Always pass `simulator_udid` when querying flows during parallel testing — otherwise you'll see traffic from all simulators mixed together.

**Leaving system proxy configured** — If you use `configure_system_proxy`, always call `unconfigure_system_proxy` when done. Forgetting this breaks the user's browser. With local capture enabled, you typically don't need the system proxy for simulator traffic at all.

**Holding flows too long** — Held flows auto-release after 30 seconds to prevent hanging clients. Use `list_held_flows` with `timeout` for long-polling instead of rapid polling.
