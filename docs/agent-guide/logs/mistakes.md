# Mistakes to avoid

*`quern://guide/logs/mistakes` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

**Confusing `tail_logs` and `query_logs`** — Use `tail_logs` for "show me recent stuff" (defaults to 50, newest first). Use `query_logs` for searching with filters and time ranges.

**Ignoring log source names** — `device` = physical device logs (on-demand, via `start_device_logging`), `simulator` = simulator unified logging (on-demand, via `start_simulator_logging`), `oslog` = host Mac unified logging (on-demand, via `start_oslog_streaming` — useful for capturing dev tool logs like Vite or webpack that write to os_log), `crash` = crash reports, `build` = xcodebuild output, `proxy` = network traffic. Legacy: `syslog` = idevicesyslog (disabled by default, opt-in with `--syslog`).
