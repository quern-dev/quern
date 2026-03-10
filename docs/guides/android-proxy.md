# Android Proxy Setup

Intercepting HTTPS traffic from Android emulators and physical devices. Android's certificate trust model is more restrictive than iOS, so the setup depends on whether you have root access.

## The Certificate Problem

Android separates certificate trust into two stores:

- **System certs** (`/system/etc/security/cacerts/`): Trusted by all apps by default. Read-only partition — requires root to modify.
- **User certs**: Installed via Settings. Trusted by the browser, but **not trusted by apps** targeting API 24+ (Android 7+) unless the app explicitly opts in via `networkSecurityConfig`.

This means dropping a cert into the user store doesn't help for debugging most modern apps. You either need to install it as a system cert (requires root) or configure your app to trust user certs in debug builds.

## Rootable Emulators (Automatic)

If your emulator uses a Google APIs image (not Google Play), Quern handles everything:

```
install_proxy_cert(udid="emulator-5554")
```

This:
1. Verifies the emulator is rootable (`ro.build.tags == "dev-keys"`)
2. Converts the mitmproxy CA to Android's expected format (`<hash>.0`)
3. Installs it as a system certificate
4. Configures the HTTP proxy to `10.0.2.2:9101` (the host machine's loopback from the emulator's perspective)

After this, all HTTPS traffic from the emulator flows through mitmproxy and is fully decryptable.

### How Root Cert Installation Works

The technique depends on the Android API level:

#### API < 34 (Android 13 and below)

Classic remount approach:
```
adb root
adb remount
# push cert to /system/etc/security/cacerts/
```

Simple, reliable, well-documented.

#### API >= 34 (Android 14+)

Android 14 moved system certificates into an APEX module (`com.android.conscrypt`), making the old remount technique fail with "Device must be bootloader unlocked" — even on emulators with `adb root`.

Quern uses the **nsenter APEX injection** technique:

1. Copy existing APEX certs to a temp directory
2. Mount a `tmpfs` over `/system/etc/security/cacerts/`
3. Restore existing certs + add the new one
4. Find Zygote PIDs (`zygote` and `zygote64`)
5. Use `nsenter --mount=/proc/$PID/ns/mnt` to inject the mount into each Zygote and running app process's mount namespace

This is non-persistent — the cert is lost on emulator reboot. Quern detects this and re-installs as needed.

### Checking Installation

```
verify_proxy_setup(udid="emulator-5554")
```

Checks both `/system/etc/security/cacerts/` and `/apex/com.android.conscrypt/cacerts/` for the cert by hash.

### Idempotency

Calling `install_proxy_cert` twice returns `"already_installed"` on the second call — it checks before installing.

## Non-Rootable Emulators and Physical Devices

Google Play emulator images and physical devices (without Magisk/root) can't have system certs injected. You have two options:

### Option 1: Use a Rootable Emulator Instead

Create a Google APIs emulator (see [Getting Started](android-getting-started.md#creating-a-rootable-emulator)). This is the easiest path if you don't need Google Play Store specifically.

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

Then install the mitmproxy cert as a user certificate on the device:
1. Download from `http://mitm.it` (while proxy is configured)
2. Settings > Security > Encryption & credentials > Install a certificate > CA certificate

The `<debug-overrides>` block only applies to debug builds (`android:debuggable="true"`). Release builds ignore it entirely, so there's no security risk in shipping this config.

**This is actually the right approach for app development.** It explicitly declares your app's trust policy, works on any device, and doesn't require root. The rootable-emulator approach is better for ad-hoc debugging of third-party apps or when you can't modify the app.

## HTTP Proxy Configuration

### Emulators

Quern auto-configures the HTTP proxy when installing the cert:

```
adb shell settings put global http_proxy 10.0.2.2:9101
```

`10.0.2.2` is Android's special alias for the host machine's loopback address. Port 9101 is mitmproxy's default listen port.

This proxy setting persists across app restarts but may be cleared on emulator reboot. Quern re-applies it during cert installation.

### Physical Devices

Manual configuration required:

1. Settings > Wi-Fi > long-press your network > Modify network > Advanced options
2. Proxy: Manual
3. Proxy hostname: Your Mac's IP on the same network
4. Proxy port: 9101
5. Save

Same as iOS physical device setup — the device routes traffic through your Mac's mitmproxy instance.

## Cleaning Up

Unlike system proxy on macOS (which should be unconfigured after testing), the Android emulator proxy and cert are designed to persist. They don't affect anything outside the emulator. If you want to clear them:

```bash
# Remove proxy
adb shell settings delete global http_proxy
adb shell settings delete global global_http_proxy_host
adb shell settings delete global global_http_proxy_port

# Cert clears on emulator wipe/reboot (for nsenter installs)
```

## Troubleshooting

**`install_proxy_cert` fails with "not rootable":**
- Your emulator is using a Google Play image. Check with: `adb shell getprop ro.build.tags` — if it says `release-keys`, it's not rootable.
- Solution: Create a Google APIs emulator or use `networkSecurityConfig`.

**Cert installed but HTTPS still fails:**
- On API >= 34, the nsenter injection may not have reached all app processes. Try: kill and relaunch the app.
- Check if the app uses certificate pinning — pinned apps reject any non-pinned cert.

**`10.0.2.2` not reachable:**
- This only works from Android emulators, not physical devices.
- Verify mitmproxy is running: `proxy_status` should show the proxy as active.

**Proxy configured but no flows appearing:**
- Open Chrome in the emulator and visit any HTTPS site. If it loads but no flows appear, the proxy routing isn't working.
- Try: `adb shell settings get global http_proxy` — should show `10.0.2.2:9101`.
