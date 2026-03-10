# Live Video Preview

Real-time screen mirroring for physical iOS devices over USB. Uses Apple's CoreMediaIO framework to capture the device screen as a video stream, displayed in a native macOS window.

## What It Is

When you plug an iPhone or iPad into your Mac via USB, macOS can capture the device's screen through CoreMediaIO — the same mechanism that lets you use an iOS device as a camera source in QuickTime or OBS. Quern wraps this in a lightweight Swift app that provides:

- Real-time video preview in a native macOS window
- Multi-device support (multiple windows, one per device)
- MCP tool control (start/stop from AI agents)
- A Devices menu for manual device selection

This is **USB only**. Wi-Fi connections don't support CoreMediaIO screen capture.

## Usage

### From MCP Tools

```
preview_device(name="iPhone")        # Start preview (partial name match)
stop_preview(name="iPhone")          # Stop a specific preview
preview_status                       # List active previews
```

The `name` parameter does partial matching — "iPhone" will match "iPhone 15 Pro", "iPad" will match "iPad Air". If multiple devices match, you'll get an error asking you to be more specific.

### Manual Control

Once the preview window is open, the Devices menu (in the menu bar) shows all connected USB devices. Click a device to toggle its preview on/off. Devices with active previews are marked with a checkmark.

## How It Works Under the Hood

### Binary Management

The preview app is a single-file Swift program (`tools/ios-preview.swift`) that Quern compiles on first use:

1. `preview_device` is called
2. Quern checks if `~/.quern/bin/ios-preview` exists and is newer than the source
3. If not, compiles with `swiftc` (linking AVFoundation, CoreMediaIO, AppKit)
4. Creates a proper `.app` bundle at `~/.quern/bin/Quern Preview.app/` with Info.plist and icon
5. Launches the app in interactive mode

The binary is cached — subsequent calls skip compilation unless the Swift source changes.

### CoreMediaIO Capture

The app uses `AVCaptureDevice.DiscoverySession` to discover connected iOS devices, then creates an `AVCaptureSession` with the device as input and an `AVCaptureVideoPreviewLayer` for display. The capture runs at whatever resolution and frame rate the device provides (typically 60fps at native resolution).

### Interactive Protocol

When launched by Quern, the preview app communicates via JSON Lines on stdin/stdout:

- **Commands** (Quern → app): `add`, `remove`, `list`, `quit`
- **Events** (app → Quern): `ready`, `added`, `removed`, `window_closed`, `devices`, `error`

This lets Quern manage preview windows programmatically while the app stays alive between operations.

## Multi-Device

You can preview multiple devices simultaneously. Each device gets its own window.

### The Stagger Rule

CoreMediaIO has a race condition when starting multiple `AVCaptureSession` instances simultaneously. Quern works around this by staggering session starts by 1 second:

```
preview_device(name="iPhone 15")     # Starts immediately
preview_device(name="iPad Air")      # Waits ~1s after first
```

If you try to start them too fast (e.g., scripting rapid `preview_device` calls), the second session may fail silently. The 1-second stagger is handled automatically by the server.

## Orientation

CoreMediaIO always captures in the device's native orientation (portrait). When an app rotates to landscape, the captured frames show the rotated content within the portrait-oriented buffer (with black bars).

The preview window does not auto-rotate. What you see matches what CoreMediaIO delivers: the raw frame buffer. This means landscape apps look sideways in the preview.

Future work: auto-rotation by polling WDA's orientation endpoint and applying `CATransform3DMakeRotation` to the preview layer.

## Known Limitations

- **USB only.** CoreMediaIO screen capture requires a wired USB connection. Lightning and USB-C both work. Wi-Fi does not.
- **No click-to-tap.** The preview is view-only. You can't interact with the device by clicking on the preview window. (This is planned — mapping window coordinates to device screen coordinates via WDA.)
- **No audio.** CoreMediaIO captures video only. Audio capture would require a separate AVAudioSession which isn't implemented.
- **macOS only.** CoreMediaIO is an Apple framework. This feature doesn't work on Linux.
- **First-launch permission.** macOS will ask for screen recording permission the first time the preview app runs. Grant it, or the capture will fail silently.
- **Device trust required.** The iOS device must trust the Mac (the "Trust This Computer?" dialog). If you haven't trusted, the device won't appear in the device discovery list.
