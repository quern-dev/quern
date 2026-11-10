# Debugging UI issues

*`quern://guide/ui/debugging` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

1. Call `get_screen_summary` to see current state
2. Trigger the issue (tap, swipe, type)
3. Call `get_screen_summary` again to see what changed
4. If the result is unexpected, use `get_ui_tree` (optionally scoped with `children_of`) to inspect the full hierarchy

**Key insight**: Use summary for quick checks, full tree when you need details.

**An input call can come back `ok` with an advisory, and the advisory is the
real answer.** Xcode 27's Device Hub attaches a guest HID daemon to every
booted simulator, which disconnects the touch, button and keyboard services
quern drives and never reconnects them. Everything else keeps working — tree
reads, screenshots, `open_url`, app launches — so the device looks healthy
while nothing you do reaches it. When quern has read that state, `tap`,
`tap_element`, `swipe`, `gesture`, `type_text`, `clear_text` and `press_button` carry an
advisory on the response. Do not treat the tap as landed because the status
says `ok`: read the advisory, and if it is there, call
`restore_simulator_input` before continuing. That restarts SpringBoard, so
running apps are killed and you will need to relaunch — which is why it is
asked for rather than done for you. Nothing is said for a healthy device, or
for one whose state has not been read.

**A launch that reports success is now a launch that happened.** `simctl
launch` reports the launch it was *asked* for and returns a pid, so an app
that cannot start — on iOS 27, any app that has not adopted the UIScene
lifecycle — used to come back as launched while the screen stayed on
SpringBoard. Every later call then failed for a reason unrelated to the
cause, typically `tap_element` reporting "no element found". `launch_app` now
confirms the app is actually frontmost before reporting success, so a launch
failure surfaces where it happens. **That failure is not one to retry**: the
app cannot launch on that runtime, and retrying will produce the same result
more slowly.

**If the element isn't on screen**: reach for `tap_element` or `scroll_to_element` rather than a manual `swipe` loop. `tap_element` sweeps for it on Android by default, but on iOS only on a screen recorded as `scrollable: true`; otherwise pass `scroll_to_find: true` (see `quern://guide/ui/off-screen`). On Android the sweep's selector read avoids a tree dump only for an exact label or identifier with no other filters; a miss after sweeping, or any other query, falls back to a full read, which on the layouts below can move the screen. Be aware that reading the full UI tree can itself scroll the content — on Android's `CoordinatorLayout`/`RecyclerView` screens the accessibility traversal a dump performs pushes top controls out of view before your tap lands. Both scroll paths avoid the dump for exactly this reason, so prefer them over "dump, read coordinates, tap".

**Debugging the platform normalizer**: When `tap_element` or a landmark match doesn't behave as expected and you suspect the underlying source attributes aren't surfacing correctly (e.g. an Android tab that doesn't appear `selected`), call `get_ui_tree` with `include_raw=true` to get the raw provider attributes (full uiautomator2 XML on Android) on each element under `extra_attrs`. This is faster than dropping to `adb shell uiautomator dump` and stays inside the Quern API surface.

**If the screen looks empty apart from its chrome**: you are probably looking at
a `WKWebView`. On iOS simulators it is absent from the accessibility tree
entirely, so `get_screen_summary` shows the navigation bar and nothing else,
and it is easy to read that as a failed load or a blank screen. Call
`get_web_content` — it pairs the Web Inspector's view of the DOM with the
native tree so elements come back with real screen frames, which means you can
tap them like any other element.

The results are merged into subsequent UI reads, so `tap_element`,
`get_ui_tree` and `get_screen_summary` all see them and you can tap by label as
usual. That overlay is dropped as soon as anything changes the screen — a tap,
swipe, scroll or launch — so **call `get_web_content` again after each
interaction with the page** rather than reusing an earlier read. A tap against
stale web content is refused with reason `stale_web_content` instead of landing
somewhere wrong.

A lookup that misses on a web page says so. When Safari is in front, or the
native tree has collapsed to a bare `Application` (a presented web modal), a
miss from `tap_element`, `type_text`, `get_element_state`, `clear_text` or a
`wait_for_element` timeout tells you to call `get_web_content` first, and
`tap_element` and `wait_for_element` also carry it as `web_content_hint`. Read
that as "not visible to the tree yet", not "the field does not exist".

If the response reports `anchored: false`, the page was found but its position
on screen could not be confirmed, and the elements are withheld rather than
returned at a guessed offset. Treat that as "look again", not "no content".

Simulator only, and deliberately: Android's accessibility tree already descends
into `WebView`, and physical iOS devices are reached over a different
transport. On both, the ordinary UI tree is the right tool and this one has
nothing to add.
