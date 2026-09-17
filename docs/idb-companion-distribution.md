# Distributing the Patched idb_companion

> **Status (v0.13+):** sim-bridge replaces this path on Xcode 26+ Apple Silicon
> hosts. The group-children probe, native screenshot capture, and HID input
> injection all run through `tools/sim-bridge.swift` now — no idb daemon, no
> companion binary, no subprocess-per-call. This doc remains relevant only as
> the fallback path for Intel Macs and pre-Xcode-26 setups; the `quern setup`
> flow skips the companion install entirely when sim-bridge is supported.
> See [`sim-bridge-spec.md`](sim-bridge-spec.md) for the current backend.
>
> An install left over from an older setup is still the fallback when
> sim-bridge cannot run, so setup offers to replace an outdated one on every
> host (see *Releases* below).

## Overview

Quern ships its own `idb_companion` binary instead of depending on the stale Homebrew `facebook/fb/idb-companion` (v1.1.8, August 2022). Our patched build is from `main` (March 2026) and includes:

- **Group children probe** — discovers hidden elements inside childless Group containers (fixes [facebook/idb#767](https://github.com/facebook/idb/issues/767))
- **xcode-select fallback** — works without `/var/db/xcode_select_link` symlink
- **Xcode 26 build fixes** — compiles cleanly with current toolchain
- **Xcode 27 SimulatorKit location** (v2) — Xcode 27 moved SimulatorKit to `Contents/SharedFrameworks`; v1 looks only in `Developer/Library/PrivateFrameworks`, so every HID command fails under Xcode 27 ([#222](https://github.com/quern-dev/quern/issues/222))

## Releases

Published on [quern-dev/idb](https://github.com/quern-dev/idb/releases), asset `idb-companion-patched-arm64.tar.gz`:

| Release | Branch @ commit | Built with | Notes |
|---|---|---|---|
| `idb-companion-v1` | `fix/group-children-fallback` @ `ae03179e` | Xcode 26 | HID fails under Xcode 27 |
| `idb-companion-v2` | `fix/xcode27-simulatorkit` @ `a9daf33d` | Xcode 27.0 | current |

v2 requires macOS 12 (Xcode 27's minimum deployment target; v1 ran on 11). `idb xctest` is likely still broken under Xcode 27 -- XCTestBootstrap hardcodes `Developer/Library/PrivateFrameworks` for XCTAutomationSupport, which also moved -- but quern does not use it.

An install is replaced by extracting into a staging directory under `~/.quern/bin` and swapping `Frameworks/` and the binary in once the payload is complete, so an interrupted update leaves the previous install intact. The release marker is cleared only at the swap and rewritten after it. A newer marker than setup's own is left alone rather than offered a downgrade.

`_IDB_COMPANION_RELEASE` in `server/lifecycle/setup.py` names the release setup installs. A successful install writes it to `~/.quern/bin/idb_companion.release`; v1 wrote no marker, so an install without one is read as v1. `check_idb_companion` reports an install older than the current release as a warning, and setup offers to replace it.

## Building a release

On the Xcode the release should support, from a checkout of the fork:

```bash
brew install xcodegen protobuf swift-protobuf
./build.sh build idb_companion        # products in build/Build/Products/Release
```

The build is universal; the published asset is arm64 only, and must keep the layout below. Thinning changes the nested frameworks' contents, so re-sign ad hoc afterwards, nested frameworks first:

```bash
B=build/Build/Products/Release; S=/tmp/idb-stage
mkdir -p $S/bin $S/Frameworks/PackageFrameworks
ditto --arch arm64 $B/idb_companion $S/bin/idb_companion
for f in CompanionLib FBControlCore FBDeviceControl FBSimulatorControl \
         IDBCompanionUtilities IDBGRPCSwift XCTestBootstrap; do
  ditto --arch arm64 $B/$f.framework $S/Frameworks/$f.framework
done
for f in $B/PackageFrameworks/*.framework; do
  ditto --arch arm64 "$f" "$S/Frameworks/PackageFrameworks/$(basename "$f")"
done
# re-sign Versions/A/Frameworks/*.framework inside each, then each framework:
#   codesign -f -s - <framework>
find $S -name .DS_Store -delete
(cd $S && COPYFILE_DISABLE=1 tar czf /tmp/idb-companion-patched-arm64.tar.gz ./bin ./Frameworks)
```

The SPM frameworks under `PackageFrameworks` report "code has no resources but signature indicates they must be present" from `codesign -v --deep`. v1 is the same, and the binaries inside are signed, which is what loading needs.

Before publishing, drive a simulator through the staged bundle (tap, swipe, text, describe-all) with `DYLD_FRAMEWORK_PATH` pointing at its `Frameworks` and `Frameworks/PackageFrameworks`. After publishing, bump `_IDB_COMPANION_RELEASE`.

## Artifact

- **File**: `idb-companion-patched-arm64.tar.gz` (~17MB)
- **Architecture**: arm64 (Apple Silicon only for now)
- **Contents**:
  ```
  bin/idb_companion          # 2.4MB binary
  Frameworks/                # Framework dependencies
    CompanionLib.framework
    FBControlCore.framework
    FBDeviceControl.framework
    FBSimulatorControl.framework
    IDBCompanionUtilities.framework
    IDBGRPCSwift.framework
    XCTestBootstrap.framework
    PackageFrameworks/       # SPM-built gRPC dependencies
  ```

## Hosting

Published as a GitHub release asset on the `quern-dev/idb` fork, with the source commit named in the release notes. See *Releases* above.

## Installation (quern setup)

During `quern setup`, replace the Homebrew `idb-companion` check with:

```python
def install_companion():
    """Download and install the patched idb_companion."""
    dest = Path.home() / ".quern" / "bin"
    dest.mkdir(parents=True, exist_ok=True)

    if (dest / "idb_companion").exists():
        return  # Already installed

    url = "https://github.com/<org>/quern/releases/download/idb-companion-v1/<idb-companion-arm64.tar.gz>"
    tarball = dest / "idb-companion.tar.gz"

    # Download
    urllib.request.urlretrieve(url, tarball)

    # Extract
    subprocess.run(["tar", "xzf", str(tarball), "-C", str(dest)], check=True)
    tarball.unlink()

    # Move bin/idb_companion to dest directly
    (dest / "bin" / "idb_companion").rename(dest / "idb_companion")
    # Move Frameworks alongside
    # (already extracted to dest/Frameworks/)
```

## Runtime Integration

### How idb finds the companion

The `idb` Python CLI accepts `--companion-path` to specify the companion binary:

```bash
idb ui describe-all --companion-path ~/.quern/bin/idb_companion --udid <UDID>
```

### How Quern should launch it

Option 1 — pass `--companion-path` on every `idb` call in `idb.py`:

```python
async def _run(self, *args: str) -> tuple[str, str]:
    companion = Path.home() / ".quern" / "bin" / "idb_companion"
    cmd = [self._resolve_binary()]
    if companion.exists():
        cmd.extend(["--companion-path", str(companion)])
    cmd.extend(args)
    ...
```

Option 2 — start the companion directly with `DYLD_FRAMEWORK_PATH`:

```python
async def _ensure_companion(self, udid: str):
    """Start our patched companion for a simulator."""
    bin_dir = Path.home() / ".quern" / "bin"
    companion = bin_dir / "idb_companion"
    sock = Path(f"/tmp/idb/{udid}_companion.sock")

    env = os.environ.copy()
    env["DYLD_FRAMEWORK_PATH"] = f"{bin_dir}:{bin_dir / 'Frameworks' / 'PackageFrameworks'}"

    proc = await asyncio.create_subprocess_exec(
        str(companion),
        "--udid", udid,
        "--grpc-domain-sock", str(sock),
        "--only", "simulator",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Wait for gRPC socket to appear
    ...
```

Option 2 is more reliable since it controls the framework path. Option 1 is simpler but may not handle `DYLD_FRAMEWORK_PATH`.

## Removing Homebrew Dependency

Once this is in place:
1. Remove `brew install idb-companion` from setup
2. Remove `check_idb_companion()` or repurpose it to check `~/.quern/bin/idb_companion`
3. Update all "install idb-companion" messages

## Rebuilding

To rebuild from source (e.g., after pulling upstream changes):

```bash
cd /path/to/idb
git checkout fix/group-children-fallback
./build.sh generate
# Build arm64:
for scheme in FBControlCore XCTestBootstrap FBSimulatorControl FBDeviceControl CompanionLib IDBCompanionUtilities; do
    xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project FBSimulatorControl.xcodeproj \
        -scheme $scheme -sdk macosx -derivedDataPath build -configuration Release build
done
xcodebuild ONLY_ACTIVE_ARCH=YES ARCHS=arm64 -project idb_companion/idb_companion.xcodeproj \
    -scheme idb_companion -sdk macosx -derivedDataPath build -configuration Release build

# Package:
tar czf idb-companion-arm64.tar.gz -C build/Build/Products/Release .
```

Prerequisites: Xcode 26+, `brew install xcodegen protobuf swift-protobuf`

## Current State of the Work Machine

On the work Mac (`/Users/jerimiah`):

- **idb fork**: `/Users/jerimiah/Dev/idb` on branch `fix/group-children-fallback`
  - NOT pushed to a remote yet — need to fork facebook/idb on GitHub first
- **Pre-built tarball**: `/tmp/idb-companion-patched-arm64.tar.gz` (17MB)
  - This is in `/tmp` — copy it somewhere durable before reboot!
- **Running patched companion**: `~/.quern/bin/idb_companion` (PID varies)
  - Started with: `DYLD_FRAMEWORK_PATH=~/.quern/bin:~/.quern/bin/PackageFrameworks ~/.quern/bin/idb_companion --udid <UDID> --grpc-domain-sock /tmp/idb/<UDID>_companion.sock --only simulator`
  - Frameworks live alongside binary in `~/.quern/bin/` and `~/.quern/bin/PackageFrameworks/`
- **Homebrew idb-companion was uninstalled** — `brew install facebook/fb/idb-companion` to restore if needed
- **`/var/db/xcode_select_link` was removed** — the patched companion doesn't need it (falls back to `xcode-select -p`), but the Homebrew v1.1.8 companion DOES need it. If you reinstall the old companion, recreate with: `sudo ln -sf /Applications/Xcode.app/Contents/Developer /var/db/xcode_select_link`

## Files Changed in the idb Fork (5 files)

1. **`FBSimulatorControl/Commands/FBSimulatorAccessibilityCommands.m`** — THE FIX
   - Added `probeChildlessGroupsInElements:keys:frontmostPid:collector:` method (~80 lines)
   - Wired into `FBAXTranslationRequest_FrontmostApplication.run:` after recursive traversal
   - Fixed sign conversion warnings in `FBAccessibilityCoverageGrid` (pre-existing)

2. **`FBControlCore/Utility/FBXcodeConfiguration.m`** — xcode-select fallback
   - `findXcodeDeveloperDirectory:` now falls back to `xcodeSelectDeveloperDirectory` subprocess when the `/var/db/xcode_select_link` symlink doesn't exist

3. **`FBControlCore/Utility/FBVideoStream.m`** — build fix
   - Added `#pragma clang diagnostic ignored "-Wgnu-folding-constant"` for Xcode 26 compat

4. **`FBSimulatorControl/FBSimulatorControl.h`** — umbrella header fix
   - Added missing `#import` for `FBPeriodicStatsTimer.h` and `FBSimulatorVideoStream_Testing.h`

5. **`FBSimulatorControl/Framebuffer/FBSimulatorVideoStream_Testing.h`** — include fix
   - Changed double-quoted includes to angle-bracket framework includes

## How the Group Probe Works

The fix is in `FBSimulatorAccessibilityCommands.m` inside `FBAXTranslationRequest_FrontmostApplication`:

1. After the normal recursive tree traversal (`flatRecursiveDescriptionFromElement` / `nestedRecursiveDescriptionFromElement`), the `run:` method has a list of all discovered elements.

2. The new code scans that list for elements where:
   - `role == "AXGroup"` (Group container)
   - Frame area > 5000 pt² (not a tiny icon group)

3. For each such group, it probes a 30px grid within the group's frame using `[self.translator objectAtPoint:point displayId:0 bridgeDelegateToken:self.token]` — the same hit-testing API that `idb ui describe-point` uses.

4. Hits that return an element with a DIFFERENT frame than the group itself are new discoveries. They're deduplicated by frame and added to the results with `discovery_method="group_probe"`.

5. This runs BEFORE the remote content discovery step, so it doesn't interfere with cross-process element detection.

**Why this works**: `accessibilityChildren` returns empty for these containers (Apple framework bug), but `objectAtPoint` uses a different code path in `AccessibilityPlatformTranslation` that correctly resolves elements at coordinates. The probe bridges this gap.

**Performance**: A typical empty-list group (400x770 pt) generates ~300 probe points at 30px intervals. Each `objectAtPoint` call is ~0.5ms, so total probe time is ~150ms — imperceptible.

## TODO

1. Fork facebook/idb on GitHub → push branch → submit PR
2. Upload tarball as GitHub release asset
3. Update Quern setup to download + install instead of `brew install idb-companion`
4. Update Quern's `idb.py` to launch companion from `~/.quern/bin/`
5. Remove `"Group"` workaround from `_PROBEABLE_ROLES` if present (the companion now handles it natively)
6. Unskip `test_basic_user_premium_cache_cta_C26613` in the search tests

## Source

- Fork branch: `fix/group-children-fallback` at `/Users/jerimiah/Dev/idb`
- Based on: facebook/idb `main` @ `a571e0b3` (March 23, 2026)
- Our commit: `7463e76d` — "Probe childless Group elements to discover hidden accessibility children"
- PR: to be submitted to facebook/idb