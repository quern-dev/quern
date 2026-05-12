# idb Fix: Probe Childless Group Elements (fixes facebook/idb#767)

> **Status (Quern, v0.13+): shipped natively in sim-bridge.** Quern's
> default simulator backend now performs the same grid hit-test via
> AXPTranslator's `objectAtPoint:displayId:bridgeDelegateToken:`,
> implemented in `tools/sim-bridge.swift` and wired through
> `server/device/probing.py`. The idb patch described below is still
> the right fix for the upstream `idb_companion` and remains relevant
> for Intel Macs / pre-Xcode-26 hosts that fall back to the idb path.

## Status: FIXED — PR ready

Branch: `fix/group-children-fallback` in our idb fork at `/Users/jerimiah/Dev/idb`

## Problem

`idb ui describe-all` drops children of Group-type accessibility containers. This is a known idb bug ([facebook/idb#767](https://github.com/facebook/idb/issues/767), open since March 2023).

The root cause: `accessibilityChildren` returns an empty array for certain container elements (e.g., `UITableView` with 0 data rows reporting as "Empty list"). No alternative attribute accessor (`AXVisibleChildren`, `AXChildren`, `accessibilityVisibleChildren`) returns the children either. However, `objectAtPoint` hit-testing CAN find elements within these groups.

## Solution

After the recursive tree traversal in `FBAXTranslationRequest_FrontmostApplication.run:`, scan the results for Group elements with:
- No children in the tree
- A frame area > 5000 pt² (filters out small icon containers)

For each such group, probe its interior at a 30px grid using `objectAtPoint`. Discovered elements are deduplicated by frame and tagged with `discovery_method="group_probe"`.

### Before
```
Total elements: 4
Application  label=Geocaching
Button       label=Search By:
TextField    label=                   id=_Geocache search field
Group        label=Empty list
```

### After
```
Total elements: 6
Application  label=Geocaching
Button       label=Search By:
TextField    label=                   id=_Geocache search field
Group        label=Empty list
StaticText   label=This is a Premium-only cache...
Button       label=Upgrade            id=_CTA Upgrade button
```

## Files Changed

- `FBSimulatorControl/Commands/FBSimulatorAccessibilityCommands.m` — added `probeChildlessGroupsInElements:` method, wired into `run:`
- `FBControlCore/Utility/FBVideoStream.m` — suppress `-Wgnu-folding-constant` for Xcode 26 compat
- `FBSimulatorControl/FBSimulatorControl.h` — add missing umbrella header entries
- `FBSimulatorControl/Framebuffer/FBSimulatorVideoStream_Testing.h` — fix include style

## Build Notes

Building from `main` requires Xcode 26+ build fixes (included in our branch). Build arm64-only:

```bash
cd /Users/jerimiah/Dev/idb
./build.sh generate  # generates xcodeproj from project.yml
# Then build each target arm64:
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj -scheme FBControlCore -sdk macosx -derivedDataPath build -configuration Release build
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj -scheme XCTestBootstrap -sdk macosx -derivedDataPath build -configuration Release build
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj -scheme FBSimulatorControl -sdk macosx -derivedDataPath build -configuration Release build
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj -scheme FBDeviceControl -sdk macosx -derivedDataPath build -configuration Release build
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj -scheme CompanionLib -sdk macosx -derivedDataPath build -configuration Release build
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj -scheme IDBCompanionUtilities -sdk macosx -derivedDataPath build -configuration Release build
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project idb_companion/idb_companion.xcodeproj -scheme idb_companion -sdk macosx -derivedDataPath build -configuration Release build
```

## Running the Patched Companion

```bash
DYLD_FRAMEWORK_PATH=~/.quern/bin:~/.quern/bin/PackageFrameworks \
  ~/.quern/bin/idb_companion \
  --udid <SIMULATOR_UDID> \
  --grpc-domain-sock /tmp/idb/<SIMULATOR_UDID>_companion.sock \
  --only simulator
```

