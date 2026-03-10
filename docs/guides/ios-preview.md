# Live Video Preview

Real-time screen mirroring for physical iOS devices over USB. See exactly what's on the device screen in a native macOS window — useful for watching what happens when your agent interacts with the device, or just keeping an eye on things.

## Usage

Tell your agent:

> "Show me the device screen"
> "Start a live preview of my iPhone"

A native macOS window opens showing the device's screen in real time. The preview updates at whatever frame rate the device provides (typically 60fps).

To stop:

> "Close the preview" or just close the window.

## What You Need to Know

### USB Only

CoreMediaIO screen capture requires a **wired USB connection**. Lightning and USB-C both work. Wi-Fi does not. If you're working with a physical device wirelessly, preview won't be available.

### First-Launch Permission

The first time the preview app runs, macOS will ask for screen recording permission. Grant it, or the capture will fail silently. This is a standard macOS privacy prompt — you'll find it in System Settings > Privacy & Security > Screen Recording.

### Device Trust

Your iPhone must trust the Mac ("Trust This Computer?" dialog). If you haven't accepted this, the device won't appear for preview.

## Multi-Device

You can preview multiple devices simultaneously — each gets its own window. Just ask your agent to start preview on each device.

Behind the scenes, Quern staggers the capture sessions by 1 second between devices to work around a CoreMediaIO race condition. This is automatic.

### Manual Control

Once a preview window is open, the **Devices** menu in the menu bar shows all connected USB devices. Click to toggle preview on/off. Devices with active previews are marked with a checkmark.

## Orientation

CoreMediaIO always captures in the device's native orientation (portrait). When your app rotates to landscape, the captured frames show the rotated content within the portrait-oriented buffer — it'll look sideways.

The preview window doesn't auto-rotate currently. What you see is the raw frame buffer from CoreMediaIO.

## Limitations

- **USB only.** Wi-Fi connections don't support CoreMediaIO screen capture.
- **View only.** You can't interact with the device by clicking on the preview window. (This is planned — mapping clicks to taps via WDA.)
- **No audio.** Video only.
- **macOS only.** CoreMediaIO is an Apple framework.
- **No brightness control.** But the preview always shows full brightness via CoreMediaIO regardless of the device's actual screen brightness. So for dark-room scenarios: face the device down, the preview still works fine.
