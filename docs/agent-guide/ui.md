# UI automation: screens, taps and landmarks

*`quern://guide/ui` -- part of the Quern agent guide. Start from `quern://guide`.*

Reading what is on screen and acting on it.
## Reading the screen

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

## Tapping and gestures

Use `tap_element` with a label and element type instead of `tap` with raw coordinates. Accessibility-based taps work across screen sizes, survive layout changes, and are self-documenting.

**When coordinates are OK**: Gestures that aren't tied to specific elements (swipe to refresh, drag to reorder).

**When a tap is refused as obstructed** (iOS simulators): `tap_element` hit-tests the point it is about to tap. If something else is there -- a row scrolled under the navigation bar or a floating search field, a field under the edit menu or the keyboard -- or the element is off screen, it answers 409 with `status: "obstructed"`, `covered_by` naming what is in the way, and a `detail` with the remedy. Nothing was tapped. Scroll the element into view (`scroll_to_element`) or dismiss what is on top, then tap again; retrying unchanged will be refused again. A tap sent moments after another action waits for the screen to settle first (`waited_for_settle_ms`), because a transition still running swallows taps silently. A target still moving across that wait is tapped where it was last read, with `target_moving: true` and a `warning` -- read the screen before trusting that tap; on Android's fast path the wait is for the target itself (`waited_for_rest_ms`). On an iOS simulator a target that has moved off its point is not tapped at all: `obstructed` with `reason: "moving"`. A coordinate `tap` is never refused, but says what it `landed_on`. On a screen that never stops moving (a pulsing location dot, a spinner), pass `skip_stability_check` to `tap_element` or `skip_settle` to `tap`, or each wait runs to its timeout. A pass is not proof: something on top that fits entirely inside a large target, like a floating button over the middle of a card, reads as part of it.

**Long press and edge swipes**: pass `duration` (seconds; 1.0 is safe) to `tap` or `tap_element` for a long press -- context menus, drag handles, press-and-hold controls. Pass `edge` to `swipe` for a swipe the system takes: back from the left, home from the bottom, Control Centre or notifications from the top. Start the swipe at that edge (`start_x: 1` for the left) or it is refused. On a simulator an edge swipe needs sim-bridge, the default backend; idb, or WDA after `start_driver`, refuses it.

