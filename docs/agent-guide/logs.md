# Logs, crashes and recordings

*`quern://guide/logs` -- part of the Quern agent guide. Start from `quern://guide`.*

Reading app logs, investigating crashes, and recording a run to read back afterwards.
## Can quern see this app's logs?

Check before you rely on them. Quern reads app logs from **unified logging on iOS** and **logcat on Android**, and nothing else.

| The app logs with | iOS simulator | Physical iPhone | Android |
|---|---|---|---|
| `os.Logger` / `Logger`, `os_log` | Captured | Captured | -- |
| `NSLog()` | Captured | Captured | -- |
| `print()`, `debugPrint()`, `dump()` | **Not captured** | **Not captured** | -- |
| `android.util.Log`, `println` / `System.out` | -- | -- | Captured (stdout lands in logcat as `System.out`) |

On iOS, `print()` writes to the app's stdout, which unified logging never sees and quern never reads. So an app that logs with `print()` reads as an app that logged nothing: an empty `query_logs` filtered to its process is not evidence that nothing happened.

**How to check**, before a session that depends on logs:
- Search the project: `print(` against `Logger(`, `os_log` or `NSLog`.
- Or start log capture (`start_simulator_logging` or `start_device_logging`), launch the app, and look for a line you know it logs with `query_logs(process=...)`. On a simulator, an empty answer for a running app says why in `note`: a log filter that drops it, a level it never reaches, or `print()`.

**If the app uses `print()`**, tell the user rather than concluding the app is silent, and recommend `os.Logger`, with `privacy: .public` on values worth seeing on a device (they otherwise show as `<private>`), and never on tokens, passwords or personal data. Don't rewrite the app's logging without asking: it's their code. The full advice for the app's developers, including a `print()` diverter for an app that can't switch yet, is in quern's iOS logging guide: https://quern.dev/ios/ios-logging/

## Reading logs

**Logs**: Filter by `level`, `process`, `search` text, and time range. Don't fetch 1,000 entries when 50 filtered ones will do. For sustained debugging, use `set_log_filter` to drop noise at ingestion — the `device-quiet` preset removes common system daemons, or combine `process` + `subsystems` includes for app-only output with zero framework noise. Setting a process filter automatically restarts running log adapters with subprocess-level filtering, cutting noise at the source. You can also apply presets at start time: `start_device_logging(process: "MyApp", preset: "device-quiet")`.

**"I need to see logs"**
- Recent activity: `tail_logs`
- Overview: `get_log_summary`
- Specific search: `query_logs` with filters
- Errors only: `get_errors`
- Reduce noise at start: `start_device_logging(process: "MyApp", preset: "device-quiet")` — applies subprocess-level process filter and ingestion preset in one call
- Reduce noise mid-session: `set_log_filter(source: "device", process: "MyApp")` — automatically restarts the adapter with subprocess-level filtering and purges old entries
- **App-only mode** (zero noise): First `tail_logs` with the process filter to see your app's own entries, then lock it down with `set_log_filter(source: "device", process: "MyApp", subsystems: ["com.myapp"])` -- your os_log subsystem -- or the library your code lives in, which entries carry as `sender` (`"MyApp.debug.dylib"` in a debug build); a subsystem filter matches either. This eliminates all framework noise (UIKitCore, CFNetwork, Security) and shows only your code's output.

## Mistakes to avoid

**Confusing `tail_logs` and `query_logs`** — Use `tail_logs` for "show me recent stuff" (defaults to 50, newest first). Use `query_logs` for searching with filters and time ranges.

**Ignoring log source names** — `device` = physical device logs (on-demand, via `start_device_logging`), `simulator` = simulator unified logging (on-demand, via `start_simulator_logging`), `oslog` = host Mac unified logging (on-demand, via `start_oslog_streaming` — useful for capturing dev tool logs like Vite or webpack that write to os_log), `crash` = crash reports, `build` = xcodebuild output, `proxy` = network traffic. Legacy: `syslog` = idevicesyslog (disabled by default, opt-in with `--syslog`).

## A screenshot after every action: screenshot timelines

