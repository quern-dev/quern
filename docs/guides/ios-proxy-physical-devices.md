# Physical Device Proxy Setup

Capturing HTTPS traffic from a physical iPhone or iPad is more involved than simulators because the device is on a separate network and needs manual configuration. But the payoff is seeing exactly what your app does on real hardware.

## How It Works

Your iPhone sends its traffic through your Mac's mitmproxy instance:

```
iPhone (Wi-Fi) → Mac (mitmproxy on port 9101) → Internet
```

Three things must be true:
1. The mitmproxy CA certificate is installed and trusted on the device
2. The device's Wi-Fi is configured to use your Mac as an HTTP proxy
3. The device and Mac can reach each other over the network

## Certificate Installation

The cert needs to be installed *and* trusted — these are separate steps on iOS.

### Automated (Recommended)

If WDA is set up on the device, your agent can do the entire certificate installation flow automatically. It navigates Settings, installs the profile, and enables certificate trust — all through WDA's UI automation. Just ask:

> "Install the proxy certificate on my iPhone"

### Manual

If WDA isn't set up yet, or you prefer doing it yourself:

1. Open Safari on the device and go to `http://<your-mac-ip>:9101` (ask your agent for the URL — it knows your Mac's IP)
2. Download the certificate profile when prompted
3. Settings > General > VPN & Device Management > install the downloaded profile
4. Settings > General > About > Certificate Trust Settings > enable full trust for the mitmproxy CA

## Wi-Fi Proxy Configuration

On the device: Settings > Wi-Fi > tap the (i) on your network > Configure Proxy > Manual

- **Server**: Your Mac's IP (ask your agent — it detects the right interface automatically)
- **Port**: 9101
- **Authentication**: Off

After configuring, tell your agent what you've done so it can record the device's proxy config. This enables per-device traffic filtering and lets Quern detect when the config goes stale (e.g., you switch networks).

## The Split-Tunnel VPN Scenario

This is surprisingly common and worth understanding.

### The Setup

Your Mac has multiple network connections:
- **Ethernet or VPN**: Connected to the corporate network (10.x.x.x)
- **Wi-Fi**: Connected to your home/office network (192.168.x.x)
- Your **iPhone**: Also on Wi-Fi, same 192.168.x.x subnet

This happens when:
- You're at home with a split-tunnel corporate VPN
- You're at the office with Mac on wired ethernet and phone on guest Wi-Fi

### What Quern Does

When your agent records the device's proxy config, Quern doesn't just use your Mac's primary IP. It finds the specific network interface that's on the same subnet as your device. So even with multiple active interfaces (VPN, ethernet, Wi-Fi), the proxy routes correctly.

### Why This Is Powerful

You can proxy your phone's traffic through a machine that's simultaneously on the corporate VPN. API calls to internal services go through the VPN; everything else goes direct. You get full visibility into traffic that's normally locked behind the VPN.

### Why You Should Be Careful

You're routing a device's traffic through a machine with corporate network access. **Rules of thumb:**
- Only proxy test devices, not your daily driver phone
- Don't leave the proxy configured on the device when you're done testing
- If you're sharing flow captures, scrub internal URLs and auth tokens
- Be aware that mock rules could accidentally affect internal service traffic

## Multi-Network Tracking

Quern tracks proxy configurations per Wi-Fi network. Your "Home Wi-Fi" config and your "Office Guest" config coexist. When you switch networks, your agent can detect that the stored proxy host doesn't match any current Mac interface and prompt you to reconfigure.

## Filtering Traffic by Device

Once proxy config is recorded, your agent can show you just the physical device's traffic, separate from everything else flowing through the proxy (simulator traffic, Mac traffic, other devices). Just ask:

> "Show me the network traffic from my iPhone"

## Troubleshooting

**No traffic appearing:**
- Ask your agent to check the proxy status — it'll tell you if the config looks stale
- Try loading `http://httpbin.org/get` in Safari on the device — does it work?
- If HTTPS fails but HTTP works, the cert isn't trusted. Check Settings > General > About > Certificate Trust Settings.
- If nothing loads, the device can't reach your Mac. Check firewall settings.

**Cert trust keeps resetting:**
- iOS sometimes untrusts user-installed CA certs after a software update. Reinstall and re-trust.
