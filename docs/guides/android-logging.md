# Logcat Integration

How Android logs flow into Quern and what you should know about working with them.

## How It Works

When your agent starts log capture on an Android device, Quern reads from `logcat` (Android's logging system) and normalizes entries into the same format as iOS logs. You can ask your agent about logs the same way regardless of platform:

> "Show me the errors from my Android app"
> "What happened in the last 5 minutes?"
> "Filter logs to just my app"

## Level Mapping

Android has its own log levels, which Quern maps to its unified levels:

| Android | Quern | When to use it |
|---|---|---|
| Verbose / Debug | DEBUG | Development details, internal state |
| Info | INFO | Normal operations worth noting |
| Warn | WARNING | Something unexpected but recoverable |
| Error | ERROR | Something went wrong |
| Fatal / Assert | FAULT | App is about to crash |

Both Verbose and Debug map to DEBUG because in practice the distinction rarely matters for debugging.

## Tags

Android logs are organized by **tags** — short labels set by the code. Your agent can filter by tag for precise results. Some useful ones:

- Your app's custom tags (whatever you pass to `Log.d("MyTag", ...)`)
- `ReactNativeJS` — React Native JavaScript console output
- `ActivityManager` — App lifecycle events
- `System.err` — Uncaught exceptions and stack traces

If you know which tag your code uses, tell your agent: **"Show me logs with tag NetworkClient"**

## Tips

- **Your agent clears the logcat buffer** when it starts capture, so you won't see stale entries from before your session.
- **Multi-line messages** (stack traces) are handled — they show up as connected entries, not separate lines.
- **Logcat's buffer is limited** (~256KB on emulators). High-volume logging wraps quickly. Tell your agent to start capturing *before* you reproduce the issue, not after.
- **Process filtering is client-side** on Android (unlike iOS where it's subprocess-level). This means all log entries are still captured and parsed, just filtered in Quern's pipeline. Still fast enough for normal use, but heavier than iOS process filtering.
