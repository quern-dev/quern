# Android Proxy Setup

Capturing HTTPS traffic from Android emulators and physical devices. Android's certificate trust model is more restrictive than iOS, so the approach depends on whether you have root access.

## The Quick Version

For rootable emulators (Google APIs images):

> "Install the proxy certificate on my Android emulator"

Your agent handles everything: root access, certificate installation, HTTP proxy configuration. Traffic starts flowing.

For non-rootable devices, you'll need to modify your app. Keep reading.

## Why Android Is Different

Android separates certificate trust into two stores:

- **System certs**: Trusted by all apps. Read-only — requires root to modify.
- **User certs**: Installable without root, but **not trusted by apps** targeting API 24+ (Android 7+) unless the app explicitly opts in.

This means installing a cert through Android Settings doesn't help for debugging most modern apps. You either need root (to install as a system cert) or you need to configure your app to trust user certs in debug builds.

## Rootable Emulators (Automatic)

If your emulator uses a Google APIs image (not Google Play), your agent handles everything automatically. Behind the scenes, it:

1. Verifies the emulator is rootable
2. Converts the mitmproxy CA to Android's expected format
3. Installs it as a system certificate
4. Configures the HTTP proxy to route through your Mac

The technique varies by API level:

- **API < 34 (Android 13 and below)**: Classic remount — `adb root`, push cert to system partition
- **API >= 34 (Android 14+)**: Certificates moved to an APEX module. Quern uses an `nsenter` injection technique to mount the cert into running process namespaces

Both approaches are non-persistent — the cert may be lost on emulator reboot. Your agent detects this and re-installs as needed.

## Non-Rootable Devices (Manual App Change)

Google Play emulator images and physical devices (without root) can't have system certs injected. You have two options:

### Option 1: Use a Rootable Emulator Instead

Create a Google APIs emulator (see [Getting Started](android-getting-started.md#creating-a-rootable-emulator)). This is the easiest path.

Your agent will suggest this if you try to install a cert on a non-rootable device — it'll tell you the exact `sdkmanager` and `avdmanager` commands to create one. If you ask nicely, it might even run them for you.

### Option 2: networkSecurityConfig (Debug Builds)

Add a network security configuration to your app that trusts user-installed certificates in debug builds only:

**`res/xml/network_security_config.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <debug-overrides>
        <trust-anchors>
            <certificates src="user" />
            <certificates src="system" />
        </trust-anchors>
    </debug-overrides>
</network-security-config>
```

**`AndroidManifest.xml`:**
```xml
<application
    android:networkSecurityConfig="@xml/network_security_config"
    ... >
```

Then install the mitmproxy cert as a user certificate (Settings > Security > Install a certificate > CA certificate).

The `<debug-overrides>` block only applies to debug builds. Release builds ignore it entirely — no security risk.

**This is actually the recommended approach for app developers.** It explicitly declares your app's trust policy, works on any device, and doesn't require root. The rootable-emulator approach is better for ad-hoc debugging of apps you can't modify.

## HTTP Proxy

### Emulators

Your agent configures this automatically during cert installation. Android emulators use `10.0.2.2` to reach the host machine's loopback — this is a built-in Android emulator feature.

### Physical Devices

Manual configuration: Settings > Wi-Fi > long-press your network > Modify network > Proxy: Manual. Set your Mac's IP and port 9101 — same as iOS physical device setup.

## Cleanup

Unlike the system proxy on macOS, the Android emulator proxy and cert are designed to persist. They don't affect anything outside the emulator, so there's nothing to clean up when you're done.

## Troubleshooting

**"Not rootable" error:**
- Your emulator uses a Google Play image. Ask your agent to help you create a Google APIs emulator, or use the `networkSecurityConfig` approach.

**Cert installed but HTTPS still fails:**
- On API 34+, the injection may not have reached all app processes. Kill and relaunch the app.
- Check if the app uses certificate pinning — pinned apps reject any non-pinned cert.

**No traffic appearing:**
- Ask your agent to check the proxy status. The emulator's HTTP proxy should be pointing at `10.0.2.2:9101`.
