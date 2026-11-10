# Common iOS Element Types

*`quern://app-knowledge-guide/element-types` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

**Where these names come from matters.** On a simulator, `get_ui_tree` normally reads the **accessibility tree** (sim-bridge, or idb as a fallback). XCUITest does not classify every element the same way, so a type that is right for driving quern can be wrong in an XCUITest selector — and nothing at runtime says why it failed. The sharpest case is tab-bar items: the accessibility tree reports them as `RadioButton`, while XCUITest sees `XCUIElementTypeButton` inside a `XCUIElementTypeTabBar`, so `app.radioButtons["Home"]` finds nothing.

**To see what XCUITest sees on a simulator, run `start_driver` on it.** While WebDriverAgent runs there, every read and action on that simulator goes through it, elements read from it (`get_ui_tree`, `get_element`, `wait_for_element`) carry `xcui_type` (XCUITest's own type, e.g. `XCUIElementTypeButton`), and `backend` on the response says `wda`. `stop_driver` returns the simulator to the default. Physical devices always go through WDA, so their elements always carry `xcui_type`.

What you'll see from the accessibility tree — the default on a simulator — and what these are for:

- **Navigation**: the tab bar, navigation bar and toolbar mostly appear as a generic `Group`, and a screen's title as a `Heading`. WDA names them `TabBar`, `NavigationBar`, `Toolbar` and `StaticText`.
- **Tab bar items**: often `RadioButton` in the accessibility tree; `XCUIElementTypeButton` to XCUITest. Correct in `tap_element element_type=...` and in landmarks — quern matches it against the `Button` WDA reports when a label or identifier also pins the element (see [Across backends](https://github.com/quern-dev/quern/blob/main/docs/screen-landmarks.md#across-backends)) — but **wrong in an XCUITest selector**. Tab items may use identifiers like `_TabName button` with a leading underscore.
- **Buttons**: `button`, `link` (for hyperlink-style buttons)
- **Text**: `staticText`, `textField`, `secureTextField`, `textView`
- **Containers**: `scrollView`, `table`, `collectionView`, `cell`
- **Toggles**: `switch`, `segmentedControl`
- **Indicators**: `activityIndicator`, `progressIndicator`, `image`

Element types and identifier patterns vary by app framework (UIKit vs SwiftUI) and how the developer set up accessibility. Don't assume — verify on each screen, **against the oracle that matches what you are writing**: `get_ui_tree` as it comes for driving quern (`tap_element`, landmarks), and `xcui_type` from a simulator in WDA mode for an XCUITest.
