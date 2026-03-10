# Physical Device Proxy Setup

Capturing HTTPS traffic from a physical iPhone or iPad through Quern's mitmproxy. This is more involved than simulators because the device is on a separate network and needs manual proxy configuration.

## How It Works

The device sends its traffic through your Mac's mitmproxy instance. The path is:

```
iPhone (Wi-Fi) → Mac (mitmproxy on port 9101) → Internet
```

For this to work, three things must be true:
1. The mitmproxy CA certificate is installed and trusted on the device
2. The device's Wi-Fi is configured to use your Mac as an HTTP proxy
3. The device and Mac can reach each other over the network

## Certificate Installation

The cert needs to be installed *and* trusted — these are separate steps on iOS.

### Automated (via WDA)

If WDA is set up on the device, Quern's agent can do the entire flow automatically. See [physical-device-cert-setup.md](../physical-device-cert-setup.md) for the step-by-step WDA automation sequence.

### Manual

1. Open Safari on the device and navigate to `http://<your-mac-ip>:9101` (the proxy must be running)
2. Download the certificate profile
3. Go to Settings > General > VPN & Device Management > install the profile
4. Go to Settings > General > About > Certificate Trust Settings > enable full trust for the mitmproxy CA

## Wi-Fi Proxy Configuration

On the device: Settings > Wi-Fi > tap the (i) on your network > Configure Proxy > Manual

- **Server**: Your Mac's IP on the same network as the device
- **Port**: 9101
- **Authentication**: Off

After configuring, call `record_device_proxy_config(udid, ssid, client_ip)` to tell Quern about the setup. This enables per-device flow filtering and stale-config detection.

## The Split-Tunnel VPN Scenario

This is where things get interesting — and powerful.

### The Setup

Your Mac has multiple network interfaces active simultaneously:

- **Ethernet** (or VPN tunnel): Connected to the corporate network, subnet `10.x.x.x`
- **Wi-Fi**: Connected to a local network, subnet `192.168.x.x`
- Your **iPhone**: Also on Wi-Fi, same `192.168.x.x` subnet

This happens in two common situations:

1. **At home**: Mac on a split-tunnel corporate VPN (routes corp traffic through the tunnel, everything else through Wi-Fi). Phone on the same home Wi-Fi.
2. **At the office**: Mac on a wired ethernet connection to the corp network. Phone on the office's "guest" or "public" Wi-Fi (different subnet from the corp network).

### How Quern Handles It

When you call `record_device_proxy_config(udid, ssid, client_ip="192.168.1.42")`, Quern doesn't just store the Mac's primary IP. It finds the Mac interface that's on the *same /24 subnet* as the device's `client_ip`. So if your Mac has:

- `en0` (Ethernet): `10.0.1.50`
- `en1` (Wi-Fi): `192.168.1.100`
- `utun3` (VPN): `10.8.0.12`

And the phone reports `client_ip=192.168.1.42`, Quern correctly picks `192.168.1.100` as the proxy host — not the VPN or ethernet address.

This means the phone's proxy configuration points at the right interface, and traffic flows correctly even with multiple active network paths.

### Why This Is Powerful

You can proxy your phone's traffic through a machine that's simultaneously on the corporate VPN. The phone hits your Mac via Wi-Fi, mitmproxy decrypts and logs the traffic, then the outbound request goes through whichever Mac interface is appropriate for the destination (corp API via VPN, public internet via Wi-Fi).

This is incredibly useful for debugging apps that talk to internal APIs — you get full request/response visibility on traffic that's normally locked behind a VPN.

### Why This Is Dangerous

Think about what you're doing: you're routing a device's traffic through a machine that has access to the corporate network. If the device (or an app on it) makes requests to internal services, those requests will succeed — through your Mac's VPN connection.

**Rules of thumb:**
- Only proxy devices you control (your test devices, not your daily driver)
- Be aware that mock rules and intercepts could affect traffic destined for internal services
- Don't leave the proxy configured on a device when you're done testing
- If you're capturing traffic for a demo or sharing flow exports, scrub internal URLs and auth tokens

This isn't a security vulnerability — it's just a powerful tool that requires awareness. A woodworking router can do amazing things; it can also take off a finger.

## Multi-Network Tracking

Quern tracks proxy configurations per Wi-Fi network (SSID). When you call `record_device_proxy_config`, the config is stored under the SSID you provide. This means:

- Your "Home Wi-Fi" config and your "Office Guest" config coexist
- When you switch networks, `proxy_status` shows `wifi_proxy_stale: true` if the stored proxy host doesn't match any current Mac interface
- You just reconfigure the proxy and call `record_device_proxy_config` again for the new SSID
- Old SSID configs are preserved — next time you're on that network, it may still be valid

## Filtering Traffic by Device

Once `record_device_proxy_config` is called, you can filter flows by the device's IP:

```
get_flow_summary(client_ip="192.168.1.42")
query_flows(client_ip="192.168.1.42", host="api.example.com")
```

This isolates the physical device's traffic from everything else flowing through the proxy (simulator traffic, Mac traffic if system proxy is on, other devices).

## Troubleshooting

**No traffic appearing:**
1. Check `proxy_status` — is `wifi_proxy_stale: true`? Reconfigure.
2. Open Safari on the device and visit `http://httpbin.org/get` — does it load?
3. If HTTPS fails but HTTP works, the cert isn't trusted. Check Settings > General > About > Certificate Trust Settings.
4. If nothing loads, the device can't reach your Mac on port 9101. Check firewall settings.

**Traffic appears but can't filter by device:**
- Did you call `record_device_proxy_config`? The `client_ip` filter only works after recording.
- Did the device get a new DHCP lease? The IP may have changed. Check Settings > Wi-Fi > (network) > IP Address and re-record.

**Cert trust keeps resetting:**
- iOS sometimes untrusts user-installed CA certs after a software update. Reinstall and re-trust.
