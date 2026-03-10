# Getting Started with Android

Quern's Android support covers the fundamentals: device discovery, app lifecycle, screenshots, emulator management, and logcat. It's designed to work alongside the existing iOS features — the same MCP tools handle both platforms.

## What Works

### Device Discovery

Quern automatically discovers Android devices and emulators through `adb`. They appear in `list_devices` alongside iOS simulators and physical devices:

```json
{
  "name": "Pixel 6 Dev",
  "udid": "emulator-5554",
  "type": "android_emulator",
  "state": "booted",
  "os_version": "14",
  "runtime": "API 34",
  "device_family": "Android"
}
```

Physical devices show up with their USB serial number as the UDID (e.g., `R5CR10XXXXX`).

### Device States

- **Booted**: Device is online and responsive
- **Unauthorized**: USB debugging prompt hasn't been accepted on the device. Tap "Allow" on the device screen.
- **Shutdown**: Device is offline (emulator not running, or physical device disconnected)

### Emulator Boot

```
resolve_device(type="android_emulator")  # Find or boot an emulator
```

If no Android emulator is running, `resolve_device` will boot one from your available AVDs. You can also boot a specific AVD by name through the `boot` endpoint.

Quern discovers AVDs via `emulator -list-avds` and boots them with `emulator -avd <name>`. It polls `adb devices` until the emulator appears and reports as booted (up to 60 seconds).

### App Lifecycle

All the standard app tools work with Android package names:

```
install_app(udid="emulator-5554", path="/path/to/app.apk")
launch_app(udid="emulator-5554", bundle_id="com.example.myapp")
terminate_app(udid="emulator-5554", bundle_id="com.example.myapp")
uninstall_app(udid="emulator-5554", bundle_id="com.example.myapp")
list_apps(udid="emulator-5554")
```

The `bundle_id` parameter accepts Android package names — both are reverse-domain strings, so the API works naturally across platforms. Launch uses `adb shell monkey` which opens the app's launcher activity without needing to know the activity class name.

### Screenshots

```
take_screenshot(udid="emulator-5554")
```

Returns a PNG via `adb exec-out screencap -p`. Works on both emulators and physical devices.

### Logcat

```
start_device_logging(udid="emulator-5554")
query_logs(source="logcat")
```

Logcat entries are parsed from `adb logcat -v threadtime` format and normalized to Quern's standard log entry schema. See [Logcat Integration](android-logging.md) for details on filtering and level mapping.

## What's Not Yet Supported

- **UI automation**: No `tap_element`, `get_ui_tree`, `get_screen_summary`, or `swipe` for Android. This would require uiautomator2 integration (planned for a future phase).
- **Annotated screenshots**: `take_annotated_screenshot` is iOS-only since it overlays accessibility tree data.
- **App state management**: `save_app_state` / `restore_app_state` are iOS-only (simulator container access).
- **Plist operations**: iOS-specific. Android equivalents (SharedPreferences) would need different tooling.
- **Build integration**: No Gradle build capture or APK build tooling yet.

## Emulator Image Types

This matters more than you'd think, especially for proxy certificate installation.

### Google APIs (Recommended for Development)

- Image name contains "Google APIs" (no "Play" in the name)
- Build tags: `dev-keys`
- **Rootable** via `adb root` — Quern can install proxy certificates as system certs automatically
- No Google Play Store pre-installed
- Available for all API levels

### Google Play

- Image name contains "Google Play"
- Build tags: `release-keys`
- **Not rootable** — `adb root` is disabled
- Google Play Store and Play Services pre-installed
- Cannot auto-install proxy certificates; requires manual user cert + `networkSecurityConfig`

### Vanilla AOSP

- No Google services at all
- Rootable
- Smallest image size
- Missing Google Play Services APIs (Firebase, Maps, etc. won't work)

**Rule of thumb:** Use Google APIs images for development and debugging. They give you full `adb root` access for cert installation and other debugging tasks, while still having the Google APIs (Firebase, Maps, etc.) your app likely depends on. Switch to Google Play images only when you need to test Play Store–specific behavior.

## SDK Tool Discovery

Quern finds `adb` and `emulator` by searching:

1. Your shell's `PATH`
2. `ANDROID_HOME` environment variable
3. `ANDROID_SDK_ROOT` environment variable
4. Well-known locations:
   - `~/Library/Android/sdk/` (Android Studio default on macOS)
   - `~/Android/Sdk/` (Linux default)
   - `/opt/android-sdk/`

If you've installed Android Studio but `adb` isn't on your PATH, Quern will still find it. The server logs which paths it resolved at startup — check `adb` in the tool availability output.

### If adb Isn't Found

If the Quern server was started before Android Studio was installed (or before `ANDROID_HOME` was set), restart the server:

```
./quern stop && ./quern start
```

The server resolves tool paths at startup. Environment changes require a restart.

## Creating a Rootable Emulator

If you only have Google Play emulator images and need a rootable one for proxy cert installation:

```bash
# Install a Google APIs system image (no "playstore" in the name)
sdkmanager "system-images;android-34;google_apis;x86_64"

# Create an AVD
avdmanager create avd -n "Pixel_6_Dev" -k "system-images;android-34;google_apis;x86_64" -d "pixel_6"
```

Then boot it through Quern and use `install_proxy_cert` for automatic certificate installation.