`start_screenshot_timeline` captures a screenshot after every UI action quern performs (tap, type, swipe, launch, `open_url`...), each labelled with the action, and `stop_screenshot_timeline` returns the manifest: timestamp, action, screenshot path and HTTP status for every step. `get_screenshot_timeline` reads it without stopping. Use it for a visual record of a run for a person to review; for actions joined to their flows and logs, use `get_trace` or a recording instead.

## Debugging Crashes

1. Check recent crashes with `get_latest_crash`
2. Get logs around the crash time — query for error-level entries in the seconds leading up to the crash
3. Check network activity around the same time with `query_flows`
4. Correlate: crash report + logs + network activity = full picture

**Key insight**: Crashes leave traces in multiple places. Cross-referencing sources is where you find root causes.

**Crash discovery**: Simulator crash reports are automatically picked up from `~/Library/Logs/DiagnosticReports/` (enabled by default). The macOS crash dialog can be disabled via `quern setup` or manually with `defaults write com.apple.CrashReporter DialogType none` — crash reports are still written to disk.

**Crash hooks**: Use `--on-crash '<command>'` to run a shell command whenever a crash is detected: an iOS crash report, or an Android crash found by `get_latest_crash` (once per crash, even when logcat already logged it). Crashes from before the server started, such as a phone's older reports or DropBox history, are listed but do not run the hook. The full `CrashReport` JSON is piped to the command's stdin. The hook runs in the background with a 60-second timeout and never blocks the server. Example:

```bash
quern start --on-crash 'cat > /tmp/last_crash.json'
```

## Reproducing Bug Reports

1. Use `get_screen_summary` to verify your starting state
2. Follow the reported steps using `tap_element`, `type_text`, `swipe`, etc.
3. After each step, call `get_screen_summary` to verify the expected state before continuing — this catches where the reproduction diverges from expectations
4. Check logs and network flows alongside UI state to build the full picture
5. If the bug reproduces, capture a diagnostic bundle: screenshot, logs, network flows, and UI tree

**To let people watch it live**, pass `viewer=true` with `video=true`: the response's `viewer_url` plays the simulator in a browser on another machine on the LAN, and expires 10 minutes after the recording stops. Needs `viewer.enabled` in `~/.quern/config.json`; see docs/api-reference.md, "The LAN viewer".

**To keep the whole run**, call `start_recording(udid=...)` before step 1 and `stop_recording` after. Quern's buffers are bounded and shared, so a long run outlives them; a recording writes the device's actions, full network flows (including requests that never finished) and app logs to disk, and on a simulator `video=true` films the screen too. Read it back as a timeline with `get_trace(recording=<id>)`, where each action carries the flows and logs it caused, or page through it with `get_recording`. To see what each step cost the app, add `gauges=<bundle id or package>`: every action in the trace then carries its CPU, memory and (on Android) frames, as peaks over the samples that overlap it, live as well as afterwards.

**Key insight**: Verify state at each step. The step where expected and actual diverge is where the bug lives.

## Which request did an action cause: `get_trace`

- `get_trace` — one timeline of quern's own actions, the flows each caused, and the app log lines that arrived while it ran. This is the tool for "I tapped Checkout, what did the app send?" and it replaces reconstructing the answer by hand from `query_flows` and `query_logs` timestamps.
- When several agents share one server, pass `mine: true` to see only the actions you asked for. Every action carries `session`, the id of the agent session that asked for it (each agent's quern MCP server sends one automatically), so this separates agents even on one shared device; `udid` separates devices. `session` reads another agent's actions by its id.
- Read `caveats` and `overlaps` before trusting a join. They are always present, so an empty list means "nothing to flag", not "this version doesn't say". Attribution strength varies: a simulator under local capture is joined exactly, a physical device by IP against a proxy config that DHCP may have invalidated, and a simulator behind a plain Wi-Fi proxy on timing alone.
- Traffic an action caused *after* it returned is attributed through a three-second grace window and flagged as such — most actions hand work to the device and return before the request goes out.
- `proxy_running` is in the response because a trace with no flows because the proxy was off looks exactly like one where the app requested nothing.
- If the log window reached further back than the ring buffer holds, the trace says so rather than presenting a truncated window as a quiet one.
