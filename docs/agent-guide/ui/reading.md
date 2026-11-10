# Reading the screen

*`quern://guide/ui/reading` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

Use `get_screen_summary` for a curated text description or `get_ui_tree` for the full accessibility hierarchy. These are cheaper, faster, and easier to reason about programmatically than screenshots.

Screenshots are still useful — for verifying visual layout, catching rendering bugs, documenting state for humans, or when you need to see something the accessibility tree doesn't capture. Use both, but reach for structured data first when you need to make decisions or find elements to interact with.

Both `take_screenshot` and `take_annotated_screenshot` support a `save_path` parameter to write the image to disk instead of returning base64 — useful when handing off files to other tools (e.g., opening in Preview) or attaching to reports.

**UI Tree**: Use `get_screen_summary` with a reasonable `max_elements` limit. If you need detail, scope `get_ui_tree` with `children_of` to a specific container rather than fetching the full 500+ element hierarchy.

**"I need to see what's on screen"**
- Quick overview: `get_screen_summary`
- Full detail: `get_ui_tree`
- Visual for humans: `take_screenshot`
- Accessibility overlay: `take_annotated_screenshot` — draws bounding boxes on interactive elements, useful for debugging why `tap_element` can't find an element

**"The screen looks empty, or I know it's a web view"**
- `get_web_content` — reads `WKWebView` content the accessibility tree cannot see, with real screen coordinates
- iOS simulator only; on Android and physical iOS the normal `get_ui_tree` already covers web views
- Optional `bundle_id` when several apps are running, and `udid` to target a specific simulator
- Re-read after every interaction; the merged results are dropped when the screen changes
