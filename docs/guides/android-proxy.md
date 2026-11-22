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
the part that needs work, and you'll need to modify your app. The simplest way
puts quern's CA in your app's debug build, with nothing installed on the device
([Option 2](#option-2-trust-querns-ca-in-your-debug-build)). Keep reading.

## Which Path a Device Takes

Every Android device in `list_devices`, `list_remotes` and a reservation's answer carries `ca_install`, so you can tell before routing it -- and pick a device that suits, if you need HTTPS:

| `ca_install` | Which devices | HTTPS through quern |
|---|---|---|
| `automatic` | Rootable builds: Google APIs and AOSP emulator images | Your agent installs the certificate (`install_proxy_cert`) and every app's HTTPS is decrypted. On Android 14+ the install does not survive a reboot. |
| `manual` | Google Play emulator images, and phones | quern cannot install it in the system store. For your own app, put quern's CA in its debug build ([Option 2](#option-2-trust-querns-ca-in-your-debug-build)): nothing is installed on the device. Or install it as a user certificate in Settings (it needs a screen lock), and only apps that trust user certificates are decrypted. |
| `unknown` | quern could not read the build (still booting, or an image it does not recognise) | Ask again once the device has settled. |

A shut-down emulator's `ca_install` comes from the image its AVD names, so you can choose one without booting it.

When you route a device (`record_device_proxy_config` with `apply=true`), the answer's `https` says whether HTTPS will work there now: `ca_install`, `ca_trusted` (`true`, `false`, or `null` when quern cannot see -- a user certificate is kept where adb cannot read it), and a `note` saying what to do. Routing is never refused for want of a certificate: plain HTTP is captured either way.

**Routing ends with the hold.** A proxy applied by a session that holds the device is cleared when that hold ends -- a release, a closed agent, a `use_remote` switch -- and the device's network re-attached, so the next person to use it doesn't inherit a device pointing at a proxy that may be gone. The apply's answer says so (`ends_with_hold`); one applied without holding the device is left as it is. If another session of the same key holds the device by then, that session's release clears it instead; if another key holds it, it is left alone. A device that cannot be reached -- shut down, unplugged -- is tried three times and then given up on, with a note to clear it once it is back. The certificate stays installed. After `use_remote`, the MCP routes the Android devices of your next reservation on the new server.

**Session capture is for simulators.** `add_session_capture` names processes on the Mac, which an Android device's apps are not, so it refuses an Android device and points here. Filter a routed device's flows by `device_serial` (emulators) or `client_ip` (phones).

## Why Android Is Different

Android separates certificate trust into two stores:

- **System certs**: Trusted by all apps. Read-only — requires root to modify.
- **User certs**: Installable without root, but **not trusted by apps** targeting API 24+ (Android 7+) unless the app explicitly opts in.

This means installing a cert through Android Settings doesn't help for debugging most modern apps. You either need root (to install as a system cert), or you configure your own app's debug build to trust quern's CA: by bundling the CA in it, or by trusting user certs.

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

Google Play emulator images and physical devices (without root) can't have system certs injected. You have three options:

### Option 1: Use a Rootable Emulator Instead

Create a Google APIs emulator (see [Getting Started](android-getting-started.md#creating-a-rootable-emulator)). This is the easiest path.

Your agent will suggest this if you try to install a cert on a non-rootable device — it'll tell you the exact `sdkmanager` and `avdmanager` commands to create one. If you ask nicely, it might even run them for you.

### Option 2: Trust quern's CA in your debug build

The simplest route for your own app on a phone or a Google Play image: the debug build carries quern's CA and trusts it, so **nothing is installed on the device and no screen lock is needed**. Everything below lives in the debug-only source set, so a release build never sees any of it.

1. **Get quern's CA as PEM** and save it as `app/src/debug/res/raw/quern_ca.pem`. Fetch it from the quern server with `GET /api/v1/proxy/cert?format=pem` (no API key needed), or copy `~/.mitmproxy/mitmproxy-ca-cert.pem` on the server's Mac -- they are the same file. Gitignore it: it is that machine's CA. Its SHA-256 fingerprint should match the one `proxy_status` reports under `cert_setup`.

2. **`app/src/debug/res/xml/network_security_config.xml`:**
```xml
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config>
        <trust-anchors>
            <certificates src="system" />
            <certificates src="@raw/quern_ca" />
        </trust-anchors>
    </base-config>
</network-security-config>
```

   **If your app uses plain HTTP:** once an app has a network security config, Android ignores `android:usesCleartextTraffic`, and from API 28 the config refuses cleartext unless it says otherwise. An app that relied on that attribute would lose its HTTP requests in the debug build. Allow cleartext where it's needed, scoped to the hosts that need it:
```xml
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="true">dev.example.com</domain>
    </domain-config>
```
   inside `<network-security-config>`, or `cleartextTrafficPermitted="true"` on `<base-config>` if it needs HTTP everywhere. An app that uses only HTTPS needs nothing here.

3. **`app/src/debug/AndroidManifest.xml`**, merged over your main manifest:
```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application android:networkSecurityConfig="@xml/network_security_config" />
</manifest>
```

   **If your main manifest already sets `android:networkSecurityConfig`**, the debug manifest above conflicts with it when the manifests merge. Two ways out:
   - **Same resource name, no manifest change:** skip the debug manifest. Name the debug file after the one your main manifest points at, for example `app/src/debug/res/xml/production_network_security_config.xml`. The debug build's resource then replaces the main one. Copy your main config's rules into it (its `<domain-config>`s) and add `@raw/quern_ca` to its trust anchors, so the debug build keeps your policy and trusts quern too. A `<pin-set>` you copy still refuses quern for its domains, so leave it out of the debug copy if you need to see their traffic.
   - **A different name:** in the debug manifest, declare `xmlns:tools="http://schemas.android.com/tools"` on `<manifest>`, and add `tools:replace="android:networkSecurityConfig"` to `<application>`. The debug build then uses your debug file instead of the main one, so carry over any rules from the main config that you still want.

4. **Rebuild the debug APK and install it over the old one.** `install_app` upgrades in place; no clean install is needed. Then route the device with `record_device_proxy_config` and `apply=true`.

Measured on a Pixel 3 XL on Android 10, with adb over Wi-Fi: the app's HTTPS showed up as decrypted flows straight away. Two limits: an app or SDK that **pins certificates** still refuses the proxy, since this only adds a trust anchor; and if quern's CA changes (a new `~/.mitmproxy`), copy it in again and rebuild.

### Option 3: Trust user certificates (Debug Builds)

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

Then install the mitmproxy cert as a user certificate (Settings > Security > Install a certificate > CA certificate). Android asks for a screen lock before it will install one.

As with Option 2, a network security config replaces `android:usesCleartextTraffic`: if your app uses plain HTTP, allow it there with `cleartextTrafficPermitted` (see [Option 2](#option-2-trust-querns-ca-in-your-debug-build)).

The `<debug-overrides>` block only applies to debug builds. Release builds ignore it entirely — no security risk.

**Options 2 and 3 are the recommended approach for app developers.** They declare your app's trust policy explicitly, work on any device, and don't require root. Option 2 needs nothing on the device; Option 3 needs the certificate installed by hand. The rootable-emulator approach is better for ad-hoc debugging of apps you can't modify.

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

- `network_reattached` — on some devices the setting is only read when the
  network attaches, so quern bounces Wi-Fi. When this is `false` the setting is
  written, and may not be in effect yet; reconnect the device and it will be.
  `hint` says what to do. Quern will not bounce Wi-Fi on a device whose adb
  connection runs over that same Wi-Fi, since that would cut the connection
  needed to turn it back on. Such a device may use the proxy at once anyway --
  a Pixel 3 XL on Android 10 did -- so look for its flows before reconnecting
  it, which would drop the adb connection too.
- `proxy_verified` — quern reads the setting back off the device and compares
  it, rather than assuming the write took.

### Physical devices

No different. The Settings > Wi-Fi > Modify network > Proxy route still works
by hand if you prefer, but nothing requires it.

## Telling two emulators apart

Filter by `device_serial`, not `client_ip`. An emulator's traffic reaches the
proxy from the *host's* own address, because QEMU network-address-translates
it — so every emulator on one machine shares a single `client_ip`, and so does
anything else on the host. Filtering on it cannot separate two emulators, and
will also catch a `curl` you ran yourself.

`device_serial` is resolved from the process that owns the host socket the
connection came in on, so it names the emulator that actually made the
request. It is accepted by `query_flows`, `get_flow_summary` and
`wait_for_flow`.

Physical devices keep their own addresses and are unaffected either way, so
`client_ip` remains correct for them.

## Cleanup

The proxy setting lives in Android's global settings and **survives reboots**,
so it stays until something changes it. A device left pointing at a proxy that
is no longer listening has no working network, so undo it when you are done:
call `record_device_proxy_config` with `clear=true`. It puts back the proxy the
device had before quern's, such as a corporate or lab one, or unsets it if
there was none, and forgets every network config quern recorded for that
device.

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
- If `network_reattached` is `false`, the setting is written but the device may
  not have picked it up yet: some devices use it at once, others only after the
  network re-attaches. If no flows arrive, reconnect it to Wi-Fi -- which, over
  adb on Wi-Fi, drops the adb connection too, so connect it again after.
