# Device Pool & Resolution

When you tell your agent "debug my app on an iPhone," Quern figures out which device to use, boots it if needed, and handles the surprisingly complex identifier translation between Apple's various toolchains. This guide explains what's happening behind the scenes so you can guide your agent effectively.

## How Device Selection Works

Your agent can ask for a device by name, type, OS version, or device family — or just say "give me a simulator" and let Quern pick the best one. The selection logic:

1. **Already booted?** Prefer running devices over shut-down ones (avoids boot delay)
2. **Recently used?** Prefer the device you were just working with (warm state, caches)
3. **Auto-boot**: If nothing matching is running, Quern boots one automatically

This means you can usually just tell your agent what you need in plain terms:

> "Use an iOS 18 simulator"
> "Run this on the iPad Air"
> "I need three simulators for parallel testing"

Your agent translates these into the right resolution criteria. You don't need to know UDIDs.

## What You Should Know

### Device Types

Quern manages four kinds of devices:

- **Simulators** — Xcode iOS simulators. Fastest to boot, easiest to work with. Default for most tasks.
- **Physical iOS devices** — iPhones and iPads connected via USB. Required for performance testing, hardware features, and real-world behavior.
- **Android emulators** — AVD-based emulators. Boot from your Android Studio AVD configurations.
- **Android physical devices** — Connected via USB with developer mode enabled.

### When You Need to Intervene

Most of the time, device selection is automatic. But there are cases where you'll need to provide guidance:

- **No matching device exists**: If you ask for "iPad with iOS 17" but don't have one installed, you'll need to create it in Xcode first (Window > Devices and Simulators).
- **Physical device not showing up**: Make sure you've tapped "Trust This Computer" on the device. For iOS 17+, developer mode must also be enabled (Settings > Privacy & Security > Developer Mode).
- **Android "unauthorized"**: The USB debugging prompt on the device hasn't been accepted. Tap "Allow" on the device screen.

## The iOS 17+ Complexity (That Quern Hides)

This section explains *why* Quern exists for device management. If you just want to use it, skip ahead — but understanding the problem helps when troubleshooting.

### Three Identifiers for One iPhone

When Apple introduced iOS 17, they changed how Macs communicate with devices. A single physical iPhone now has different identifiers depending on which tool you ask:

| Tool | What it returns | Used for |
|---|---|---|
| **devicectl** (iOS 17+) | CoreDevice UUID | Device discovery, app installation |
| **usbmux** (libimobiledevice) | 40-character hex string | Crash reports, legacy tools, WDA port forwarding |
| **tunneld** | Tunnel address (IPv6) | Screenshots, logs, WDA connection |

Different operations need different identifiers, and they're not interchangeable.

### What Quern Does About It

When your agent asks for a device list, Quern queries all three backends simultaneously and builds a translation map by correlating device names. From then on, it automatically translates to the right identifier for each operation:

| Operation | iOS 17+ | Pre-iOS 17 |
|---|---|---|
| Screenshots | Via tunneld (IPv6 RemoteXPC) | Direct usbmux |
| WDA (UI automation) | Tunneld IPv6 direct connection | Local port-forward via usbmux |
| Log capture | pymobiledevice3 via tunnel | pymobiledevice3 via usbmux |
| App install | `devicectl` | `ideviceinstaller` |
| Crash reports | Maps to usbmux UDID first | usbmux UDID directly |

Every operation tries the modern path first and falls back automatically. Your agent just uses whatever UDID it got from the device list — Quern handles the rest.

### tunneld

The tunneld daemon creates persistent IPv6 tunnels to iOS 17+ devices. It's a LaunchDaemon that runs in the background and serves tunnel addresses to any tool that needs them.

If it's not running, iOS 17+ physical device features fall back to older mechanisms (which may or may not work depending on the operation). If you're having trouble with physical devices on iOS 17+, check:

```bash
./quern tunneld status
```

## Android Devices in the Pool

Android devices appear alongside iOS ones. Your agent handles them the same way:

> "Use an Android emulator"
> "Boot my Pixel 6 Dev emulator"

If no Android emulator is running, Quern discovers available AVDs and boots one. The same auto-boot behavior as iOS simulators.

**Important distinction**: Google APIs vs Google Play emulator images. Google APIs images are "rootable" — Quern can install proxy certificates automatically. Google Play images are locked down. See [Android Getting Started](android-getting-started.md) for details on choosing the right image.

## Tips for Working with Your Agent

- **Be specific when it matters.** "Use the iPhone 15 Pro simulator" is better than "use a simulator" if you need a specific screen size or Dynamic Island behavior.
- **OS version prefixes work.** Asking for "iOS 18" matches 18.0, 18.1, 18.2, etc. You don't need to specify the exact point release.
- **Multi-device is supported.** You can ask your agent to boot and manage multiple devices for parallel testing. Quern ranks and selects the best candidates.
- **Physical devices must be plugged in.** Unlike simulators, physical devices can't be "booted" on demand. They need to be connected, trusted, and (on iOS 17+) have developer mode enabled.
- **Restart Quern after installing new simulators.** If you create a new simulator runtime in Xcode, the server needs a refresh to see it. Either restart the server or ask your agent to refresh the device list.
