# Device Pool & Resolution

How Quern finds, boots, and manages devices across simulators, emulators, and physical hardware. The goal: agents say "I need an iPhone" and get one, without caring about UDIDs, iOS versions, or which tool talks to which device.

## The Problem Quern Solves

Without Quern, working with Apple devices programmatically means juggling:

- **simctl** for simulators (returns simulator UDIDs)
- **devicectl** for iOS 17+ physical devices (returns CoreDevice UUIDs — a completely different format)
- **usbmux / libimobiledevice** for pre-iOS 17 physical devices (returns 40-char hex UDIDs)
- **adb** for Android (returns serial numbers like `emulator-5554` or USB serials)

Each tool has its own UDID format, its own state reporting, its own quirks. An agent would need to know which tool to use for which device, translate between identifier formats, and handle the cases where a device appears in one tool's output but needs a different identifier for another tool.

Quern's device pool unifies all of this into a single `list_devices` / `resolve_device` interface.

## resolve_device

The primary way to get a device. Describe what you want, get back a UDID:

```
resolve_device(type="simulator")                           # Any booted iPhone simulator
resolve_device(type="simulator", os_version="18")          # iOS 18.x simulator
resolve_device(type="device")                              # Any connected physical device
resolve_device(name="iPhone 15 Pro")                       # Specific model
resolve_device(type="android_emulator")                    # Any Android emulator
resolve_device(device_family="iPad", os_version="17")      # iPad on iOS 17
```

### How Resolution Works

1. **Explicit UDID** — if provided, verify it exists and return it
2. **Active device** — if one was previously resolved, reuse it (if it still matches)
3. **Booted match** — find a running device matching all criteria
4. **Shutdown + auto_boot** — find a shutdown device matching criteria, boot it
5. **Error with diagnostics** — list what's available so the agent can adjust

All filter criteria are AND'd: `type="simulator" & os_version="18" & device_family="iPhone"` means all three must match.

### Filtering Options

| Parameter | What it does | Examples |
|---|---|---|
| `type` | Device type | `"simulator"`, `"device"`, `"android_emulator"`, `"android_device"` |
| `name` | Name match (exact preferred, substring fallback) | `"iPhone 15 Pro"`, `"Pixel"` |
| `os_version` | Version prefix match | `"18"` matches 18.0, 18.1, 18.2; `"17.5"` matches 17.5.x |
| `device_family` | Device family | `"iPhone"`, `"iPad"`, `"Apple Watch"`, `"Apple TV"` |
| `auto_boot` | Boot shutdown devices if needed | `true` (default), `false` |

### Ranking

When multiple devices match, Quern picks the best one:

1. **Already booted** beats shutdown (avoids boot cost)
2. **Most recently used** beats idle (warm caches, likely the one you want)
3. **Alphabetical** as tiebreaker (deterministic)

## ensure_devices

Need multiple devices at once (parallel testing, multi-device workflows):

```
ensure_devices(count=3, type="simulator", os_version="18")
```

This finds or boots 3 iOS 18 simulators. Same filtering and ranking as `resolve_device`, but returns a list. Errors if not enough matching devices exist.

## The iOS 17+ Identity Problem

This is where things get interesting — and where Quern earns its keep.

### Three UDID Formats for One Device

A single physical iPhone can have three different identifiers depending on which tool you ask:

| Tool | Format | Example |
|---|---|---|
| **devicectl** (iOS 17+) | CoreDevice UUID | `53DA57AA-1B2C-3D4E-5F6A-7B8C9D0E1F2A` |
| **usbmux** (libimobiledevice) | 40-char hex | `a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0` |
| **tunneld** | Hardware ECID | Different again |

And different operations need different formats:
- **Screenshots** (pymobiledevice3): tunneld UDID for iOS 17+, libimobiledevice UDID for older
- **WDA connection**: tunneld IPv6 address for iOS 17+, localhost port-forward for older
- **Crash reports** (idevicecrashreport): always libimobiledevice UDID
- **App install** (devicectl): CoreDevice UUID for iOS 17+, libimobiledevice UDID for older
- **xcodebuild**: hardware UDID, resolved differently per version

### How Quern Handles It

When `list_devices()` runs, it queries all backends simultaneously and builds a mapping:

```
CoreDevice UUID → libimobiledevice UDID
```

The correlation happens by matching device names across devicectl and usbmux output. This map is cached and refreshed lazily.

