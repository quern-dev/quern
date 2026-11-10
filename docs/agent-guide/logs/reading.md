# Reading logs

*`quern://guide/logs/reading` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

**Logs**: Filter by `level`, `process`, `search` text, and time range. Don't fetch 1,000 entries when 50 filtered ones will do. For sustained debugging, use `set_log_filter` to drop noise at ingestion — the `device-quiet` preset removes common system daemons, or combine `process` + `subsystems` includes for app-only output with zero framework noise. Setting a process filter automatically restarts running log adapters with subprocess-level filtering, cutting noise at the source. You can also apply presets at start time: `start_device_logging(process: "MyApp", preset: "device-quiet")`.

**"I need to see logs"**
- Recent activity: `tail_logs`
- Overview: `get_log_summary`
- Specific search: `query_logs` with filters
- Errors only: `get_errors`
- Reduce noise at start: `start_device_logging(process: "MyApp", preset: "device-quiet")` — applies subprocess-level process filter and ingestion preset in one call
- Reduce noise mid-session: `set_log_filter(source: "device", process: "MyApp")` — automatically restarts the adapter with subprocess-level filtering and purges old entries
- **App-only mode** (zero noise): First `tail_logs` with the process filter to see your app's own entries, then lock it down with `set_log_filter(source: "device", process: "MyApp", subsystems: ["com.myapp"])` -- your os_log subsystem -- or the library your code lives in, which entries carry as `sender` (`"MyApp.debug.dylib"` in a debug build); a subsystem filter matches either. This eliminates all framework noise (UIKitCore, CFNetwork, Security) and shows only your code's output.
