# Simulator Proxy Setup

Capturing HTTPS traffic from iOS simulators through Quern's mitmproxy. This is the simplest proxy setup — most of it is automatic.

## Two Modes: Local Capture vs System Proxy

### Local Capture (Recommended)

Local capture uses mitmproxy's macOS System Extension to transparently intercept traffic from specific processes. No system proxy configuration needed — your Mac's browser and other apps are unaffected.

```
set_local_capture(processes=["MobileSafari"])
```

This tells mitmproxy to capture traffic from `MobileSafari` (or whatever process names you specify) without touching system network settings. The first time you use it, macOS will prompt you to approve the System Extension.

**Why this is better:**
- Your browser keeps working normally
- No cleanup needed — you can't "forget to unconfigure" anything
- Per-simulator flow tagging works automatically (see below)
- Multiple simulators captured simultaneously with isolated traffic

### System Proxy

The system proxy configures macOS network settings to route *all* traffic through mitmproxy. This captures everything — simulators, your browser, curl commands, everything on the active network interface.

```
configure_system_proxy     # Turn on
unconfigure_system_proxy   # Turn off — don't forget this
```

Use this when you need to capture traffic from processes whose names you don't know, or when local capture's System Extension isn't available.

**Important:** Always call `unconfigure_system_proxy` when you're done. If the server crashes or you forget, `proxy_status` will show the stale configuration, and `unconfigure_system_proxy` will restore your original settings (Quern snapshots them before configuring).

## Certificate Installation

Even with local capture, the mitmproxy CA certificate must be installed on the simulator for HTTPS decryption. Without it, you'll see flows but the request/response bodies will be encrypted and unreadable.

```
install_proxy_cert(udid="XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX")
```

Under the hood, this runs `simctl keychain add-root-cert` to inject the cert into the simulator's trust store. It's a one-time operation per simulator — the cert persists across app installs and simulator reboots. It's only lost if you erase the simulator.

### Verification

```
verify_proxy_setup(udid="...")
```

This does a ground-truth check: it queries the simulator's `TrustStore.sqlite3` database directly and verifies the cert fingerprint matches. It detects:
- Cert never installed
- Cert was installed but simulator was erased since then
- Cert is current and valid

Results are cached for an hour, so repeated checks are fast.

### Multiple Simulators

Install the cert on each simulator you want to capture from. The cert is per-simulator — booting a new simulator means installing again.

```
# For each simulator you're using:
install_proxy_cert(udid="<sim1-udid>")
install_proxy_cert(udid="<sim2-udid>")
```

## Per-Simulator Flow Tagging

This is where local capture really shines. When a network request flows through the proxy, Quern traces the originating process back through its parent chain to find the `launchd_sim` process that owns it. Each `launchd_sim` instance corresponds to exactly one simulator, and its command line contains the simulator's UDID.

The result: every captured flow is automatically tagged with `simulator_udid`.

### Filtering by Simulator

```
# See traffic from just one simulator
query_flows(simulator_udid="XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX")

# Summary for a specific simulator
get_flow_summary(simulator_udid="XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX")
```

This is critical for parallel testing. If you have three simulators running the same app hitting the same API, you can isolate each simulator's traffic without any app-side changes.

### How the Tagging Works

1. mitmproxy's local redirector captures the source PID of each connection
2. Quern walks the parent process chain (up to 10 levels) looking for a `launchd_sim` ancestor
3. When found, it extracts the UDID from `launchd_sim`'s command line
4. The UDID is cached, so subsequent flows from the same process resolve instantly

This all happens server-side — the simulator and your app have no idea it's happening.

## Quick Start

The typical workflow for an AI agent:

```
1. proxy_status                          # Verify proxy is running
2. install_proxy_cert(udid="...")        # Install cert (once per sim)
3. verify_proxy_setup(udid="...")        # Confirm it worked
4. set_local_capture(["MobileSafari"])   # Start capturing (if not already)
5. launch_app(udid="...", bundle_id="com.example.app")
6. # ... interact with the app ...
7. get_flow_summary(simulator_udid="...")  # See what happened
8. query_flows(simulator_udid="...", host="api.example.com")  # Drill down
```

## Troubleshooting

**No flows appearing:**
1. Is the proxy running? Check `proxy_status`.
2. Is the cert installed? Run `verify_proxy_setup`.
3. Is local capture active? Check `proxy_status` — the `local_capture` field should list your process names.
4. Is the app using certificate pinning? Pinned apps reject mitmproxy's cert regardless of trust store status. You'll need to disable pinning in debug builds.

**Flows appear but bodies are encrypted:**
- The cert isn't trusted. Run `install_proxy_cert` with `force: true` to reinstall.

**System Extension prompt not appearing:**
- macOS may block the prompt if System Preferences > Privacy & Security has pending approvals. Check there first.
- On macOS Ventura+, the extension may need explicit approval in System Settings > Privacy & Security > Network Extensions.

**Flows from wrong simulator:**
- If `simulator_udid` is null on some flows, the process parent chain couldn't be resolved. This can happen with background daemon processes that aren't children of `launchd_sim`. App-initiated traffic should always resolve correctly.
