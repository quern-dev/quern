# Build & Install

Build an Xcode project and install it on one or more devices in a single operation. Quern handles architecture partitioning, multi-device installation, and the various UDID translations needed across iOS versions.

## Basic Usage

```
build_and_install(
    project_path="/path/to/MyApp",
    scheme="MyApp"
)
```

This builds for the currently active device and installs. If no device is active, it resolves one automatically.

### Multiple Devices

```
build_and_install(
    project_path="/path/to/MyApp",
    scheme="MyApp",
    udids=["<sim-udid>", "<physical-udid>"]
)
```

Quern partitions the UDIDs by device type and builds once per architecture:

```
Simulator UDIDs  ──→  Build for iOS Simulator (once)  ──→  Install on each simulator
Physical UDIDs   ──→  Build for iOS (once)             ──→  Install on each physical device
```

Two builds, regardless of how many devices. Installation happens in parallel.

## Project Resolution

Pass either:
- A `.xcworkspace` path (preferred if you use CocoaPods/SPM)
- A `.xcodeproj` path
- A directory containing either (workspace takes priority)

Quern auto-discovers the project file and scheme list.

### Scheme Discovery

If you omit `scheme`, Quern queries the available schemes and returns them in the error message so you can pick one:

```json
{
  "error": "scheme is required",
  "available_schemes": ["MyApp", "MyAppTests", "MyAppUITests"]
}
```

## Architecture-Aware Building

Physical devices and simulators need different builds:

| Target | Build Destination | Architecture |
|---|---|---|
| Simulators | `generic/platform=iOS Simulator` | x86_64 / arm64 (Rosetta or native) |
| Physical devices | `generic/platform=iOS` | arm64 |

Quern uses `generic/platform=...` destinations so xcodebuild produces a universal build for each architecture class. One simulator build serves all simulators; one device build serves all physical devices.

## OS Version Checking

Before installing on a physical device, Quern reads `MinimumOSVersion` from the built app's Info.plist and compares it to the device's OS version. If the device is too old:

```json
{
  "device": "iPhone 12",
  "status": "skipped",
  "error": "App requires iOS 17.0, device has iOS 16.7"
}
```

This check is physical-device only — simulators always match their runtime.

## Auto-Boot

If a target simulator is shut down, Quern boots it before installing. This is transparent — you don't need to boot manually first.

## UDID Translation

This is where things get complex behind the scenes (see [Device Pool](device-pool.md) for the full story).

When you pass UDIDs to `build_and_install`:
1. Each UDID is resolved through the device pool
2. CoreDevice UUIDs (iOS 17+) are mapped to hardware UDIDs for xcodebuild
3. The correct install tool is selected per device:
   - iOS 17+: `xcrun devicectl device install app`
   - Pre-iOS 17: `ideviceinstaller` (with pymobiledevice3 fallback)
   - Simulators: `xcrun simctl install`

You don't need to know any of this. Just pass the UDIDs from `list_devices`.

## Response

```json
{
  "build_iphoneos": {
    "success": true,
    "app_path": "/path/to/Build/Debug-iphoneos/MyApp.app"
  },
  "build_iphonesimulator": {
    "success": true,
    "app_path": "/path/to/Build/Debug-iphonesimulator/MyApp.app"
  },
  "devices": [
    {"udid": "...", "name": "iPhone 15 Pro", "status": "installed"},
    {"udid": "...", "name": "iPhone 12", "status": "installed"},
    {"udid": "...", "name": "iPad Air", "status": "skipped", "error": "App requires iOS 17.0"}
  ],
  "all_installed": false
}
```

Each device gets an independent status. A build failure for one architecture doesn't prevent installation on the other.

## Configuration

```
build_and_install(
    project_path="/path/to/MyApp",
    scheme="MyApp",
    configuration="Release",          # Default: "Debug"
    udids=["<udid1>", "<udid2>"]
)
```

## Tips

- **Use Debug configuration** for development. Release builds strip debug symbols and enable optimizations that make debugging harder.
- **Combine with ensure_devices** for CI-like workflows: `ensure_devices(count=3)` → `build_and_install(udids=[...])` → run tests on all three.
- **Build errors** are returned with structured output. Use `parse_build_output` to get a summary of errors and warnings if the raw xcodebuild log is too verbose.
- **Watch for signing issues** on physical devices. If the build succeeds but install fails, it's usually a provisioning profile mismatch. The error message will indicate this.
