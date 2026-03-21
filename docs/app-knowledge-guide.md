# App Knowledge Base — Agent Guide

You have a `.quern/knowledge/` directory in this project for documenting the app under test. This guide explains how to build and maintain it.

## What This Is

The app knowledge base is a set of markdown files that capture everything you need to navigate, test, and debug this app: screen identification, navigation graphs, deep link shortcuts, device quirks, and domain terminology. It is optimized for agent consumption — precise, structured, and directly actionable with quern tool calls.

## Getting Started

If `.quern/knowledge/` was just initialized from templates, start with:

1. **Fill in `app.md`** — Ask the user for the bundle ID, URL scheme, and universal link domain. Use `list_apps` on a device where the app is installed to find the bundle ID if unknown.
2. **Launch the app** and begin the guided tour (see below).

If `.quern/knowledge/` already has content, read the existing files before making changes. Your job is to extend and correct, not overwrite.

## Before You Start

Prepare the device before launching the app. Skipping this causes unnecessary interruptions during the tour.

1. **Resolve a device.** Use `resolve_device` to find or boot a simulator (or connect a physical device).
2. **Pre-grant permissions.** On simulators, use `grant_permission` to pre-accept common permission prompts before launching:
   ```
   grant_permission bundle_id="..." permission="location-always"
   grant_permission bundle_id="..." permission="photos"
   grant_permission bundle_id="..." permission="camera"
   grant_permission bundle_id="..." permission="notifications"
   ```
   This prevents system alerts from interrupting the tour. Not all apps need all permissions — ask the user which ones the app requests, or grant the obvious ones and handle any remaining prompts as you encounter them.
   Note: `grant_permission` is simulator-only. On physical devices, you'll need to accept permission prompts via the UI during the tour.
3. **Disable password autofill.** On simulators, the password autofill/keychain dialog can appear over login fields and block interaction. Disable it before starting:
   ```
   # Navigate to Settings > Passwords > Password Options > toggle off AutoFill Passwords
   launch_app bundle_id="com.apple.Preferences"
   ```
   Or use `set_app_plist_value` if a simulator-level plist key is available. This is especially important for apps with login flows.
3. **Install the app** if needed (`install_app`) and verify with `list_apps`.
4. **Check proxy status** if network capture is relevant (`proxy_status`).

## The Guided Tour

This is a one-time investment in complete app coverage — not a quick overview. Expect the process to take one or more sessions, especially for apps with many screens. Don't optimize for speed; optimize for thoroughness. Document every screen you can reach, even trivial settings sub-pages, because the goal is a complete map that future agents can rely on without gaps. If you run low on context, stop at a natural boundary and pick up in the next session — partial coverage that's thorough is better than rushed coverage that's shallow.

The guided tour is a collaborative process with the user. You explore the app together, and you document what you find.

### How It Works

1. **You drive the device.** Use quern tools to navigate the app — `get_screen_summary`, `tap_element`, `swipe`, etc.
2. **You document what you see.** Observe the screen, capture elements, edges, and states. Write the document based on what you can determine yourself.
3. **You present a summary and invite corrections.** After writing the doc, share a brief summary with the user: "Here's what I documented for this screen — anything I got wrong, missed, or that you'd add?" The user's context comes in bursts, not per-question — let them correct and add rather than answering a checklist.
4. **You incorporate their input.** Update the doc with domain knowledge, quirks, and context the user provides.

Avoid asking a long list of questions at every screen. The user knows the app — present what you found and let them fill gaps naturally. Save specific questions for things you genuinely can't determine from the UI (deep links, suppression flags, internal terminology).

### Tour Order

Start from the app's entry point and work outward:

1. **Launch screen** — What does the user see on cold launch? Document it.
2. **Primary navigation** — Tab bar? Sidebar? Document `app.md`'s global navigation table.
3. **States and environments** — Before going deep, ask the user about app-wide states (auth, subscription, onboarding) and available environments (staging, production). Fill in `states.md` and `environments.md`. This shapes the rest of the tour.
4. **Each top-level screen** — Visit each tab/section. Document screens as you go. Create stubs for screens you discover but don't visit yet. As you encounter domain-specific terms (codes, ratings, feature names, acronyms), add them to `glossary.md` — don't wait until the end.
5. **Alerts** — As you encounter any dialog, popup, permission prompt, or coaching overlay, document it in `alerts/` immediately. Also ask: "Are there other popups I should know about on this screen?"
6. **Key flows** — After screens are documented, trace the most important user flows (login, core feature, settings changes) and document them.
7. **Deep links** — Ask the user: "Are there deep links or URL schemes I should know about?" Document each one, verify it works by launching with `launch_app url=...`.
8. **Quirks** — As you encounter anything unexpected, document it immediately. Also ask: "Are there any known quirks or device-specific issues with this screen?"

