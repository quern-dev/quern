# Workflow: Physical Device Setup from Zero

The first time you connect a physical iPhone to work with Quern, there are several one-time setup steps. Here's the complete flow — what you need to do, what your agent handles, and what to expect.

## Prerequisites

- iPhone or iPad connected to your Mac via **USB** (Lightning or USB-C)
- **Xcode** installed with your Apple Developer account signed in
- **Quern** running (`quern start`)
- **tunneld** installed for iOS 17+ devices (done automatically by `quern setup`)

## Step 1: Trust the Mac

When you first plug in the device, iOS shows **"Trust This Computer?"** on the device screen. Tap **Trust** and enter your passcode. Without this, the device is invisible to everything — Quern, Xcode, all of it.

## Step 2: Enable Developer Mode (iOS 16+)

On iOS 16 and later, developer mode must be explicitly enabled:

**Settings > Privacy & Security > Developer Mode > toggle on > restart device > confirm**

Your device will reboot. After it comes back, confirm the prompt to enable developer mode. This is a one-time step — it persists across reboots and updates.

## Step 3: Verify the Device Appears

> "List my devices — do you see my iPhone?"

Your agent should show the physical device in the list. If it doesn't appear:
- Check the USB connection (try a different cable or port)
- Make sure you tapped Trust
- On iOS 17+, check that tunneld is running: `quern tunneld status`

## Step 4: Set Up WDA (Quern Driver)

> "Set up WDA on my iPhone"

Your agent will:
1. Check your Xcode signing identities — if you have multiple teams, it'll ask which to use
2. Build WDA (first time takes a minute or two)
3. Install "Quern Driver" on the device

**If you're on a free developer account:** Your agent will warn you about the 7-day profile expiry and the App ID slot limits. See [WDA Guide](ios-wda.md#free-vs-paid-developer-accounts) for details.

**If the install fails with a trust error:** You need to trust the developer profile on the device: Settings > General > VPN & Device Management > [your name] > Trust. This only applies to free accounts.

## Step 5: Verify UI Automation

> "Take a screenshot of my iPhone and show me in Preview"

If you get a screenshot, WDA is working. Your agent can now tap, swipe, type, and read the screen.

> "What's on the screen right now?"

Your agent reads the accessibility tree via WDA and describes what it sees.

## Step 6: Set Up Network Capture (Optional)

If you want to see your app's network traffic:

> "Install the proxy certificate on my iPhone"

Your agent can do this automatically via WDA — it navigates Settings, installs the certificate profile, and enables trust. Or you can do it manually (see [Physical Device Proxy Setup](ios-proxy-physical-devices.md)).

After the cert is installed, configure the Wi-Fi proxy on the device. Your agent will tell you the IP and port to use.

## Step 7: Start the Live Preview (Optional)

> "Show me the device screen"

A live video preview window opens showing the device in real time. Useful for watching what happens when your agent interacts with the device.

On iOS 18+, the device may show a "headphones or other device?" dialog — tap **Other** to dismiss it.

## Step 8: Start Working

Everything's set up. You can now:

> "Build and install my app on the iPhone"
> "Launch the app and show me what API calls it makes"
> "Tap the login button and walk me through what happens"

## What Persists

After this setup, here's what survives across sessions:

| What | Persists? | Re-setup when? |
|---|---|---|
| Device trust | Yes (permanent) | Only after device restore/wipe |
| Developer mode | Yes (permanent) | Only after device restore/wipe |
| WDA build (paid account) | 1 year | Profile expiry or signing change |
| WDA build (free account) | 7 days | Every week — tell your agent to rebuild |
| Proxy certificate | Yes (permanent) | After iOS updates (occasionally untrusted) |
| Wi-Fi proxy config | Per-network | When you switch Wi-Fi networks |

Most of this is truly one-time. A returning session usually just means plugging in the cable and starting to work.
