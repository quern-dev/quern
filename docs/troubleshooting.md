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

## Reading Crash Reports

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