### Common iOS Element Types

When exploring with `get_ui_tree`, you'll encounter these element types. Knowing what to expect saves trial-and-error:

- **Navigation**: `navigationBar`, `tabBar`, `toolbar`
- **Tab bar items**: Often `button` or `radioButton` (not `tabBarButton` — the actual tappable items inside a tab bar are frequently `radioButton` type). Tab items may use identifiers like `_TabName button` with a leading underscore.
- **Buttons**: `button`, `link` (for hyperlink-style buttons)
- **Text**: `staticText`, `textField`, `secureTextField`, `textView`
- **Containers**: `scrollView`, `table`, `collectionView`, `cell`
- **Toggles**: `switch`, `segmentedControl`
- **Indicators**: `activityIndicator`, `progressIndicator`, `image`

Element types and identifier patterns vary by app framework (UIKit vs SwiftUI) and how the developer set up accessibility. Don't assume — verify with `get_ui_tree` on each screen.

### What to Capture Per Screen

Follow this workflow for each screen: `get_screen_summary` first for quick orientation, then `get_ui_tree` for the full element list, then write the document, then move on to the next screen. This summary → tree → document → move on rhythm is the most efficient way to maintain momentum without missing details.

For each screen, capture:

1. **Identification** — Find the most unique, stable element(s) that distinguish this screen. A navigation bar title is ideal. Avoid dynamic content.
2. **Key elements** — List interactive elements with their types and labels. Note which elements are conditional (appear only in certain states).
3. **Navigation edges** — Where does this screen lead? What action triggers each transition? Where can you arrive from?
4. **States** — What are the distinct states? (empty, loading, error, populated, etc.) How does the agent recognize each?
5. **Dynamic content** — If there's a list/feed/collection: what are the items? What do they represent? What happens when you tap one?
6. **Quirks** — Anything non-obvious. Ask the user.

### What to Ask the User

Don't run through a checklist at every screen. Instead, present your documentation and let the user react. Reserve questions for things you genuinely can't determine from the UI:

- Deep links or URL schemes that reach this screen directly
- Internal terminology or domain-specific names (add these to `glossary.md`)
- States you can't easily trigger (error conditions, empty states, edge cases)
- Suppression behavior for popups ("does this appear every time or just once?")
- Known quirks the UI doesn't reveal (timing issues, device-specific problems)

### Stubs: Tracking Undiscovered Screens

As you document a screen, you'll find navigation edges leading to screens you haven't visited yet. Don't lose track of them — create a **stub**.

A stub is a minimal screen file that records "this screen exists and I know how to get there" without requiring a full visit. Use `screens/_stub.md` as the starting point.

**When to create a stub:**

1. You're documenting Screen A and find it leads to Screen B.
2. Before creating a stub for Screen B, check if a file already exists for it — search existing screen docs by name and `identify_by` fields.
3. If no match exists, create a stub: `screens/screen-b.md` with `status: stub`, the `reachable_from` edge you just discovered, and whatever you can infer about the screen name.
4. If a match exists (stub or documented), just add the new `reachable_from` edge to the existing file.

**When to upgrade a stub:**

When you visit the screen, replace the stub content with a full document (use `_template.md`). Change `status: stub` to `status: documented` and fill in all sections.

**Reconciling duplicates:**

It's possible that the same screen gets stubbed twice from different edges before you realize they're the same destination. When you discover duplicates:

1. Keep the file with richer content (or the one with the better filename).
2. Merge `reachable_from` edges from both files.
3. Delete the duplicate.
4. Update any `leads_to` references in other screen docs that pointed to the deleted file.

The `init_app_knowledge` tool reports stub vs. documented counts so you can gauge tour progress.

### Writing Screen Documents

Copy `screens/_template.md` and fill it in. Key principles:

