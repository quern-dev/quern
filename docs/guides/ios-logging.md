# iOS Logging Best Practices

Getting useful logs out of iOS apps and into Quern. This covers why os.log matters, how to redirect print() for legacy code, and how to filter effectively.

## os.log vs print()

### The Problem with print()

`print()` in Swift writes to stdout. On simulators, this shows up in Xcode's console. On physical devices, it goes... nowhere useful. It doesn't appear in the unified logging system, can't be filtered by level or subsystem, and isn't captured by `log stream` or any log collection tool.

For an AI agent trying to debug your app, `print()` output is invisible on physical devices and noisy on simulators.

### Why os.log Wins

`os.log` (via the `Logger` API in iOS 14+ or `os_log` in older code) writes to Apple's unified logging system. This means:

- **Structured metadata**: subsystem, category, level (debug/info/notice/error/fault)
- **Captured everywhere**: simulators, physical devices, crash reports
- **Filterable at source**: Quern can filter by process, subsystem, level — before the data even enters the ring buffer
- **Performance**: os.log is designed to be always-on with minimal overhead. Debug-level messages are only materialized when someone is actually listening.
- **Privacy**: os.log supports privacy annotations (`\(value, privacy: .private)`) that redact sensitive data in non-debug contexts

### The Logger API (iOS 14+)

```swift
import os

extension Logger {
    static let networking = Logger(subsystem: "com.example.myapp", category: "networking")
    static let ui = Logger(subsystem: "com.example.myapp", category: "ui")
    static let auth = Logger(subsystem: "com.example.myapp", category: "auth")
}

// Usage
Logger.networking.info("Fetching user profile for \(userId, privacy: .private)")
Logger.networking.error("Request failed: \(error.localizedDescription)")
Logger.auth.debug("Token refresh started")
```

### Filtering in Quern

With structured logs, Quern can do surgical filtering:

```
# Only your app's logs
tail_logs(source="simulator", process="MyApp")

# Only networking errors
query_logs(process="MyApp", level="error", search="networking")

# Everything from your app, no system noise
start_simulator_logging(process="MyApp", preset="device-quiet")
```

Without os.log, you get a firehose of unstructured text. With it, you get exactly what you need.

## The print() Diverter Pattern

You have a large codebase full of `print()` calls. Rewriting them all to use `Logger` isn't happening today. Here's a bridge:

```swift
import os

#if DEBUG
/// Redirects print() output to os.log so it's captured by Quern and log tools.
/// Only active in DEBUG builds — release builds use the standard print().
@_transparent
public func print(_ items: Any..., separator: String = " ", terminator: String = "\n") {
    let message = items.map { String(describing: $0) }.joined(separator: separator)
    let logger = Logger(subsystem: Bundle.main.bundleIdentifier ?? "app", category: "print")
    logger.debug("\(message, privacy: .public)")

    // Also write to stdout so Xcode console still works
    Swift.print(items, separator: separator, terminator: terminator)
}
#endif
```

Put this in a file that's compiled in all targets. Now every `print()` call also emits an os.log entry at debug level, tagged with the "print" category. Quern captures it, you can filter on it, and physical device debugging works.

**Caveats:**
- This is a stopgap, not a long-term solution. Structured logs with proper levels and categories are always better.
- The `@_transparent` attribute inlines the function so the overhead is minimal, but it's still an extra call per print.
- In release builds, print() reverts to the standard behavior (which is still a no-op on devices, but at least you're not paying the os.log cost).

## Log Filtering Strategies

### Process-Level Filtering (Best Performance)

When you start log capture with a `process` filter, Quern passes it to the underlying `log stream --process` command. This means the OS does the filtering — entries that don't match never enter Quern's pipeline.

```
start_simulator_logging(process="MyApp")
start_device_logging(udid="...", process="MyApp")
```

This is dramatically more efficient than capturing everything and filtering afterward.

### Presets

The `device-quiet` preset drops common system daemons that spam logs (locationd, symptomsd, etc.). Combine with process filtering for minimal noise:

```
start_device_logging(udid="...", process="MyApp", preset="device-quiet")
```

### Runtime Filtering

Use `set_log_filter` to change what's captured without restarting the log adapter:

```
set_log_filter(process="MyApp", level="warning")  # Only warnings and above
set_log_filter(process="MyApp", subsystems=["com.example.myapp.networking"])
```

### Summary-First Approach

Don't start by reading raw logs. Start with:

```
get_log_summary(window="5m")
```

This gives you a breakdown by level and source. Then drill down:

```
query_logs(level="error", limit=20)  # What's going wrong?
query_logs(search="timeout", limit=20)  # Specific issue
```

## Crash Reports

### Automatic Discovery

Quern watches `~/Library/Logs/DiagnosticReports/` for new crash reports. When your app crashes on a simulator, the report appears within seconds. No configuration needed.

For physical devices, crash reports are pulled via devicectl when you call `get_latest_crash(udid="...")`.

### Crash Hooks

Run a command whenever a crash is detected:

```bash
./quern start --on-crash 'cat > /tmp/last_crash.json'
```

The full crash report (as JSON) is piped to stdin. The hook runs in the background with a 60-second timeout.

### Cross-Referencing

A crash report tells you *what* happened. Logs tell you *why*. Network flows tell you *what triggered it*. The debugging pattern:

1. `get_latest_crash` — get the crash report
2. Note the crash timestamp
3. `query_logs(after=<timestamp - 10s>, before=<timestamp>)` — what was the app doing?
4. `query_flows(after=<timestamp - 10s>, before=<timestamp>)` — any failed network requests?

## Build Output

Quern can capture Xcode build output and parse it into structured summaries:

- Error count, warning count
- Failed targets
- Specific error messages with file/line references

Use `parse_build_output` after a build to get a structured summary instead of scrolling through pages of compiler output.

## Tips

- **Set accessibility identifiers on debug UI elements.** When an agent is debugging with logs AND UI automation simultaneously, having identifiable elements makes correlating "I tapped the refresh button" with "this log entry appeared" much easier.

- **Log at the boundaries.** Network request/response, view lifecycle (viewDidAppear/viewDidDisappear), user actions (button taps, form submissions). You don't need to log every internal function call — just the edges where your code meets the outside world.

- **Use fault level sparingly.** os.log fault-level entries persist across reboots and have higher overhead. Reserve them for truly unexpected conditions (assertion failures, impossible states), not for ordinary errors.

- **Include correlation IDs.** If your API returns a request ID, log it. When you see a failed request in Quern's flow view and want to find the corresponding log entries, a shared ID makes the connection trivial.
