# Can quern see this app's logs?

*`quern://guide/logs/visibility` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

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
- Or start log capture (`start_simulator_logging` or `start_device_logging`), launch the app, and look for a line you know it logs with `query_logs(process=...)`. On a simulator, an empty answer filtered to a running app's process says why in `note`: a log filter that drops it, a level it never reaches, or `print()`. That is decided from what the buffer still holds from the app, on each simulator the query covers, not from your query's search or window. So an empty search or `since` window on an app with lines in the buffer gets `note: null` -- it logs, just not that -- unless every line it has is below the `level` you asked for, which is said instead.

**If the app uses `print()`**, tell the user rather than concluding the app is silent, and recommend `os.Logger`, with `privacy: .public` on values worth seeing on a device (they otherwise show as `<private>`), and never on tokens, passwords or personal data. Don't rewrite the app's logging without asking: it's their code. The full advice for the app's developers, including a `print()` diverter for an app that can't switch yet, is in quern's iOS logging guide: https://quern.dev/ios/ios-logging/
