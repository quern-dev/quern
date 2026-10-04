# Android UI Automation Spec

## Goal

Bring Android UI automation to parity with iOS. After this work, all MCP UI tools (`get_ui_tree`, `get_screen_summary`, `tap_element`, `tap`, `swipe`, `type_text`, `clear_text`, `press_button`, `wait_for_element`, `get_element_state`) work on Android emulators and physical devices.

## Background & Research

### How iOS works today

Two backends behind a common interface in `DeviceControllerUI`:

| | Simulators | Physical devices |
|---|---|---|
| **UI tree** | `idb ui describe-all` (subprocess) | WDA `/source` (HTTP on-device server) |
| **Input** | `idb ui tap/swipe/text` (subprocess) | WDA `/wda/tap`, `/wda/keys`, etc. |
| **Screenshots** | `simctl io screenshot` | pymobiledevice3 / CoreMediaIO |

Both return data normalized to `UIElement` (type, label, identifier, value, frame, enabled).

### Android options evaluated

**Raw ADB** (`uiautomator dump` + `input tap`):
- No dependencies, works everywhere.
- `uiautomator dump` is slow (1-3s), hangs on complex UIs (WebViews, Flutter, some Compose).
- `input tap` spawns a new JVM per command (~500-1000ms).
- `input text` is ASCII-only.
- No element waiting, no element-by-selector.

**openatx/uiautomator2** (Python library, v3.x):
- On-device HTTP RPC server started on-demand via ADB instrumentation.
- Element finding by text, resourceId, className, contentDescription, XPath.
- Unicode text input via clipboard. Built-in waits.
- ~100-300ms per operation. Pure Python (`pip install uiautomator2`).
- Architecturally mirrors WDA: on-device HTTP server controlled from host.

**scrcpy-server** (from Genymobile/scrcpy):
- Pushed as a JAR, executed via `app_process` with shell privileges.
- Screen capture: hardware H.264 via MediaCodec + virtual display. 30-60 FPS, ~70ms latency.
- Input injection: `InputManager.injectInputEvent()` via reflection. ~5-10ms, full multitouch.
- No UI tree / accessibility awareness. Pure visual + input.

**Appium UiAutomator2 Driver**: Too heavy (Node.js + Java SDK + Appium server). Designed for CI test suites, not interactive control.

**Device Farmer (STF)**: Architecture study. Used minicap (private SurfaceFlinger APIs, dead on Android 10+) and minitouch (`/dev/input` writes, blocked by SELinux on Android 10+). Key lesson: their approach is obsolete. scrcpy is the modern replacement for both screen streaming and input injection.

### Chosen approach: hybrid

| Layer | Purpose | Latency |
|-------|---------|---------|
| **uiautomator2** | UI tree, element queries, semantic interactions | ~100-300ms |
| **scrcpy-style input** | Fast tap/swipe/type when coordinates are known | ~5-10ms |
| **Raw ADB** | Device mgmt, app lifecycle, logcat (already done) | varies |

Phase 1 (this spec) focuses on uiautomator2 integration. scrcpy-based input and live preview are follow-up work.

## Architecture

### New component: `server/device/u2_client.py`

Async wrapper around `uiautomator2` that exposes the same interface as `IdbBackend` and `WdaBackend`:

```
DeviceControllerUI._ui_backend(udid)
    ├── iOS physical  → WdaBackend     (HTTP to WDA on port 8100)
    ├── iOS simulator → IdbBackend     (subprocess to idb)
    └── Android       → U2Backend      (HTTP to u2 on-device server)
```

### Interface contract

`U2Backend` must implement these methods to satisfy `DeviceControllerUI`:

```python
class U2Backend:
    async def describe_all(self, udid: str, snapshot_depth: int | None = None,
                           source_timeout: float | None = None) -> list[dict]
    async def describe_all_nested(self, udid: str,
                                  snapshot_depth: int | None = None) -> list[dict]
    async def describe_point(self, udid: str, x: float, y: float) -> dict | None
    async def tap(self, udid: str, x: float, y: float) -> None
    async def swipe(self, udid: str, start_x: float, start_y: float,
                    end_x: float, end_y: float, duration: float) -> None
    async def type_text(self, udid: str, text: str) -> None
    async def press_button(self, udid: str, button: str) -> None
    async def select_all_and_delete(self, udid: str, x: float, y: float,
                                     element_type: str | None = None) -> None
```

### UI tree normalization

uiautomator2's XML hierarchy uses different attribute names than iOS. The backend must normalize to idb-compatible dicts so `parse_elements()` produces valid `UIElement` objects.

| UIElement field | iOS (idb) source | Android (u2) source |
|---|---|---|
| `type` | `AXType` (e.g. "Button") | `className` stripped of package (e.g. `android.widget.Button` → "Button") |
| `label` | `AXLabel` | `text` or `content-desc` |
| `identifier` | `AXUniqueId` | `resource-id` (e.g. `com.app:id/btn_submit` → `btn_submit`) |
| `value` | `AXValue` | `text` (for EditText), checked state (for checkboxes) |
| `frame` | `AXFrame` `{x, y, width, height}` | `bounds` `[x1,y1][x2,y2]` → compute `{x, y, width, height}` |
| `enabled` | `AXEnabled` | `enabled` attribute |

### Class name mapping

