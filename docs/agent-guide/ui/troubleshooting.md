# When something is wrong

*`quern://guide/ui/troubleshooting` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

**"No element found matching label"** — The element may not exist, the label may be wrong, or multiple elements match. Use `get_screen_summary` to see what's actually on screen, then refine your query with the exact label and an element_type.

**"Wait for element timed out"** — The element may never have appeared (a bug or wrong expectation), the timeout may be too short, or the label may differ from what you expect. Check what actually appeared with `get_screen_summary`.

**"Tap isn't registering / wrong element activated"** — Your coordinates may be off. Use `take_annotated_screenshot` to see element bounding boxes overlaid on the actual screen. Find your target element's bounding box, express its center as a fraction of the screen image dimensions, then multiply by the device's logical point size (e.g. iPhone 12: 390×844 pt, iPhone 15 Pro: 393×852 pt) to get the correct tap coordinates. This is the reliable calibration technique when `tap_element` can't find the element and coordinate taps are missing.