**A documented navigation tap that did not register is reported** (#491). With a knowledge base loaded, a `tap_element` whose element is a `leads_to` edge is remembered, at no cost. If a later `tap_element` misses, or `wait_for_element` times out, on the screen that tap is documented to *leave* -- with nothing having changed the screen in between (another tap, a swipe, a button, a launch) -- the response carries `previous_tap`: what was tapped, where it should have led (`expected`), where you still are (`on`), and a `note`. That is usually the explanation for the miss: the earlier tap was dropped, which apps -- and WDA in particular -- do. Check the screen before repeating it, and never repeat blindly a submit, a purchase or a send. It is said once, only on an exact identification, not for a tap more than two minutes old, and not before the edge's `timeout:` (else one second) has passed, so a screen still arriving is not blamed. Its limit: quern does not know where the tap was made, so a label that is documented on one screen and also used, undocumented, on another can be blamed wrongly -- hence "probably".

**A miss says when the screen has not changed since your last tap** (#502), with or without a knowledge base. Every `tap_element` (except a long press) captures a frame of the screen first. If your next `tap_element` misses, or `wait_for_element` times out, with no screen change made through quern in between, at least a second after the tap (longer if the knowledge base gives the edge a `timeout:`), and the screen looks exactly as it did before that tap, the response carries `screen_unchanged_since_tap`: what was tapped, how long ago, the measured `change`, and a `note`. If the tap was meant to navigate, open or toggle something, it probably did not register. A tap meant to change nothing -- a label -- reports it too, which is still true and is only said on a miss. `previous_tap`, when there is one, is the more specific claim and replaces it. The comparison is near-exact, so a switch flipping or a digit changing counts as a change, and so does a spinner or a blinking caret: on an animated screen there is no note. The status bar is not compared. The frame costs one capture per tap -- measured, ~60ms on Android, ~90ms on a simulator, ~200ms on an iPhone 11 -- and `note_unchanged: false` skips it.

**To check at the tap instead**, pass `verify_navigation: true`. A documented tap from the screen you are on then waits for the screen it leads to and reports `navigation`: `{expected, from, arrived, waited_ms}`. `status` stays `ok`, because the tap was delivered, but `arrived: false` means the screen did not change as documented: `on` names where you are (`stayed: true` when it is still the starting screen), and a top-level `warning` says so. For a screen that is slow to arrive, the edge's `timeout:` in the knowledge base, or `navigation_timeout` on the call, lengthens the wait (3s by default). `arrived: null` means it could not tell -- the screen afterwards was unidentified or ambiguous (`unidentified`, `ambiguous`), every read failed (`read_failed`), no read began late enough after the tap to show where it went (`no_settled_read`), or only a `condition`al edge matched (`conditional`) -- and is not a miss. It costs a screen read before the tap and at least one after: measured, a few hundred milliseconds on a WDA simulator or Android, but 1.5-2s a tap on an iPhone and on sim-bridge. Use it where knowing at once matters more than speed.

**Two fingers, or a real double tap**: `gesture` does pinch (`scale`), rotate (`degrees`), two-finger pan, double tap and two-finger tap, centred on an element by `label`/`identifier` or on `x`/`y`. Use it for maps, photo viewers and zoomable canvases, and for a double tap: two `tap` calls each pay a round trip and can land outside the double-tap interval, reading as two single taps. Expect the app to report a little less than you sent -- a recogniser starts measuring only once the fingers pass its threshold, so `scale: 3` reads as about 2.6. Works on iOS simulators, physical iPhones and Android (which needs scrcpy installed; without it the 400 says so). On Android a pinch defaults to starting 30mm apart along the long axis, since many apps ignore fingers closer than ~27mm -- so a 3x spread may not fit on a phone screen, and the 400 names the point that would leave it.

**"I need to tap/interact with UI"**
- Known element: `tap_element` with label and element_type
- Coordinates (rare): `tap`
- Gesture: `swipe`
- Text input: focus the element, then `type_text`

## Waiting for the screen

Use `wait_for_settle` when you are waiting for the screen to stop changing rather than for a specific element — after a navigation, an animation, or before interacting with a web view, whose content is invisible to the accessibility tree so nothing else can tell you it has finished drawing.

It answers "has drawing stopped", not "has content arrived". A blank page still loading is perfectly still, and will be called settled in under two seconds. Raising the timeout does not help, because nothing is moving. So for a slow load, wait for the content first — poll `get_web_content` until it returns elements, or `wait_for_element` for a native one — and use `wait_for_settle` afterwards to let the result stop moving.

Check the `settled` field before acting on the result. `settled: false` means something is animating rather than loading — a spinner, a video, a carousel — and will never settle. Treat that as a fact about the screen, not a failed wait.

## Debugging UI issues

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

**If the element isn't on screen**: reach for `tap_element` or `scroll_to_element` rather than a manual `swipe` loop. `tap_element` sweeps for it on Android by default, but on iOS only on a screen recorded as `scrollable: true`; otherwise pass `scroll_to_find: true` (see *Reaching elements off screen* below). On Android the sweep's selector read avoids a tree dump only for an exact label or identifier with no other filters; a miss after sweeping, or any other query, falls back to a full read, which on the layouts below can move the screen. Be aware that reading the full UI tree can itself scroll the content — on Android's `CoordinatorLayout`/`RecyclerView` screens the accessibility traversal a dump performs pushes top controls out of view before your tap lands. Both scroll paths avoid the dump for exactly this reason, so prefer them over "dump, read coordinates, tap".

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

## Reaching elements off screen

**"The element I want is scrolled off-screen"**
- `tap_element` handles this: on a miss it scrolls the target into view and retries. `scroll_to_find` is tri-state — `true` always sweeps, `false` never, and **unset (the default)** sweeps on Android, while on iOS it asks the knowledge base and sweeps only on a screen recorded as `scrollable: true`.
- On iOS with no knowledge base loaded nothing is recorded, so a miss does *not* sweep and the response says `scrollability_unknown`. Pass `scroll_to_find: true` if you know the screen scrolls, or record it once (see `quern://app-knowledge-guide`) and every later tap gets it right for free.
- A sweep that runs is reported: the response carries the swipes and whether anything moved, so a gesture you did not ask for is never invisible. On Android it also stops early and says why: `ends` lists the directions found at their end, and `no_progress: true` means the screen did not move when swiped either way -- it does not scroll there, so the element is not off screen.
- Need it visible but *not* tapped (asserting on it, screenshotting it): `scroll_to_element`
- Both work on Android and iOS (simulator and physical), and both drive a bounded swipe loop that re-checks the target by selector rather than dumping the UI tree — a tree dump can itself scroll the target away, which is the bug this design exists to avoid
- Neither is a substitute for `swipe` when you want to scroll a *screen* rather than reach a known element

## Identifying Screens with Landmarks

When the question is "what screen am I on right now?" — for verifying navigation, gating actions, or driving recipe-style workflows — landmarks give you a deterministic answer that doesn't depend on label parsing or model swaps.

**Why use landmarks instead of just reading `get_screen_summary`?**

- **Deterministic.** Quern matches the live UI tree against authored selectors and returns `matched: <screen_name>` with `confidence: exact | ambiguous | none`. No prose interpretation.
- **Cross-platform.** The same landmarks work on iOS (sim-bridge / idb / WDA depending on device) and Android (uiautomator2) — selection state, identifiers, and labels are normalized to a single schema.
- **Stable across LLMs.** A workflow that asks "is this the cart screen?" gets the same answer regardless of which model is driving.
- **Authored once, reused everywhere.** The screen knowledge base is the source of truth; agents don't re-discover screen identity on every call.

**Workflow:**

1. Load landmarks from the app's knowledge base. In the app's own project you usually need not: the MCP server loads the project's `.quern/knowledge` at session start, and the session's instructions say whether it did. Set `QUERN_PROJECT_DIR` in the MCP server's environment to point it at another project. Elsewhere -- or to keep it loaded across restarts:
   ```
   load_landmarks(path="/Users/dev/myapp", remember=true)
   ```
   **From a remote quern or a client key** the MCP can't send a path the server can read, so it uploads the project's knowledge base at session start and loads it for your session only; `load_landmarks(path=...)` uploads the same way, and `remember=true` is refused there. The set ends with your session -- not when you release a device -- and follows you through a `use_remote` switch.
   In an app project with no knowledge base, the instructions say that too, and `identify_screen` answers `no_landmarks_loaded` with a `hint`. Suggest building one to the user -- `init_app_knowledge` and `quern://app-knowledge-guide` -- rather than starting it unasked: it is a guided tour with them, and it writes into their repo.
   Or pass landmarks inline (useful for ad-hoc identification):
   ```
   load_landmarks(app="...", landmarks={"home-tab": [{"element": "RadioButton", "identifier": "tab.home", "selected": true}]})
   ```
2. Call `identify_screen(app="...")` or — more often — pass `identify=true` to `get_screen_summary` to fold identification into a call you were already making.
3. Read the response. On a match, you get `matched`, `confidence: "exact"`, and `matched_landmarks` with per-landmark hit/miss detail. On no match, `partial_matches` lists every evaluated screen sorted by descending match count, *each with its full per-landmark results* — you can see exactly which selectors failed without re-running.

**Landmark schema** (in screen frontmatter or inline JSON):

| Field | Purpose |
|---|---|
| `element` | Element type, required (e.g. `Button`, `RadioButton`, `Heading`). Matched against `type`, which follows the backend that read the screen — a simulator's tab is `RadioButton` from the accessibility tree and `Button` through WDA. With an `identifier` or a `label` as well, quern matches the same element under either name, and says so with `matched_via: "RadioButton≈Button"`; a type-only landmark is matched exactly. Avoid `navigationBar`: the accessibility tree does not expose one, so anchor on the screen title as a `Heading` |
| `identifier` | Accessibility identifier, exact match (preferred — locale-independent) |
| `label` | Label text, case-insensitive exact match |
| `label_contains` | Substring match for elements with dynamic content in their label |
| `absent: true` | Element must NOT be present (use sparingly — rare) |
| `selected: true` | For tabs/switches/radios/checkboxes, element must be in the on/active state. Distinguishes "the Home tab is the selected one" from "a Home tab exists." |

**A knowledge base with no landmarks:** If `load_landmarks` returns `screens: 0` and a populated `skipped[]`, each entry says why. `legacy_format` means the file uses `identify_by:`, the field that preceded `landmarks:` and that the loader has never evaluated; the original entries come back in the response, so the rename can be done from it directly (keep `element`/`identifier`/`label`/`label_contains`/`absent`, turn `value: "1"` into `selected: true`, drop the rest). Check the result against the running app rather than translating blind — a knowledge base old enough to use that field is old enough to have drifted.

**Validating before relying on landmarks**: Run `validate_landmarks(app="...")` after loading. Reports collisions (two screens whose landmark sets overlap — one could be mistaken for the other) and screens with no landmarks. Fix collisions by adding a distinguishing element to one of the screens.

**Checking a knowledge base against the current conventions**: `load_landmarks` and `validate_landmarks` both return a `conventions` block — counts over every screen file, and each file not `current` with the landmarks at fault and why (for example a type-only landmark that matches on one backend only). A file's `landmark_conventions: 2` is what it is written for, not a claim it complies; the block is the computed answer. "Auditing a knowledge base" in `docs/screen-landmarks.md` is the procedure.

**When `confidence: "none"` on a known-good screen**: the landmarks are likely stale — the app has shipped UI changes since the knowledge base was authored. Surface this to the user before continuing; downstream automation built on top of stale landmarks will silently produce wrong results (you'll act on the wrong screen and misreport state). The fix is to navigate to the screen, run `get_ui_tree`, and re-author the landmarks block from what's actually there. See "Keeping Landmarks in Sync" in the knowledge-base authoring guide.

## Mistakes to avoid

**Forgetting element_type when label is ambiguous** — `tap_element(label="Cancel")` might match a StaticText instead of the Button. Specify `element_type="Button"` when the label might not be unique.

**Using `get_ui_tree` to debug missing elements** — When `tap_element` can't find an element, use `take_annotated_screenshot` to visually see what the accessibility tree detects overlaid on the actual screen. It's faster than reading through the full UI tree and immediately shows mismatches between visual layout and accessibility labels.

**Retrying a `tap_element` miss that says the screen does not scroll** — When a miss reports `reason: "screen_not_scrollable"`, that is an answer, not a failure to work around: the screen is recorded as not scrolling, so the element is not on it and sweeping cannot find it. Retrying with `scroll_to_find: true` costs two real gestures and reaches the same conclusion. Look at where you are instead. Each no-sweep reason carries a `detail` naming the remedy that actually works, and they are deliberately distinct — `screen_ambiguous` (landmarks loaded for two apps and a screen matches both; the colliding names are in `candidates`, so scope to one app), `scrollability_unknown` (nobody has recorded it — pass `scroll_to_find: true` now, and add `scrollable: true` to the screen's entry to make it automatic), `screen_unreadable` (the read failed; this one *is* worth retrying), and `needs_page_urls` (a web-identified app on a path that does not read URLs — pass `scroll_to_find` explicitly).

**Not clearing text before typing** — Use `clear_text` before `type_text` when a field has pre-existing content. Otherwise you'll append to whatever's already there.

**Letting iOS rewrite what you type** — On a simulator, iOS edits text as it is typed: auto-correction turned "qft" into "aft" and "teh" into "the", auto-capitalization makes "hi. there" "hi. There", smart punctuation curls quotes and turns `--` into a dash, and the "." shortcut turns a double space into ". ". `type_text`'s read-back is exact, so a rewritten field fails the call -- deliberately, because in a case-sensitive field the rewrite is the bug. Turn the rewriting off once, before the test, with `set_simulator_setting` (`auto_correction`, `auto_capitalization`, `smart_punctuation`, `period_shortcut` set to `off`), and `password_autofill` off to stop iOS's "Save Password?" sheet, which is invisible in the app's UI tree. Each change reboots the simulator, so it refuses with an error on a booted one unless you pass `reboot: true` -- do it before launching the app under test. The settings survive reboots and app-state restores; `erase_device` resets them. `get_simulator_settings` shows what is set and which iOS versions each setting was verified on.

**Not checking simulator backend availability** — Device management and screenshots use `simctl` (always available with Xcode). Simulator UI automation (`get_ui_tree`, `tap`, `swipe`, `type_text`, `clear_text`, `press_button`) requires either sim-bridge (Xcode 26+ / Apple Silicon, preferred) or idb (fallback). Physical device UI automation uses WDA (auto-started). Check `list_devices` response for tool availability.

You do not pick the backend and normally need not think about it — quern probes at start-up and re-probes every five minutes, so an Xcode change under a running server is picked up. What matters is that the two simulator backends behave differently in ways that surface as odd results rather than errors: sim-bridge holds a swipe at the end so a list stops where the finger does and sweeps about seven-tenths of the screen, while idb cannot hold one, so it flings and steps a quarter. `get_ui_tree`, `get_screen_summary` and `scroll_to_element` therefore return a `backend` field. When a sweep overshoots or a read looks wrong, check it first — it distinguishes "this backend behaved unexpectedly" from "I am not on the backend I assumed", which otherwise look identical.

## When something is wrong

**"No element found matching label"** — The element may not exist, the label may be wrong, or multiple elements match. Use `get_screen_summary` to see what's actually on screen, then refine your query with the exact label and an element_type.

**"Wait for element timed out"** — The element may never have appeared (a bug or wrong expectation), the timeout may be too short, or the label may differ from what you expect. Check what actually appeared with `get_screen_summary`.

**"Tap isn't registering / wrong element activated"** — Your coordinates may be off. Use `take_annotated_screenshot` to see element bounding boxes overlaid on the actual screen. Find your target element's bounding box, express its center as a fraction of the screen image dimensions, then multiply by the device's logical point size (e.g. iPhone 12: 390×844 pt, iPhone 15 Pro: 393×852 pt) to get the correct tap coordinates. This is the reliable calibration technique when `tap_element` can't find the element and coordinate taps are missing.
