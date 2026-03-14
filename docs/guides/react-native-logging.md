# React Native Logging

React Native's `console.log` writes to stdout, which means it shows up as undifferentiated `ReactNativeJS` noise in the system log. No subsystem, no category, no levels — just a wall of text. For an AI agent trying to debug your app, this is almost useless.

The [`@quern/react-native-os-logger`](https://github.com/quern-dev/react-native-os-logger) package routes your JS logs through Apple's unified logging system (`os_log`) on iOS and `android.util.Log` on Android, with proper subsystem and category support. This means Quern can filter, search, and correlate your React Native logs exactly like native app logs.

## Installation

```sh
npm install @quern/react-native-os-logger
cd ios && pod install
```

Requires React Native 0.76+ (New Architecture / TurboModules).

## The Fastest Path: patchConsole()

If you want every `console.log` in your app to appear in Quern with zero code changes:

```typescript
import { patchConsole } from '@quern/react-native-os-logger';

// Add this one line at app startup (e.g. index.js or App.tsx)
patchConsole('com.yourcompany.yourapp');
```

That's it. All console methods now route through os_log:

| console method | os_log level | Persisted on iOS? |
|---|---|---|
| `console.debug()` | DEBUG | No |
| `console.info()` | INFO | No |
| `console.log()` | DEFAULT | Yes |
| `console.warn()` | ERROR | Yes |
| `console.error()` | ERROR | Yes |

Original console methods are preserved — logs still appear in Metro as usual. Objects and errors are automatically serialized.

## Structured Logging with Categories

The real power comes from creating separate loggers with distinct categories. This lets your agent filter logs by domain:

```typescript
import { createLogger } from '@quern/react-native-os-logger';

const netLogger = createLogger('com.myapp', 'networking');
const uiLogger = createLogger('com.myapp', 'ui');
const authLogger = createLogger('com.myapp', 'auth');

// In your API layer
netLogger.info('GET /api/users → 200 OK (142ms)');
netLogger.error('POST /api/login → 401 Unauthorized');

// In your components
uiLogger.debug('ProfileScreen rendered in 12ms');

// In your auth flow
authLogger.info('Token refresh started');
authLogger.error('Refresh token expired, redirecting to login');
```

Each `createLogger` call creates a separate `os_log_t` instance on iOS and a distinct `Log` tag on Android. In Quern, your agent can then ask for "just the networking logs" or "auth errors in the last 5 minutes" and get exactly that.

## How It Appears in Quern

When your agent starts simulator logging filtered to your subsystem:

```
start_simulator_logging(subsystem: "com.myapp", level: "debug")
```

Logs arrive with full metadata:

```
[info]  com.myapp / networking  — GET /api/users → 200 OK (142ms)
[error] com.myapp / networking  — POST /api/login → 401 Unauthorized
[debug] com.myapp / ui          — ProfileScreen rendered in 12ms
[info]  com.myapp / auth        — Token refresh started
```

Your agent can then query by category, level, or text search — the same tools that work for native Swift/Kotlin logs work identically for React Native.

## Combining with patchConsole

You can use both `patchConsole` and `createLogger` together. A common pattern:

```typescript
// Catch all console.* output under the "console" category
patchConsole('com.myapp', 'console');

// Use structured loggers for your own code
const netLogger = createLogger('com.myapp', 'networking');
const authLogger = createLogger('com.myapp', 'auth');
```

This way, third-party library output (which uses `console.log`) gets captured under the "console" category, while your own code uses specific categories. Your agent can filter out the library noise and focus on your app's logs.

## Log Levels: When to Use What

| Level | Use for | iOS behavior |
|---|---|---|
| `debug` | Verbose tracing, render times, internal state | Not persisted — only visible when actively streaming |
| `info` | Noteworthy events: API calls, screen transitions | Not persisted by default |
| `default` | Important milestones: app startup, user actions | Persisted to disk |
| `error` | Failures that need attention | Persisted, includes caller info |
| `fault` | Should-never-happen conditions | Persisted across reboots, includes full backtrace |

**Tip:** Use `default` level (or `console.log`) for anything you want to survive across app restarts. `info` and `debug` are only visible when a log consumer (Quern, Console.app) is actively attached.

## Android

On Android, logs appear in logcat with `subsystem:category` as the tag:

```
I/com.myapp:networking  GET /api/users → 200 OK (142ms)
E/com.myapp:networking  POST /api/login → 401 Unauthorized
D/com.myapp:ui          ProfileScreen rendered in 12ms
```

The level mapping: `debug` → `Log.d`, `info`/`default` → `Log.i`, `error` → `Log.e`, `fault` → `Log.wtf`.

## Tips for Better AI-Assisted Debugging

- **Log at the boundaries.** Network request/response, screen navigation, user actions. Don't log every internal function call.

- **Use distinct categories.** `networking`, `ui`, `auth`, `storage` — whatever makes sense for your app. The more specific your categories, the faster your agent can find what matters.

- **Include IDs.** If your API returns a request ID or correlation ID, include it in the log. When your agent sees a failed request in Quern's network capture, a shared ID makes connecting it to the corresponding log entries instant.

- **Use patchConsole early.** Call it before any other code runs (top of `index.js`). This ensures even early startup logs from React Native internals get captured.
