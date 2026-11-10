# Tools for network work

*`quern://guide/network/tools` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

**"I need to see network traffic"**
- Overview: `get_flow_summary` (use `simulator_udid` to filter by simulator)
- Specific requests: `query_flows` with filters (`hosts`, `exclude_hosts`, `detail="summary"`)
- Bracket a UI action: `start_capture_session` → action → `stop_capture_session`
- Capture an app by process name while you hold a simulator, without changing the Mac-wide list or restarting the proxy: `add_session_capture(simulator_udid, processes)`, on the owner key or a client key alike. Capture is by process name, so it is not limited to your simulator: filter flows by `simulator_udid`, and remember that in shared visibility mode (the default) other callers can see them. End it with `remove_session_capture(simulator_udid)` -- with no arguments it removes every capture your key holds -- or by releasing the simulator. What each call adds and stops (in-app web views are included by default), and its refusals, are in the two tools' descriptions and `quern://guide/sharing`
- Full detail: `get_flow_detail`
- Modify traffic: `set_intercept` + `release_flow` with modifications
- Mock responses: `set_mock`
- Replay a request: `replay_flow`
- Check capture mode: `proxy_status` — look at `local_capture` field