- **`identify_by` is the most important field.** An agent lost in the app will check this to figure out where it is. Use the most unique, stable signals.
- **Include actual quern tool calls.** Don't write "tap the login button" — write `tap_element label="Sign In" element_type="button"`. The agent will copy-paste these.
- **Be precise about element types.** Use the exact types from `get_ui_tree` — don't guess. See "Common iOS Element Types" above.
- **Document failure modes.** What alerts, errors, or unexpected states can occur? How should the agent recover?

### Identifier Reliability

Accessibility identifiers in real apps are frequently misleading, reused, or missing. This is the biggest source of silent agent failures. Be defensive:

**Misleading identifiers.** An element's accessibility identifier may not match its visible label or purpose. For example, a "Quick Guide" button might have identifier `BackButton`, or a "Help Center" link might have identifier `Drafts`. When you document an element, always record the *visible label* alongside the identifier. If they differ significantly, flag it explicitly in the Notes column — a future agent trusting the identifier alone will tap the wrong thing.

**Shared identifiers.** Multiple elements on the same screen may share an identifier. For example, both "Go Premium" and "Redeem Code" might use `_Upgrade button`. When you discover shared identifiers:

1. Note it in both elements' documentation.
2. In tool calls, prefer `tap_element` with `label` (the visible text) rather than relying on the identifier.
3. If neither label nor identifier uniquely identifies the element, document the position or use a combination: element_type + label + parent context via `children_of`.

**Missing identifiers.** Some elements have no identifier at all. Use `label` (visible text) + `element_type` as the primary selector. Document this so agents don't waste time searching for an identifier that doesn't exist.

**These could be bugs.** Misleading, shared, or missing identifiers are usually not intentional — they're accessibility defects in the app code, typically fixable with a single line setting `accessibilityIdentifier`. When you discover one during the tour, document it immediately in `quirks/identifier-issues.md` and flag it to the user as something worth investigating right away. Don't just work around it silently. A short list of "these identifiers need attention" is high-value, low-effort work that improves both agent reliability and the app's general accessibility.

**General rule:** When writing `tap_element` commands in screen docs, use the most reliable selector you found during the tour — usually `label` for visible text. Add a comment if the identifier is known to be misleading or shared. The Key Elements table should capture both label and identifier so future agents can choose the right approach.

### Dynamic and Long Labels

Some elements have labels that include dynamic, variable-length content. For example, an attributes row might have a label like `"Attributes, Not recommended for kids, Stroller accessible, Dogs allowed, Available 24/7, Takes less than one hour, Park and grab, Parking nearby, Stealth required"` — a comma-separated list that changes per item.

This is a separate issue from identifier reliability. The label is *correct*, but it's not stable across instances.

**How to handle dynamic labels:**

- **Document the stable prefix.** If the label always starts with a fixed string (e.g., `"Attributes, "`), record that prefix and note that the rest varies.
- **Use partial matching.** In `tap_element`, use the stable portion of the label. If quern's `tap_element` requires an exact match, note in the doc that the agent should use `get_ui_tree` to find the element by prefix, then tap by coordinates or identifier instead.
- **In the Key Elements table**, write the stable portion followed by `...` and explain the dynamic part in Notes. Example: `Attributes, ...` with note "Comma-separated list of cache attributes, varies per cache."
- **Prefer identifier over label** for dynamic-label elements — this is the one case where identifiers are more reliable than labels, if one exists.

### Overlay Panels

Some UI doesn't fit neatly into "screen" or "alert" categories: map pin summary cards, bottom sheets, floating panels, and similar overlays that are persistent (not transient like alerts), interactive (they have navigation edges), but not full screens (no navigation bar, they overlay the parent screen).

**Document these inside the parent screen's doc** under a dedicated `## Overlay Panels` section. For each panel:

- How to trigger it (e.g., "tap a map pin")
- How to identify it (`identify_by` elements)
- Key elements within the panel
- Navigation edges (e.g., "tapping the panel title navigates to [[screens/cache-detail]]")
- How to dismiss it (swipe down, tap outside, back button)

This keeps them associated with the screen they overlay, while giving them enough structure that an agent can interact with them reliably. If an overlay is complex enough to warrant its own file (many elements, multiple states), promote it to a screen doc with a note that it's an overlay of the parent.

### Writing Flow Documents

Flows connect screens into goal-directed sequences. Write them after the relevant screens are documented.

