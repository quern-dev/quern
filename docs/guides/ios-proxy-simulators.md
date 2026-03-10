# Simulator Proxy Setup

Quern can capture and display all HTTPS traffic from your iOS simulators. This is how you watch every API call your app makes in real time, without modifying your app's code.

## The Simple Version

Tell your agent:

> "Set up network capture for my simulator and show me what API calls my app is making"

Your agent will make sure the proxy certificate is installed on the simulator, enable local capture for your app's process, and start showing you traffic. That's it for most workflows.

## How Capture Works

### Local Capture (The Default)

Local capture uses mitmproxy's macOS System Extension to transparently intercept traffic from specific processes. Safari and WebKit networking are typically enabled by default (configured in `~/.quern/config.json`). When you start debugging an app, ask your agent to add your app's process name to the capture list.

**Why this is the right default:**
- Your Mac's browser and other apps are unaffected
- Nothing to clean up when you're done
- Traffic from each simulator is automatically tagged so you can tell them apart

The first time local capture is used, macOS will ask you to approve the System Extension in System Settings > Privacy & Security. This is a one-time approval.

### System Proxy (The Alternative)

If local capture isn't working (System Extension issues, corporate MDM blocking it), your agent can fall back to the system proxy. This routes *all* Mac traffic through mitmproxy — including your browser. It works, but it's messier.

**Important:** The system proxy must be turned off when you're done. Your agent handles this, but if the server crashes or you kill it, your Mac's proxy settings may be left configured. Run `./quern stop` to clean up, or check System Settings > Network > your interface > Proxies.

## Certificates

The proxy certificate needs to be installed on each simulator for HTTPS decryption to work. Without it, your agent can see that traffic is happening but can't read the request/response bodies.

Your agent installs this automatically when it sets up network capture. The cert persists across app installs and simulator reboots — it's only lost if you erase the simulator entirely.

If you've recently erased a simulator or created a new one, your agent may need to reinstall the cert. It detects this automatically.

## Per-Simulator Traffic Isolation

This is where Quern really shines for testing. When traffic flows through the proxy, Quern traces each request back to the specific simulator that made it. This means:

- Running the same app on three simulators? Traffic from each is labeled separately.
- Your agent can show you "just the traffic from the iPhone 15 Pro simulator" even when multiple simulators are active.
- Parallel test runners get isolated traffic views without any app-side changes.

This happens automatically — no configuration needed.

## What You Should Know

### Certificate Pinning

If your app uses certificate pinning (common in banking, healthcare, and security-sensitive apps), the proxy won't be able to decrypt that traffic. Pinned apps explicitly reject mitmproxy's certificate.

**Solutions:**
- Disable pinning in debug builds (most pinning libraries support this)
- Use a build configuration that skips pinning for development
- Accept that pinned traffic will appear as opaque HTTPS connections

### What Gets Captured

Everything that goes through the network stack: REST API calls, GraphQL, WebSocket connections, image downloads, analytics pings, third-party SDK traffic. You'll often discover surprising things your app (or its dependencies) are doing.

### Performance Impact

Local capture adds minimal overhead — a few milliseconds per request for the TLS interception. You won't notice it during normal development. System proxy mode has similar overhead but affects all traffic.

## Troubleshooting

**No traffic appearing:**
- Ask your agent to check the proxy status and verify the cert is installed
- Make sure local capture includes your app's process name (not just Safari)
- Check if your app uses certificate pinning

**Traffic appears but bodies are encrypted/empty:**
- The cert isn't installed or trusted. Ask your agent to reinstall it.

**System Extension prompt not appearing:**
- On macOS Ventura+, check System Settings > Privacy & Security > Network Extensions
- Corporate MDM policies may block System Extensions — fall back to system proxy mode
