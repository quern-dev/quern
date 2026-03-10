# iOS Logging Best Practices

The difference between a productive AI-assisted debugging session and a frustrating one often comes down to log quality. This guide covers how to make your app's logs actually useful when an AI agent is the one reading them.

## os.log vs print()

### The Problem with print()

`print()` in Swift writes to stdout. On simulators, this shows up in Xcode's console. On physical devices, it goes nowhere useful — it doesn't appear in the unified logging system and can't be captured by any log collection tool.

For an AI agent trying to debug your app, `print()` output is invisible on physical devices and noisy on simulators.

### Why os.log Wins

`os.log` (via the `Logger` API in iOS 14+) writes to Apple's unified logging system:

- **Structured metadata**: subsystem, category, level (debug/info/notice/error/fault)
- **Captured everywhere**: simulators, physical devices, crash reports
- **Filterable at source**: Quern can filter by process, subsystem, level — before the data even enters its pipeline
- **Performance**: Designed to be always-on with minimal overhead
- **Privacy**: Supports `\(value, privacy: .private)` annotations that redact sensitive data

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

With this in place, you can tell your agent things like "show me just the networking errors" or "what happened in the auth flow?" and get precise answers.

## The print() Diverter Pattern

You have a large codebase full of `print()` calls. Rewriting them all isn't happening today. Here's a bridge:

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

Drop this in a file compiled in all targets. Now every `print()` also emits an os.log entry at debug level, tagged with the "print" category. Physical device debugging works, and your agent can see everything.

This is a stopgap — structured logs with proper levels and categories are always better.

## How Filtering Works

Understanding how Quern filters logs helps you guide your agent to the signal faster.

### Process-Level Filtering (Best Performance)

When your agent sets a process filter, Quern passes it directly to the underlying log capture command. The OS does the filtering — entries that don't match never enter Quern's pipeline. This is dramatically more efficient than capturing everything.

Tell your agent: **"Only capture logs from MyApp"** — this activates process-level filtering.

### Presets

Quern has built-in presets that drop common noise:

**`device-quiet`** (for physical devices) drops:
- System daemons: `remotepairingdeviced`, `symptomsd`, `bluetoothd`, `wifid`, `signpost_reporter`, `kernel`
- Noisy frameworks: `CoreBrightness`, `ColourSensorFilterPlugin`, `com.apple.CFNetwork`, `com.apple.network`

**`simulator-quiet`** (for simulators) drops:
- `HangTracer` messages (frequent, unhelpful)
- `com.apple.CoreFoundation` subsystem noise

Your agent applies these automatically when appropriate, but you can ask for them explicitly: **"Filter out the system noise"**

### Ingestion vs Query-Time Filtering

There are two levels of filtering, and the distinction matters:

**Ingestion filtering** happens at the source — the log capture subprocess only emits matching entries. This is fast and saves memory. Process filters work this way.

**Query-time filtering** happens when your agent searches the captured logs. Level, subsystem, text search — all applied at query time.

**Bottom line:** Always tell your agent which process to watch. Everything else is a bonus.

### Filter Scopes

Filters operate at three scopes: **device > source > global**. A filter set for a specific device overrides the source-level default, which overrides the global default. Your agent manages this for you, but it's useful to know if you're monitoring multiple devices simultaneously.

## Crash Reports

### Automatic Discovery

Quern watches `~/Library/Logs/DiagnosticReports/` for new crash reports. When your app crashes on a simulator, the report appears within seconds. No configuration needed.

For physical devices, your agent pulls crash reports on demand. For iOS 17+ devices, this involves translating between identifier formats behind the scenes (see [Device Pool](device-pool.md)).

### What You Get

Your agent parses crash reports into structured summaries. The key fields:

| Field | What it means |
|---|---|
| **EXC_BAD_ACCESS** | Memory issue — null pointer, dangling reference, use-after-free |
| **EXC_CRASH (SIGABRT)** | Deliberate abort — assertion failure, uncaught exception, `fatalError()` |
| **EXC_BREAKPOINT (SIGTRAP)** | Swift runtime trap — force-unwrap nil, array out of bounds, precondition failure |

The top stack frames show where the crash happened. Your agent can cross-reference the crash timestamp with logs and network traffic to build the full picture:

1. What API calls happened just before the crash?
2. Were there any error-level log entries leading up to it?
3. Did a network response contain unexpected data?

### Crash Hooks

You can run a command whenever a crash is detected:

```bash
./quern start --on-crash 'curl -X POST https://your-webhook.example.com -d @-'
```

The full crash report (JSON) is piped to stdin. The hook runs with a 60-second timeout.

### Suppressing the macOS Crash Dialog

Simulator crashes trigger a macOS crash dialog that blocks everything. Suppress it:

```bash
defaults write com.apple.CrashReporter DialogType none
```

Quern's `setup` command offers to do this for you.

## Build Output

When your agent builds your project and something goes wrong, Quern can parse the Xcode build output into a structured summary: error count, warning count, failed targets, and specific error messages with file/line references. Much easier to work with than scrolling through pages of compiler output.

## Tips for Better AI-Assisted Debugging

- **Log at the boundaries.** Network request/response, view lifecycle (viewDidAppear/viewDidDisappear), user actions (button taps, form submissions). You don't need to log every internal function — just where your code meets the outside world.

- **Use distinct subsystems and categories.** "com.example.myapp.networking" is infinitely more useful than just dumping everything into one stream. It lets your agent slice the logs precisely.

- **Include correlation IDs.** If your API returns a request ID, log it. When your agent sees a failed request in the network traffic and wants to find the corresponding log entries, a shared ID makes the connection instant.

- **Use fault level sparingly.** Fault-level entries persist across reboots and have higher overhead. Reserve them for truly unexpected conditions (impossible states, assertion failures), not ordinary errors.

- **Set accessibility identifiers on debug UI elements.** When your agent is debugging with logs AND UI automation simultaneously, identifiable elements make correlating "I tapped the refresh button" with "this log entry appeared" much easier.
