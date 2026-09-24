# Android Proxy Setup

Capturing HTTPS traffic from Android emulators and physical devices. Android's certificate trust model is more restrictive than iOS, so the approach depends on whether you have root access.

## The Quick Version

Two separate steps, in this order. Routing first:

> "Point my Android device at the quern proxy"

That works on **any** device or emulator, rooted or not, and is enough on its
own to capture plain HTTP. It also has to come first, because the certificate
is served *by* the proxy.

Then, for rootable emulators (Google APIs images):

> "Install the proxy certificate on my Android emulator"

For non-rootable devices — including every physical phone — the certificate is
the part that needs work, and you'll need to modify your app. Keep reading.

## Why Android Is Different

Android separates certificate trust into two stores:

- **System certs**: Trusted by all apps. Read-only — requires root to modify.
- **User certs**: Installable without root, but **not trusted by apps** targeting API 24+ (Android 7+) unless the app explicitly opts in.

This means installing a cert through Android Settings doesn't help for debugging most modern apps. You either need root (to install as a system cert) or you need to configure your app to trust user certs in debug builds.

## Rootable Emulators (Automatic)

If your emulator uses a Google APIs image (not Google Play), your agent installs the certificate automatically. Behind the scenes, it:

1. Verifies the emulator is rootable
2. Converts the mitmproxy CA to Android's expected format
3. Installs it as a system certificate

It does **not** route the device through the proxy — that is
[its own step](#http-proxy), and it applies to every device rather than just
rootable ones.

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

Pointing a device at the proxy is its own operation, separate from installing
the certificate. Ask your agent to configure the proxy, or call
`record_device_proxy_config` with `apply=true`.

This works on **any** Android device or emulator, on USB or over the network,
and needs no root: it writes `settings put global http_proxy` over adb. Quern
picks the host address on the device's own subnet and the port the proxy is
actually listening on, and reads the network name and the device's IP off the
device rather than asking you to type them.

Do this **before** installing the certificate, not after. `mitm.it` is served
*by* the proxy, so a device has to be routed through it before it can fetch a
cert at all — and plain HTTP needs no certificate, so the proxy is useful on
its own.

Two things in the response are worth reading:

- `network_reattached` — the setting is only read when the network attaches,
  so quern bounces Wi-Fi. When this is `false` the setting is written but not
  yet in effect; reconnect the device and it will be. `hint` says what to do.
  Quern will not bounce Wi-Fi on a device whose adb connection runs over that
  same Wi-Fi, since that would cut the connection needed to turn it back on.
- `proxy_verified` — quern reads the setting back off the device and compares
  it, rather than assuming the write took.

### Physical devices

No different. The Settings > Wi-Fi > Modify network > Proxy route still works
by hand if you prefer, but nothing requires it.

## Cleanup

The proxy setting lives in Android's global settings and **survives reboots**,
so it stays until something changes it. A device left pointing at a proxy that
is no longer listening has no working network, so unset it when you are done:
call `record_device_proxy_config` with `clear=true`, which unsets the proxy and
forgets every network config quern recorded for that device.

The certificate is designed to persist and affects nothing outside the device.

## Troubleshooting

**"Not rootable" error:**
- Your emulator uses a Google Play image. Ask your agent to help you create a Google APIs emulator, or use the `networkSecurityConfig` approach.

**Cert installed but HTTPS still fails:**
- On API 34+, the injection may not have reached all app processes. Kill and relaunch the app.
- Check if the app uses certificate pinning — pinned apps reject any non-pinned cert.

**No traffic appearing:**
- Ask your agent to check the proxy status, and look at `proxy_verified` and
  `network_reattached` in the response to `record_device_proxy_config`. The
  device's proxy should point at your machine's address on the device's own
  network and at the port the proxy actually listens on — `10.0.2.2` is the
  emulator's alias for *its* host and is not reachable from a physical phone.
- If `network_reattached` is `false`, the setting is written but the device has
  not picked it up. Reconnect it to Wi-Fi.
