# Getting Started with Android

Quern's Android support covers the fundamentals: device discovery, app lifecycle, screenshots, emulator management, and log capture. The same agent workflow you use for iOS works for Android — your agent handles the platform differences.

## What Works

Tell your agent things like:

> "List my Android devices"
> "Boot an Android emulator"
> "Install this APK and launch it"
> "Show me the Android logs"
> "Take a screenshot of the emulator"

Behind the scenes, these use `adb` for device communication. Your agent handles the specifics.

### What's Supported

- **Device discovery**: Emulators and physical devices appear in the device list alongside iOS
- **Emulator boot**: Your agent can boot AVDs by name from your Android Studio installations
- **App lifecycle**: Install APKs, launch, terminate, uninstall, list installed apps
- **Screenshots**: PNG capture from emulators and physical devices
- **Logcat**: Real-time log capture with level mapping (see [Logcat Integration](android-logging.md))

### What's Not Yet Supported

- **UI automation**: No tapping, swiping, or reading the screen's accessibility tree (would require uiautomator2 — planned for a future phase)
- **Annotated screenshots**: iOS-only (depends on accessibility tree overlay)
- **App state checkpoints**: iOS simulator–only feature (container access)
- **Build integration**: No Gradle build tooling yet

## Emulator Image Types

This matters for proxy certificate installation and debugging capabilities.

### Google APIs (Recommended for Development)

- Rootable via `adb root` — Quern can install proxy certificates automatically
- Has Google Play Services (Firebase, Maps, etc.) but no Play Store app
- Available for all API levels
- **This is what you want for development and testing**

### Google Play

- **Not rootable** — `adb root` is disabled
- Includes Google Play Store and Play Services
- Cannot automatically install proxy certificates
- Use only when you specifically need to test Play Store behavior

### How to Tell Which You Have

Ask your agent to check if your emulator is rootable. It reads the device's build tags — `dev-keys` means rootable (Google APIs), `release-keys` means locked (Google Play).

### Creating a Rootable Emulator

If you only have Google Play images and need a rootable one:

1. Open Android Studio > Virtual Device Manager
2. Create a new device
3. **Choose a system image with "Google APIs" in the name** (not "Google Play")
4. Any API level works, but API 34+ (Android 14) is recommended

Or from the command line:

```bash
sdkmanager "system-images;android-34;google_apis;x86_64"
avdmanager create avd -n "Pixel_6_Dev" -k "system-images;android-34;google_apis;x86_64" -d "pixel_6"
```

## Device States

Your agent reports these states for Android devices:

- **Booted**: Online and responsive
- **Unauthorized**: The USB debugging prompt hasn't been accepted on the device — tap "Allow" on the device screen
- **Shutdown**: Emulator not running, or physical device disconnected

## SDK Tool Discovery

Quern finds `adb` and `emulator` by searching:
1. Your shell PATH
2. `ANDROID_HOME` / `ANDROID_SDK_ROOT` environment variables
3. Standard locations (`~/Library/Android/sdk/` on macOS, `~/Android/Sdk/` on Linux)

If Android Studio was installed after Quern was started, restart the server: `./quern stop && ./quern start`

## Tips

- **Use Google APIs images** unless you specifically need Google Play Store behavior. Rootable emulators make proxy setup automatic.
- **USB debugging must be enabled** for physical devices: Settings > Developer Options > USB Debugging.
- **Developer mode must be unlocked** on physical devices: Settings > About Phone > tap "Build Number" 7 times.
- **Emulators share adb** — if you have multiple instances, they get sequential serial numbers (`emulator-5554`, `emulator-5556`, etc.). Your agent handles this automatically.