- **Setup section first.** Before the steps, document how to reach the starting state: checkpoint to restore, plist flags to set (use `set_app_plist_values` to suppress tips/coaching in bulk), permissions to grant, plist watcher to start. Each precondition should be verifiable — the agent must be able to confirm it's ready before step 1.
- **Preconditions must be actionable.** "Logged in" is not enough. Specify how to verify ("tab bar visible") and how to get there if not ("restore checkpoint `logged_in` or run [[flows/login]]"). The agent will hit "no tab bar because I'm in a navigation stack" if you don't.
- **Each step = an action + a verification.** The agent should always confirm it arrived where expected before proceeding. Note any interceptors (alerts, coaching tips) that may appear at each step and how to dismiss them.
- **Reference screens by link** — `[[screens/login]]`, not a description.
- **Include failure modes** with recovery steps — these prove their value immediately when interceptors appear.
- **Teardown section.** Flows that create persistent side effects (test data, lists, plist flags, logs) should document how to clean up. Options: restore a checkpoint, delete created data, reset flags, or note "no cleanup needed."
- **Shortcuts section** — deep links, state restoration, or alternative paths that skip steps. Note which steps each shortcut replaces.

### Documenting Alerts

Alerts are dialogs, popups, permission prompts, info bubbles, and any transient UI that overlays a screen. They are the most common source of agent confusion — an unexpected alert blocks interaction with the screen underneath.

**When to create an alert document:**

- System permission prompts (location, notifications, camera, photos, tracking)
- App-specific dialogs that appear across multiple screens (rate prompts, upgrade nags, info popovers, coaching tips)
- Error alerts that can surface anywhere (network errors, session expiry)

**Don't document as alerts:** dialogs that only appear on one specific screen as part of its normal flow. Those belong in that screen's States section.

**What to capture:**

- **`trigger`** — What causes this alert. Be specific: "first time user taps a map pin" not just "using the map".
- **`appears_on`** — Which screens. Use `[]` if it can appear on any screen.
- **`identify_by`** — How the agent recognizes this alert. Alert titles and button labels are usually sufficient.
- **`actions`** — Every button/action available, what each one does, and which one the agent should choose by default.
- **Suppression** — Does the alert stop appearing after being dismissed? After N times? After a plist flag is set? This is critical for the agent to know.

**Ask the user:**

- "Are there any popups, tooltips, or coaching overlays that appear on this screen?"
- "Does this prompt appear every time, or only once / on first use?"
- "Can this be suppressed via a plist flag or app state?"

### Documenting App States

`states.md` defines the app-wide modes that affect what the agent sees and can do. These are the preconditions referenced in screen and flow documents.

**When to document a state:**

- It changes which screens are accessible (logged in vs out)
- It changes what elements appear on screens (premium vs free features)
- It changes app behavior in ways the agent needs to anticipate (FTUE vs normal, staging vs production)

**What to capture per state:**

- **How to Detect** — What the agent can check. Prefer programmatic detection: `read_app_plist` for flags, `get_screen_summary` for UI signals, specific elements that only appear in certain states.
- **How to Enter** — The fastest path. Prefer: deep link > `restore_app_state` > `set_app_plist_value` > manual flow. List all available methods.
- **How to Exit** — Same priority order.

**Fill in states early.** After the initial launch and first few screens, ask the user: "What are the major modes this app can be in? Auth states, subscription tiers, onboarding stages, environments?" Document these in `states.md` before going deep into screens — it makes the rest of the tour more structured.

### Documenting Environments

`environments.md` captures the available server environments and how to switch between them. Not all apps have multiple environments — if the app only targets one backend, skip this file.

**What to capture:**

- Base URL per environment
- How to switch (plist key, build variant, settings toggle, deep link)
- Which test accounts work where
- Behavioral differences (sandboxed payments, relaxed rate limits, test data)
- Any proxy/cert setup differences

**Ask the user early:** "Does this app have staging/dev environments? How do I switch between them?" This shapes everything else — test accounts, network capture setup, and which behaviors are real vs environment-specific.

## Maintaining the Knowledge Base

- **Update when the app changes.** If a screen gains new elements or changes layout, update the doc. The knowledge base lives in the repo and is versioned with the code.
- **Add quirks immediately.** When you encounter something unexpected, create a quirk doc right away, even if brief. A one-line quirk doc is better than no record.
- **Verify before trusting.** If you're reading an existing doc and something doesn't match what you see on screen, the doc is stale. Update it.
- **Keep `app.md` current.** Global navigation changes affect every flow.