When any operation needs a specific UDID format, Quern translates automatically:
- Agent passes the CoreDevice UUID from `list_devices`
- Screenshots? Quern resolves tunneld UDID, tries pymobiledevice3 via tunnel, falls back to usbmux
- WDA? Quern tries tunneld IPv6 connection first, falls back to local port-forward
- Crash reports? Quern maps to libimobiledevice UDID before calling idevicecrashreport

The agent never sees any of this. It just uses the UDID it got from `list_devices`.

### Pre-iOS 17 Detection

Quern recognizes pre-iOS 17 UDIDs by format: if it's exactly 40 lowercase hex characters, it's a libimobiledevice UDID and no translation is needed. Everything else goes through the mapping.

## iOS 17+ Infrastructure: tunneld

Physical device operations on iOS 17+ require Apple's RemoteXPC protocol, which pymobiledevice3 supports via a daemon called **tunneld**.

### What It Does

tunneld creates persistent IPv6 tunnels to connected iOS 17+ devices. Instead of each tool opening its own connection, they all go through the tunnel:

```
pymobiledevice3 (screenshots) ──→ tunneld ──→ iPhone (iOS 17+)
WDA client (UI automation)    ──→ tunneld ──→ iPhone (iOS 17+)
pymobiledevice3 (logs)        ──→ tunneld ──→ iPhone (iOS 17+)
```

It runs as a macOS LaunchDaemon on `http://127.0.0.1:49151` and exposes tunnel addresses via HTTP.

### Setup

```
./quern tunneld install    # Installs LaunchDaemon (requires sudo)
./quern tunneld status     # Check if running
```

### Graceful Degradation

If tunneld isn't running:
- iOS 17+ operations fall back to direct usbmux connections where possible
- Some operations may fail with a helpful error pointing to `./quern tunneld install`
- Pre-iOS 17 devices are completely unaffected

## How Operations Route by iOS Version

| Operation | iOS 17+ Path | Pre-iOS 17 Path |
|---|---|---|
| Device discovery | `xcrun devicectl list devices` | `pymobiledevice3 usbmux list` |
| Screenshots | `pymobiledevice3 dvt screenshot --tunnel` | `pymobiledevice3 dvt screenshot --udid` |
| WDA connection | Tunneld IPv6 direct (`[fd35::1]:8100`) | Local port-forward (`localhost:18100`) |
| Log capture | `pymobiledevice3 syslog live --tunnel` | `pymobiledevice3 syslog live --udid` |
| App install | `xcrun devicectl device install app` | `ideviceinstaller` or pymobiledevice3 |
| WDA driver start | xcodebuild with resolved hardware UDID | xcodebuild with libimobiledevice UDID |
| Crash reports | Map to libimobiledevice UDID first | libimobiledevice UDID directly |

Every row tries the new path first and falls back automatically. The agent's experience is identical regardless of iOS version.

## Android Devices in the Pool

Android devices and emulators appear alongside iOS devices with their own types:

```json
{"type": "android_emulator", "udid": "emulator-5554", "os_version": "14", "runtime": "API 34"}
{"type": "android_device", "udid": "R5CR10XXXXX", "os_version": "13", "runtime": "API 33"}
```

Resolution works the same way:

```
resolve_device(type="android_emulator")                    # Any Android emulator
resolve_device(type="android_emulator", os_version="14")   # Android 14 emulator
```

If no emulator is booted and `auto_boot` is true, Quern finds an available AVD and boots it.

## Pool State

The pool persists to `~/.quern/device-pool.json` with:
- Known devices and their last-seen state
- Last-used timestamps (for ranking)
- Device type cache (for fast lookups without re-querying)

Refreshed from live tools on every `list_devices` call (cached for 2 seconds to prevent hammering simctl/adb).

## Tips

- **Let resolve_device do the work.** Don't hardcode UDIDs. `resolve_device(type="simulator")` adapts to whatever's available.
- **Use ensure_devices for parallel testing.** It handles booting and returns devices in priority order (most recently used first).
- **Default is iPhone.** If you don't specify `device_family`, the pool defaults to "iPhone" (configurable in `~/.quern/config.json`).
- **Physical devices must be connected.** Unlike simulators, physical devices can't be "booted" — they're either there or they're not. `resolve_device(type="device")` only finds connected, trusted devices.
- **Trust your device first.** New physical devices need to accept the "Trust This Computer?" dialog before they appear in the pool. For iOS 17+, they also need developer mode enabled (Settings > Privacy & Security > Developer Mode).
