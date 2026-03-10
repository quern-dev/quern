# Build & Install

Tell your agent to build and install your app, and Quern handles the details: figuring out architectures, building once per platform, and installing across multiple devices in parallel.

## How It Works

When you say something like "build and install my app on all three simulators," your agent:

1. Finds your Xcode project and discovers available schemes
2. Partitions your target devices by type (simulator vs physical)
3. Builds once for simulators, once for physical devices (not once per device)
4. Installs in parallel across all targets

This means building for 5 simulators is barely slower than building for 1 — there's only one compilation step.

## What You Need to Tell Your Agent

- **Where your project is.** Point it to the directory containing your `.xcworkspace` or `.xcodeproj`. Workspaces take priority (correct for CocoaPods/SPM projects).
- **Which scheme to build.** If you don't specify, the agent will see the available schemes and ask (or pick the obvious one if there's only one app scheme).
- **Which devices to target.** Or just let it use the currently active device. For multi-device, say something like "build and install on all my booted simulators."

## What Quern Handles for You

### Architecture Partitioning

Simulators and physical devices need different builds. Quern figures out which devices need which architecture and builds each only once:

- Simulators → `iOS Simulator` build (x86_64 or arm64 depending on your Mac)
- Physical devices → `iOS` build (arm64)

### OS Version Checking

Before installing on a physical device, Quern checks if the device's iOS version meets the app's minimum deployment target. If not, it skips that device with a clear explanation rather than failing cryptically during install.

### Auto-Boot

If a target simulator is shut down, Quern boots it automatically before installing.

### UDID Translation

For physical devices on iOS 17+, the build system needs different identifiers than what the device list provides. Quern translates between CoreDevice UUIDs (from device discovery) and hardware UDIDs (for xcodebuild) automatically. See [Device Pool](device-pool.md) for the full story.

## Tips

- **Use Debug configuration** (the default) during development. Release builds strip debug info and make debugging harder.
- **Build errors are structured.** If the build fails, ask your agent to parse the build output — it'll give you a summary of errors with file/line references instead of pages of xcodebuild output.
- **Watch for signing issues** on physical devices. If the build succeeds but install fails, it's usually a provisioning profile mismatch.
- **Combine with multi-device workflows.** Ask your agent to boot three simulators, build and install, then run your test scenario on each — all in one conversation.
