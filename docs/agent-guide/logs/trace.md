# Which request did an action cause: `get_trace`

*`quern://guide/logs/trace` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

- `get_trace` — one timeline of quern's own actions, the flows each caused, and the app log lines that arrived while it ran. This is the tool for "I tapped Checkout, what did the app send?" and it replaces reconstructing the answer by hand from `query_flows` and `query_logs` timestamps.
- When several agents share one server, pass `mine: true` to see only the actions you asked for. Every action carries `session`, the id of the agent session that asked for it (each agent's quern MCP server sends one automatically), so this separates agents even on one shared device; `udid` separates devices. `session` reads another agent's actions by its id.
- Read `caveats` and `overlaps` before trusting a join. They are always present, so an empty list means "nothing to flag", not "this version doesn't say". Attribution strength varies: a simulator under local capture is joined exactly, a physical device by IP against a proxy config that DHCP may have invalidated, and a simulator behind a plain Wi-Fi proxy on timing alone.
- Traffic an action caused *after* it returned is attributed through a three-second grace window and flagged as such — most actions hand work to the device and return before the request goes out.
- `proxy_running` is in the response because a trace with no flows because the proxy was off looks exactly like one where the app requested nothing.
- If the log window reached further back than the ring buffer holds, the trace says so rather than presenting a truncated window as a quiet one.
