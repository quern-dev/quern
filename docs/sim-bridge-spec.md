# sim-bridge: Native Simulator Control for Quern

> **Status: shipped in v0.13.** This doc is the original design spec — kept
> for the rationale and the technique notes (private-framework dlopen,
> token dispatcher pattern, IOHIDDigitizer path). The implementation lives
> in `tools/sim-bridge.swift` and `server/device/sim_bridge.py`. Behavior
> notes (button name normalization, server-side `objectAtPoint` probing,
> RadioButton/CheckBox in summaries, installer skip on Xcode 26+) accrued
> after the spec was written — see `CHANGELOG.md` for the running list.

## Problem

Quern currently depends on Meta's `idb` (idb_companion) for all simulator UI automation:
accessibility tree queries, tap/swipe/type gestures, and button presses. idb has several
pain points:

- **Aging project** — Meta's investment has slowed; compatibility with newer Xcode versions is fragile
- **Companion daemon** — idb_companion must be running and connected; reconnection logic adds complexity
- **Subprocess per call** — every `idb ui describe-all` or `idb ui tap` spawns a new process
- **Screenshots via simctl** — separate subprocess for each capture, no streaming capability
- **Xcode 26 breakage** — some idb operations fail or behave differently on iOS 26 simulators
- **Empty container bug** — idb's `describe-all --nested` returns empty children for nav/tab/toolbars,
  requiring Quern's multi-point probing workaround

## Solution

A new Swift helper binary (`sim-bridge`) that replaces idb for simulator control by using Apple's
private frameworks directly. Runs as a long-lived subprocess communicating via JSON Lines over
stdin/stdout — the same pattern as Quern's existing `ios-preview` helper.

## Prior Art

