# Debugging Crashes

*`quern://guide/logs/crashes` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

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
