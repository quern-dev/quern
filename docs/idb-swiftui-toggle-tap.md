# idb Tap Fails on SwiftUI Toggle/Switch Controls

**Date:** 2026-02-17
**Status:** Fixed
**Affected:** `idb ui tap` on iOS Simulator SwiftUI Toggle elements

## Problem

`idb ui tap` at the correct coordinates silently fails to activate SwiftUI Toggle (switch) controls. The tap command returns success but the toggle does not change state. This affects Settings app toggles and any SwiftUI `Toggle` view.

## Root Causes

Two independent issues combined to make `tap_element` fail on toggles:

### 1. Missing `--duration` flag on idb tap

idb's default tap (no `--duration` argument) does not reliably register as a touch event on SwiftUI Toggle controls. The touch down/up cycle is too fast or malformed for the SwiftUI gesture recognizer.

**Fix:** Always pass `--duration 0.05` (50ms) on all `idb ui tap` calls. This is long enough for SwiftUI to recognize the touch but short enough to feel instant.

**Tested durations:**
| Duration | Result |
|----------|--------|
| (none)   | Fails on toggles |
| 0.05s    | Works |
| 0.1s     | Works |
| 0.15s    | Works |
| 0.5s     | Works |
| 1.0s     | Works (but slow) |

Any explicit duration works. 0.05s was chosen as the minimum reliable value.

### 2. `tap_element` hits the center of the row, not the switch knob

idb exposes iOS toggles as `CheckBox` elements (role: `AXCheckBox`, subrole: `AXSwitch`) with a frame spanning the entire settings row (label + switch). The previous `tap_element` implementation tapped the center of this frame, which lands on the label text area. While tapping the label works for some UIKit switches, it does not activate all SwiftUI toggles.

**Example from idb accessibility tree:**
```json
{
  "type": "CheckBox",
  "label": "AutoFill Passwords and Passkeys",
  "role": "AXCheckBox",
  "role_description": "switch",
  "frame": {"x": 16, "y": 151, "width": 370, "height": 52}
}
```

Center tap: x=201 (over the label text) -- fails
Right-side tap: x=330.5 (85% of width, over the switch knob) -- works

**Fix:** New `get_tap_point()` function detects CheckBox/Switch elements by type or `role_description` and offsets the tap to 85% of the frame width, targeting the actual toggle control.

## Files Changed

- `server/device/idb.py` -- `tap()` now passes `--duration 0.05`
- `server/device/ui_elements.py` -- New `get_tap_point()` with right-side offset for switches
- `server/device/controller.py` -- `tap_element()` uses `get_tap_point()` instead of `get_center()`

## Verification

Tested on iOS 18.2 Simulator (iPhone 17 Pro) with Settings > General > AutoFill & Passwords toggles. Both on and off transitions work reliably with both fixes applied. Neither fix alone is sufficient -- both are required.

## Notes

- The `ShowSingleTouches` simulator preference (Settings > Developer > Show Single Touches, or `defaults write com.apple.iphonesimulator ShowSingleTouches -bool true`) was initially suspected as a factor but is **not** required for the fix. It is a useful debugging aid for visually confirming where taps land.
- The `--duration` fix applies to all taps, not just toggles. This is intentional -- there is no downside to a 50ms duration and it may prevent similar issues with other SwiftUI controls.
- `get_tap_point()` falls back to `get_center()` for all non-switch element types, so existing behavior is preserved.