The techniques are drawn from [baguette](https://github.com/tddworks/baguette) (MIT license),
which itself credits [AXe](https://github.com/cameroncooke/AXe) and
[SilbercueSwift](https://github.com/nicktmro/SilbercueSwift) for the accessibility bridge pattern.
The IOHIDDigitizer tap/swipe path was developed in baguette to fix Xcode 26 regressions.

## Architecture

```
Python server (SimBridgeBackend)
    │
    ├── stdin  → JSON Lines commands
    │             {"cmd":"describe-ui", "udid":"...", ...}
    │
    └── stdout ← JSON Lines responses
                  {"ok":true, "tree":{...}}

tools/sim-bridge.swift
    │
    ├── AccessibilityPlatformTranslation.framework (dlopen)
    │     AXPTranslator + TokenDispatcher → accessibility tree via XPC
    │
    ├── SimulatorKit.framework (dlopen)
    │     IndigoHIDMessage* / IOHIDDigitizerDispatch → touch injection
    │     SimDeviceLegacyHIDClient → HID message transport
    │
    ├── CoreSimulator.framework (dlopen)
    │     SimServiceContext / SimDevice → device discovery and XPC
    │
    └── IOSurface + SimulatorKit framebuffer callbacks → screenshots
```

All private frameworks are loaded via `dlopen`/`dlsym`/`NSClassFromString` at runtime.
No compile-time linking to private frameworks. The binary compiles with only public
frameworks: Foundation, IOSurface, CoreGraphics, ImageIO.

## Capabilities

### 1. Accessibility Tree

Query the full accessibility tree or hit-test a point for any booted simulator.

**Command:**
```json
{"cmd": "describe-ui", "udid": "XXXX"}
{"cmd": "describe-ui", "udid": "XXXX", "x": 100, "y": 200}
```

**Response:**
```json
{
  "ok": true,
  "tree": {
    "role": "AXApplication",
    "label": "MyApp",
    "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
    "enabled": true,
    "children": [...]
  }
}
```

**Node schema:**
```
role: string          — AXButton, AXStaticText, etc.
subrole: string?      — AXSecureTextField, etc.
label: string?        — accessibilityLabel
value: string?        — accessibilityValue
identifier: string?   — accessibilityIdentifier
title: string?        — accessibilityTitle
frame: {x, y, width, height}  — device points
enabled: bool
focused: bool
hidden: bool
children: [node]
```

**Implementation:** `AXPTranslator.sharedInstance` with a `TokenDispatcher` installed as
`bridgeTokenDelegate`. Each query registers a UUID token, obtains the frontmost app's
translation object, stamps the subtree with the token, then walks the tree into `AXNode`
values. Hit-test fetches the full tree and does client-side recursive point-in-rect search
(deepest child wins).

### 2. Input Injection

Tap, swipe, type text, press buttons on any booted simulator.

**Commands:**
```json
{"cmd": "tap", "udid": "XXXX", "x": 100, "y": 200}
{"cmd": "tap", "udid": "XXXX", "x": 100, "y": 200, "hold": 0.5}
{"cmd": "swipe", "udid": "XXXX", "x1": 200, "y1": 400, "x2": 200, "y2": 100, "duration": 0.3}
{"cmd": "type", "udid": "XXXX", "text": "hello world"}
{"cmd": "button", "udid": "XXXX", "name": "home"}
{"cmd": "button", "udid": "XXXX", "name": "lock"}
{"cmd": "key", "udid": "XXXX", "code": 42}
```

**Response:**
```json
{"ok": true}
{"ok": false, "error": "Simulator not booted"}
```

**Supported buttons:** `home`, `lock`, `volumeUp`, `volumeDown`, `swipeToHome`,
`swipeToAppSwitcher`

**Implementation:**
- **Single-finger tap/swipe:** IOHIDDigitizer path (Xcode 26+). Build `IOHIDEvent`
  digitizer parent + finger child, wrap via `IndigoHIDMessageForTrackpadEventFromHIDEventRef`,
  patch target/edge bytes, dispatch via `SimDeviceLegacyHIDClient`.
- **Text:** Decompose each character to (keycode, modifiers), send via
  `IndigoHIDMessageForHIDArbitrary` on HID page 7.
- **Buttons:** `IndigoHIDMessageForButton` (home/lock) or
  `IndigoHIDMessageForHIDArbitrary` (volume, power).
- **Gesture buttons:** Synthesized swipes for swipeToHome, swipeToAppSwitcher.

Coordinates are in device points (same as accessibility tree frames).

### 3. Screenshots

Capture a single frame as JPEG from any booted simulator.

**Command:**
```json
{"cmd": "screenshot", "udid": "XXXX"}
{"cmd": "screenshot", "udid": "XXXX", "quality": 0.8, "scale": 2}
```

**Response:**
```json
{"ok": true, "data": "<base64 JPEG>", "width": 393, "height": 852}
```

**Implementation:** Register framebuffer callback on `SimDeviceIO` port
`com.apple.framebuffer.display`, capture first `IOSurface`, encode to JPEG via
`CGImageDestination`, return as base64. Optionally downscale via `CIContext`.

The framebuffer callback is registered on-demand and torn down after capture.
No persistent screen subscription for one-shot screenshots. (Streaming is out of
scope — that's ios-preview's domain for physical devices; simulator streaming could
be added later.)

### 4. Device Discovery

List booted simulators with state info. Supplementary to simctl — provides a fast
check without spawning a subprocess.

**Command:**
```json
{"cmd": "list"}
```

**Response:**
```json
{
  "ok": true,
  "devices": [
    {"udid": "XXXX", "name": "iPhone 17 Pro", "state": "booted", "runtime": "iOS 26.4"},
    {"udid": "YYYY", "name": "iPad Air", "state": "shutdown", "runtime": "iOS 26.4"}
  ]
}
```

**Implementation:** `SimServiceContext.sharedServiceContext` → `defaultDeviceSet` →
`availableDevices`, reading UDID, name, state, runtime via KVC.

## Wire Protocol

- **Transport:** stdin/stdout pipes managed by Python `asyncio.subprocess`
- **Format:** JSON Lines — one JSON object per line, newline-delimited
- **Request:** `{"cmd": "<command>", ...params}`
- **Response:** `{"ok": true, ...data}` or `{"ok": false, "error": "message"}`
- **Ordering:** Strictly sequential — one request, one response. No interleaving.
  (Concurrency is handled on the Python side by queuing requests.)
- **Lifecycle:**
  - On launch: emit `{"event": "ready"}` after frameworks are loaded
  - On stdin EOF: exit cleanly
  - On fatal error: emit `{"ok": false, "error": "..."}` and continue if possible

## Python Integration

### SimBridgeBackend

New file: `server/device/sim_bridge.py`

```python
class SimBridgeBackend:
    """Simulator UI backend using the sim-bridge Swift helper."""

    async def describe_all(self, udid: str) -> dict: ...
    async def describe_point(self, udid: str, x: float, y: float) -> dict: ...
    async def tap(self, udid: str, x: float, y: float, duration: float = 0.05) -> None: ...
    async def swipe(self, udid: str, x1, y1, x2, y2, duration: float = 0.3) -> None: ...
    async def type_text(self, udid: str, text: str) -> None: ...
    async def press_button(self, udid: str, button: str) -> None: ...
    async def screenshot(self, udid: str, quality: float = 0.7, scale: int = 1) -> bytes: ...
```

Manages the sim-bridge subprocess lifecycle:
- Compile on demand (same mtime-based caching as ios-preview)
- Launch on first use, keep alive for the server's lifetime
- Request queue with `asyncio.Lock` to serialize stdin/stdout pairs
- Auto-restart on process death
- Compile command: `swiftc -o <binary> sim-bridge.swift -framework Foundation -framework IOSurface -framework CoreGraphics -framework ImageIO`

### Backend Selection

In `server/device/controller_ui.py`, `_ui_backend()` gains a new preference order:

```python
async def _ui_backend(self, udid: str) -> UIBackend:
    if is_android(udid):
        return U2Backend(udid)
    if is_physical_ios(udid):
        return WdaBackend(udid)
    # Simulator
    if self._sim_bridge_available:
        return SimBridgeBackend(udid)
    if self._idb_available:
        return IdbBackend(udid)
    raise RuntimeError("No simulator UI backend available")
```

sim-bridge is preferred when available. idb remains as fallback.

### Screenshot Integration

`DeviceController.take_screenshot()` for simulators currently calls
`xcrun simctl io <udid> screenshot`. With sim-bridge available, it can route through
`SimBridgeBackend.screenshot()` instead — zero-copy IOSurface capture instead of
spawning a subprocess.

## Build Requirements

- macOS 15+ (Sequoia)
- Apple Silicon
- Xcode 26+ (for SimulatorKit / CoreSimulator private framework compatibility)
- Swift 6.1+ (ships with Xcode 26)

The binary will NOT work on Intel Macs or with older Xcode versions. This matches
baguette's requirements and reflects the reality that Apple's private frameworks
are architecture-specific.

## File Layout

```
tools/sim-bridge.swift          — Single-file Swift source (~1000-1200 lines)
server/device/sim_bridge.py     — Python backend (subprocess manager + protocol)
server/device/idb.py            — Existing idb backend (kept as fallback)
```

## Out of Scope

- **Frame streaming** — ios-preview handles physical device video; simulator streaming
  via IOSurface could be added later but is not needed for UI automation
- **Two-finger gestures** (pinch, pan) — rarely needed for testing; can be added later
  using the legacy IndigoHIDMessageForMouseNSEvent path
- **Keyboard modifiers** (Cmd+C, etc.) — can be added later
- **Log capture** — Quern already has its own log capture via `xcrun simctl spawn`;
  baguette uses the same approach so there's nothing to gain
- **Orientation control** — can be added later via SimulatorKit's orientation APIs
- **Replacing simctl for device lifecycle** — boot/shutdown/erase/install will continue
  using simctl; sim-bridge focuses on UI automation and screenshots

## Risks

1. **Private API stability** — Apple can change these frameworks at any Xcode release.
   Baguette already hit this with Xcode 26 breaking single-finger taps. Mitigation:
   the idb fallback remains available, and the community (baguette, AXe, XcodeBuildMCP)
   collectively tracks breakages.

2. **Code signing** — Some private APIs check the caller's code signature. `xcrun simctl
   spawn` works because simctl is Apple-signed. Our binary is ad-hoc signed.
   Baguette works fine ad-hoc, so this is likely not an issue for the APIs we use.

3. **Single-file complexity** — ~1200 lines in one Swift file is manageable (ios-preview
   is 720 lines) but approaching the limit. If it grows significantly, we could split
   into a small SPM package, but the single-file compile-and-cache approach is much
   simpler operationally.

## Success Criteria

- `describe-ui` returns equivalent data to `idb ui describe-all --nested` (same fields,
  same coordinate system)
- `tap`/`swipe`/`type`/`button` work on iOS 26.4 simulators
- Screenshots are byte-equivalent quality to simctl screenshots
- No idb dependency required for simulator UI automation
- Existing Quern MCP tools (`get_ui_tree`, `tap_element`, `take_screenshot`, etc.)
  work transparently with the new backend
- idb fallback works when sim-bridge binary can't be compiled (e.g., no Xcode)