Android widget class names should be simplified to match iOS-style type names where possible, for consistent MCP tool behavior:

| Android class | Mapped type |
|---|---|
| `android.widget.Button` | `Button` |
| `android.widget.TextView` | `StaticText` |
| `android.widget.EditText` | `TextField` |
| `android.widget.ImageView` | `Image` |
| `android.widget.ImageButton` | `Button` |
| `android.widget.CheckBox` | `CheckBox` |
| `android.widget.Switch` | `Switch` |
| `android.widget.ToggleButton` | `Toggle` |
| `android.widget.SeekBar` | `Slider` |
| `android.widget.Spinner` | `Picker` |
| `android.widget.ScrollView` | `ScrollView` |
| `android.widget.RecyclerView` | `ScrollView` |
| `android.widget.ProgressBar` | `ProgressIndicator` |
| `android.view.View` | `Other` |
| `android.view.ViewGroup` | `Group` |
| `androidx.compose.*` | Best-effort mapping from Compose semantics |

Unknown classes: strip package prefix, keep final class name (e.g. `com.google.android.material.chip.Chip` → `Chip`).

### Button mapping

The `press_button` MCP tool currently supports iOS buttons (HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY). For Android:

| Button name | Android keyevent | Notes |
|---|---|---|
| `home` | `KEYCODE_HOME` | Same semantics as iOS |
| `back` | `KEYCODE_BACK` | Android-only, critical |
| `recents` | `KEYCODE_APP_SWITCH` | Android-only |
| `volumeUp` | `KEYCODE_VOLUME_UP` | Same as iOS |
| `volumeDown` | `KEYCODE_VOLUME_DOWN` | Same as iOS |
| `power` | `KEYCODE_POWER` | Maps to LOCK/SIDE_BUTTON |
| `enter` | `KEYCODE_ENTER` | Useful for form submission |
| `delete` | `KEYCODE_DEL` | Backspace |

Use ADB `input keyevent` for buttons (no uiautomator2 needed — it's faster).

### Connection lifecycle

uiautomator2 v3 manages the on-device server lifecycle automatically:
1. First call to `u2.connect(serial)` pushes APKs if needed and starts the instrumentation server.
2. Server runs on a device port, forwarded via ADB.
3. Subsequent calls reuse the connection.
4. Server stops when the instrumentation process ends (or can be explicitly stopped).

`U2Backend` should:
- Lazily connect per-device on first UI operation.
- Cache connections keyed by serial number.
- Handle reconnection on stale connections (same pattern as WDA session recovery).
- Clean up connections when devices disconnect.

## Changes Required

### 1. Dependencies

Add `uiautomator2` to `requirements.txt` / `pyproject.toml`.

### 2. New file: `server/device/u2_client.py`

- `U2Backend` class with the interface above.
- `_connect(serial)` → lazy, cached `uiautomator2.Device` per serial.
- `_normalize_element(node)` → convert u2 XML node to idb-compatible dict.
- `_parse_bounds(bounds_str)` → `[x1,y1][x2,y2]` → `{x, y, width, height}`.
- Run all u2 calls in `asyncio.to_thread()` since uiautomator2 is synchronous.

### 3. `server/device/controller.py`

- Import and instantiate `U2Backend` alongside other backends.
- Add `self.u2 = U2Backend()` in `__init__`.

### 4. `server/device/controller_ui.py`

- Update `_ui_backend()` to return `self.u2` for Android devices instead of raising.
- Remove the Android `DeviceError` raise.

### 5. `mcp/src/tools/device-ui.ts`

- Update `press_button` tool description to include Android button names.
- No other MCP changes needed — the tools already pass through to the API.

### 6. `docs/guides/android-getting-started.md`

- Move "UI automation" from "Not Yet Supported" to "Supported".
- Add brief usage examples.

### 7. Tests: `tests/test_u2_backend.py`

- Unit tests for element normalization (bounds parsing, class mapping, label extraction).
- Mock uiautomator2 device for describe_all, tap, swipe, type_text.
- Integration test: Android device resolves to U2Backend.

## What stays unchanged

- `UIElement` model — no schema changes needed, Android data normalizes into existing fields.
- `parse_elements()` / `find_element()` / `generate_screen_summary()` in `ui_elements.py` — they work on idb-format dicts, which is what U2Backend produces.
- Ring buffer, proxy, log capture — unrelated.
- iOS backends (idb, WDA) — untouched.
- MCP tool parameter schemas — the tools are already platform-agnostic in their parameters.

## Follow-up work (not in this spec)

1. **scrcpy-based live preview** — Android equivalent of CoreMediaIO preview. Spawn `scrcpy-server` on device, receive H.264 stream, decode server-side for screenshots or relay to web preview.
2. **scrcpy-based fast input** — Use `InputManager.injectInputEvent()` for ~5ms tap/swipe latency instead of uiautomator2's ~100ms. Would require shipping scrcpy-server JAR and managing the socket connection.
3. **Annotated screenshots** — Overlay accessibility bounds on Android screenshots (same technique as iOS, just needs UI tree data — which this spec provides).
4. **Android location simulation** — `adb emu geo fix` for emulators.
5. **Android permission grants** — `adb shell pm grant` for emulators and physical devices.
6. **Emulator shutdown** — `adb emu kill`.
