# Quern Troubleshooting Guide — iOS

## Common iOS Error Patterns

### Sandbox Violations
```
Sandbox: MyApp(1234) deny(1) file-read-data /path/to/file
```
**Cause**: App is trying to access a file outside its sandbox.
**Fix**: Check entitlements and file access patterns. Use proper APIs (FileManager, UIDocumentPickerViewController).

### AMFI / Code Signing
```
AMFI: code signature validation failed
```
**Cause**: Code signature is invalid or missing.
**Fix**: Clean build folder, re-sign the app, check provisioning profiles.

### AutoLayout Constraint Conflicts
```
Unable to simultaneously satisfy constraints
```
**Cause**: Conflicting layout constraints.
**Fix**: Look for the constraint dump in the log. Set `translatesAutoresizingMaskIntoConstraints = false`. Use constraint priorities.

### Memory Warnings
```
Received memory warning
```
**Cause**: App is using too much memory.
**Fix**: Profile with Instruments (Leaks, Allocations). Check for retain cycles, large image buffers, or unbounded caches.

### Network / TLS Issues
```
NSURLSession/NSURLConnection HTTP load failed
TIC TCP Conn Failed
boringssl_context_error_print
```
**Cause**: Network request failed, often due to ATS or certificate issues.
**Fix**: Check App Transport Security settings. Verify server certificates. Check network connectivity.

### CoreData
```
CoreData: error: Failed to call designated initializer
```
**Cause**: CoreData model/migration issue.
**Fix**: Check data model version, migration mappings, and entity class names.

## Taps and typing do nothing, and every call reports success

On Xcode 27, a booted simulator can accept input and discard it. Tools report
`{"status": "ok"}`, the tapped element is named back to you, and the screen
never changes. Reads, screenshots, `open_url` and app launches all keep
working, so the device looks healthy.

**Why.** Xcode 27 ships a guest HID daemon, `dtuhidd`, and its Device Hub
attaches it to every booted simulator. The guest answers by disconnecting the
legacy touch, button and keyboard services quern drives, and never reconnects
them. The keyboard is lost whenever Device Hub has attached; touch and buttons
depend on whether the guest's input layer started before or after the
attachment, so typing can be dead while tapping still works.

**Confirm it:**

```shell
xcrun simctl spawn <udid> notifyutil -g com.apple.coredevice.dtuhidd.active
```

`1` means the services were claimed. Note that `1` alone does not prove input
is dead -- a simulator booted before Device Hub started keeps working -- so use
it to explain a failure you are already seeing rather than to predict one.

**Fix it:** `restore_simulator_input` (or `POST /api/v1/device/ui/restore-input`).
This restarts SpringBoard: apps running on the simulator are killed and it
returns to the home screen in a few seconds. Nothing is reinstalled and the
simulator does not reboot.

Booting a simulator *through quern* does this for you, before anything is
running. The case that needs the call is a simulator that was already booted --
typically one booted while Xcode or its Device Hub was open.

Quitting Device Hub does **not** fix a simulator that is already affected: the
state persists for the life of that boot.

## Reading Crash Reports

Simulator crash reports from `~/Library/Logs/DiagnosticReports/` are watched automatically. To suppress the macOS crash dialog (useful on CI), run `defaults write com.apple.CrashReporter DialogType none` or use `quern setup`.

### Key Fields

- **Exception Type**: The Mach exception (e.g., `EXC_BAD_ACCESS`, `EXC_CRASH`)
- **Exception Codes**: Specific error codes (e.g., `KERN_INVALID_ADDRESS at 0x0`)
- **Signal**: Unix signal (`SIGSEGV` = bad memory access, `SIGABRT` = abort, `SIGTRAP` = breakpoint/assertion)
- **Faulting Thread**: The thread that crashed — look at its stack frames

### Common Crash Types

| Exception | Signal | Meaning |
|-----------|--------|---------|
| EXC_BAD_ACCESS | SIGSEGV | Dereferenced bad pointer (null, dangling, wild) |
| EXC_BAD_ACCESS | SIGBUS | Misaligned memory access |
| EXC_CRASH | SIGABRT | Deliberate abort (assertion, fatalError, uncaught exception) |
| EXC_BREAKPOINT | SIGTRAP | Swift runtime trap (force unwrap nil, array bounds, etc.) |
| EXC_BAD_INSTRUCTION | SIGILL | Illegal instruction (corrupted code or deliberate trap) |

### Investigation Steps

1. Find the **faulting thread** and read its stack frames top-to-bottom
2. Look for **your code** in the frames (not system frameworks)
3. Check the **exception type** to understand the category of crash
4. Look at logs just before the crash time for context
5. If symbolication is incomplete, use `atos` to resolve addresses
