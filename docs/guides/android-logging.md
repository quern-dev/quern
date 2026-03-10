# Logcat Integration

How Quern captures and normalizes Android logcat output into its standard log entry format.

## Format

Quern reads logcat in `threadtime` format, which is the most structured format logcat offers:

```
03-08 14:22:45.123  1234  5678 D MyTag  : This is a debug message
03-08 14:22:45.124  1234  5678 E MyTag  : Something went wrong
```

Fields: date, time, PID, TID, level character, tag, message.

## Level Mapping

Android's log levels map to Quern's unified levels:

| Android | Character | Quern Level |
|---|---|---|
| Verbose | V | DEBUG |
| Debug | D | DEBUG |
| Info | I | INFO |
| Warn | W | WARNING |
| Error | E | ERROR |
| Fatal | F | FAULT |
| Assert | A | FAULT |

Both V (Verbose) and D (Debug) map to DEBUG because Quern's level hierarchy doesn't have a "verbose" equivalent, and in practice the distinction rarely matters for debugging.

## Starting Capture

```
start_device_logging(udid="emulator-5554")
```

When capture starts, Quern:
1. Clears the logcat buffer (`adb logcat -c`) to avoid replaying stale entries
2. Spawns `adb -s <serial> logcat -v threadtime` as a background process
3. Reads stdout line by line, parsing each into a `LogEntry`

Entries flow into the same ring buffer as iOS logs, so `query_logs` and `get_log_summary` work identically.

## Filtering

### By Process

```
start_device_logging(udid="emulator-5554", process="com.example.myapp")
```

Process filtering happens client-side in Quern (logcat's native process filtering is limited). Each log entry's PID is checked against the target process.

### By Tag

Tag filters are passed directly to logcat:

```
start_device_logging(udid="emulator-5554", tag_filter="MyTag:D *:S")
```

The logcat filter syntax is `<tag>:<level>`. The `*:S` silences everything else. Common patterns:

- `MyTag:V` — all messages from MyTag
- `MyTag:W *:S` — only warnings and above from MyTag, silence everything else
- `ReactNativeJS:V *:S` — React Native JS console output only

### After Capture

Once logs are in the ring buffer, use Quern's standard query tools:

```
query_logs(source="logcat", level="error", limit=20)
query_logs(source="logcat", search="NullPointer", limit=20)
get_log_summary(source="logcat", window="5m")
```

## Multi-Line Messages

Stack traces and other multi-line output are common in logcat. Lines that don't match the threadtime format are treated as continuations of the previous entry — they're emitted as standalone entries with the current timestamp and the same source metadata.

## Tips

- **Clear buffer before testing.** `start_device_logging` does this automatically, but if you're debugging manually, `adb logcat -c` prevents old messages from polluting your session.

- **Watch for PID recycling.** Android reuses PIDs aggressively. If you're filtering by process and see unexpected entries, the PID may have been reassigned to a different process. Restarting the log adapter resets the filter.

- **Use tags, not process names, for library code.** If you're debugging a specific library or module, filtering by tag is more reliable than process name, since the tag is set by the code and doesn't change.

- **React Native apps** log JS console output under the `ReactNativeJS` tag. Filter with `tag_filter="ReactNativeJS:V *:S"` for a clean JS-only log stream.

- **Logcat buffer size is limited.** On emulators the default ring buffer is 256KB. High-volume logging can wrap quickly. Start capture before reproducing the issue, not after.
