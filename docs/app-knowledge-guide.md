# App Knowledge Base — Agent Guide

You have a `.quern/knowledge/` directory in this project for documenting the app under test. This guide explains how to build and maintain it.

## What This Is

The app knowledge base is a set of markdown files that capture everything you need to navigate, test, and debug this app: screen identification, navigation graphs, deep link shortcuts, device quirks, and domain terminology. It is optimized for agent consumption — precise, structured, and directly actionable with quern tool calls.

## Getting Started

If `.quern/knowledge/` was just initialized from templates, start with:

1. **Fill in `.quern/config.json`** — This is the machine-readable project config. Set `bundle_id` and `app_name` first (use `list_apps` on a device where the app is installed if unknown). If you can find the Xcode project/workspace in the directory, set `workspace` and discover available schemes. Set `url_scheme` and `universal_link_domains` if the user provides them.
2. **Fill in `app.md`** — Entry points, global navigation, and test accounts. The bundle ID and URL scheme should match what you put in config.json.
3. **Launch the app** and begin the guided tour (see below).

If `.quern/knowledge/` already has content, read the existing files and `.quern/config.json` before making changes. Your job is to extend and correct, not overwrite.

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
3. **States and environments** — Before going deep, ask the user about app-wide states (auth, subscription, onboarding) and available environments (staging, production). Fill in `states.md` and `environments.md`. Also update `.quern/config.json`: add each environment to the `environments` object with its domain and deep link pattern.
4. **Each top-level screen** — Visit each tab/section. Document screens as you go. Create stubs for screens you discover but don't visit yet. As you encounter domain-specific terms (codes, ratings, feature names, acronyms), add them to `glossary.md` — don't wait until the end.
5. **Alerts** — As you encounter any dialog, popup, permission prompt, or coaching overlay, document it in `alerts/` immediately. Also ask: "Are there other popups I should know about on this screen?"
6. **Key flows** — After screens are documented, trace the most important user flows (login, core feature, settings changes) and document them.
7. **Deep links** — Deep links are not discoverable from the UI. Ask the user: "Does this app have deep links or universal links? Can you point me to the routing code or AASA file?" Extracting paths from source code is the fastest way to populate `deep-links/deep_links.json`. If source isn't available, the user can provide paths directly. Then verify each link on the simulator (see "Documenting Deep Links" below).
8. **Quirks** — As you encounter anything unexpected, document it immediately. Also ask: "Are there any known quirks or device-specific issues with this screen?"

### Updating config.json During the Tour

As you discover app details during the tour, keep `.quern/config.json` updated alongside the markdown knowledge base files:

- **Plist watch targets.** When you investigate plist files (app containers, app group containers), add watch entries to `plist_watch.watches`. Each entry needs `container` (e.g. `"data"`, `"group.com.example.app"`), `plist_path`, and optionally `ignore_prefixes` for noisy third-party SDK keys.
- **State flags.** When you discover plist flags that control coaching modals, onboarding state, environment switching, or feature toggles, add them to `state_flags`. Group them by category (e.g. `"coaching"`, `"environment"`) with the key name mapped to a human-readable description of what it controls.
- **Saved checkpoints.** When you save an app state checkpoint with `save_app_state`, add it to `saved_checkpoints` with the label as the key and a description of the state (account info, environment, what's been dismissed).

This structured data in config.json complements the prose in the knowledge base markdown files. Config.json is designed for future tool consumption — keeping it accurate means tools will eventually be able to auto-configure plist watches, restore checkpoints, and set state flags without parsing markdown.

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
2. Before creating a stub for Screen B, check if a file already exists for it — search existing screen docs by name and `landmarks` (or `identify_by` on legacy files).
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

- **`landmarks` is the most important field.** Quern evaluates it server-side against the live UI tree to answer "what screen am I on?" — it's the foundation of `identify_screen`, `get_screen_summary?identify=true`, and any future automation that needs to recognize where the agent is. Use the most unique, stable elements (nav bar titles, screen-specific identifiers).
- **`identify_by` is a human-readable hint and is optional.** Older screens have it; new screens don't need it unless you want a freeform prose note that doesn't fit the structured schema. The loader ignores it for matching. If you have both, they should agree.
- **Include actual quern tool calls.** Don't write "tap the login button" — write `tap_element label="Sign In" element_type="button"`. The agent will copy-paste these.
- **Be precise about element types.** Use the exact types from `get_ui_tree` — don't guess. See "Common iOS Element Types" above.
- **Document failure modes.** What alerts, errors, or unexpected states can occur? How should the agent recover?

### Choosing Landmarks

A landmark is an element selector that must be present (or absent) for a screen to be recognized. All landmarks for a screen must match — they're AND'd together. Pick the smallest set that uniquely identifies the screen. In order of reliability:

1. **Accessibility identifier** — locale-independent and usually stable across app versions. Strongest signal when one exists.
2. **Navigation bar title or unique button identifier** — one per screen, rarely changes.
3. **Tab bar selection state** — when a screen lives behind a tab, `{ element: "RadioButton", identifier: "tab.foo", selected: true }` distinguishes "this screen is active" from "this tab merely exists." Required when several screens share the same tab.* identifier pattern.
4. **Unique label text** — works for screens with no stable identifier. Locale-dependent; flag with a comment if the app is localized.
5. **`absent: true`** — sometimes the cleanest disambiguator is "the parent screen's compose button is *not* present." Use sparingly.

After authoring, run `validate_landmarks` (or the `quern validate` HTTP endpoint) on the knowledge base to catch overlapping landmarks. Two screens whose landmark sets are subsets of each other will collide — at least one needs a distinguishing element.

### Migrating a Legacy Knowledge Base

If `load_landmarks` returns `screens: 0` with a populated `skipped[]` array, the knowledge base predates the landmarks schema (PR #22, April 2026) and uses the older `identify_by:` field. The loader categorizes each skipped file with a `reason` so you (or an agent) can act on it:

- `legacy_format` — file has structured `identify_by:` entries that can be migrated. The original entries are echoed back in `skipped[].identify_by`.
- `no_landmarks` — file is a stub or unannotated. Add landmarks during the next visit.
- `yaml_error` / `no_frontmatter` / `invalid_entries` — file is malformed. Inspect manually.

For `legacy_format` files with structured dict entries, the migration is mostly mechanical:

- Rename the `identify_by:` key to `landmarks:`.
- Translate any `value: "1"` (legacy "selected") to `selected: true`. Drop `value: "0"` entries unless you specifically want a `selected: false` landmark.
- Drop fields that are not part of the new schema (`role_description`, etc.) — these were freeform hints that the loader ignored.

For `legacy_format` entries that are strings rather than dicts (freeform prose like `"SFSafariViewController showing server settings page"`), the original author was describing a state the schema couldn't express. Re-visit the screen with `get_screen_summary` and pick concrete landmarks that fit the structured schema; the prose is a starting point, not a 1:1 mapping.

Always review proposed migrations with the human user before rewriting files — landmarks may need adjustment for app changes since the legacy KB was authored. (See the agent migration skill for the per-file workflow.)

### Keeping Landmarks in Sync

The knowledge base is a living artifact, not a one-time setup. App teams ship UI changes — accessibility identifiers get renamed during refactors, copy gets rewritten, screens get redesigned — and landmarks that were correct last quarter can silently fail to match today. Mechanical correctness in YAML is no guarantee that the underlying app still exposes those elements.

**Signs that drift has occurred:**

- `identify_screen` (or `get_screen_summary?identify=true`) returns `confidence: "none"` for a screen you expect to match. Inspect `partial_matches` — if the screen you're on is sitting at 0/N or 1/N matched, the landmarks for it are stale.
- An agent's automation suddenly starts failing on screens that used to work — often surfacing as `tap_element` not_found errors with the legacy identifier.
- `validate_landmarks` reports collisions where there were none before — a sibling screen got redesigned and now overlaps with this one.

**How to fix drift:**

The procedure is identical to the migration skill's verification phase:

1. Navigate to the affected screen (use `reachable_from` from the screen file as the recipe).
2. `wait_for_element` on a likely-stable element to handle mid-transition states.
3. Call `get_ui_tree` (with `include_raw=true` if you need to debug the platform normalizer) to see what the screen actually exposes now.
4. Re-author the `landmarks:` block — drop selectors that no longer exist, swap in current identifiers, prefer structural elements (nav title, unique button identifier) over copy-dependent labels.
5. Re-run `load_landmarks` and `identify_screen` to confirm `confidence: "exact"`.

Drop the legacy `identify_by:` block at the same time, or update it alongside `landmarks:` — leaving it pointing at stale identifiers misleads anyone reading the file.

**Cadence and triggers:**

- After every major app release (a refactor, a redesign, a copy pass).
- Whenever an agent reports a sudden batch of `tap_element not_found` failures across screens that used to work.
- Whenever you're already touching a screen for another reason — opportunistic re-verification keeps drift small and easy to fix.

There's no scheduled audit or calendar-based heuristic; drift surfaces through the agent's normal use of `identify_screen`. When `confidence: "none"` appears, treat it as a maintenance signal, not a transient bug to ignore.

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

### Documenting Web Views

A screen built on web content looks almost empty to `get_ui_tree`: on iOS the
accessibility tree does not descend into a `WKWebView`, and the out-of-process
hosts are not in the app's hierarchy at all. The failure mode is a
misdiagnosis, not an error — a near-empty tree reads as "the screen failed to
load" or "every landmark drifted", and the agent goes looking for a problem
that isn't there.

Record it in the screen's `web_content:` block. The facts that matter are ones
only the source can answer, which is exactly what a knowledge base is for.

```yaml
web_content:
  - host: "WKWebView"
    in_process: true
    inspectable: "debug"
    url: "https://joinmastodon.org/servers"
    page_offset: [0, 100]
```

**`in_process` decides everything else.** When the app constructs the view, it
can opt it into remote inspection and the page's own DOM becomes available.
When the system constructs it — `SFSafariViewController`,
`ASWebAuthenticationSession` — the app has no handle on it, so that route is
closed no matter what the app does.

**`inspectable` is about the view, not the content.** Since iOS 16.4 a
`WKWebView` is only inspectable if the app sets `isInspectable` on it. That is
the app's decision about a view it owns, so it can be true even when the HTML
comes from a third party the team has no control over. Use `"debug"` when it is
behind `#if DEBUG`, which is where it belongs.

**Why record `page_offset`.** DOM geometry is viewport-relative, so it cannot be
tapped directly. One accessibility hit-test on any element recovers the offset
between page and screen space; writing it down saves doing that every session.
Re-derive it if the screen's layout changes.

**Note when the URL is third-party.** It can change without an app release —
the example above moved from `/communities` to `/servers` between knowledge-base
revisions. Prefer structural selectors (`h1`, `button[aria-label]`) over text
the site can reword.

None of this applies on Android, where the accessibility tree does descend into
a `WebView` and web content appears as ordinary elements.

### Overlay Panels

Some UI doesn't fit neatly into "screen" or "alert" categories: map pin summary cards, bottom sheets, floating panels, and similar overlays that are persistent (not transient like alerts), interactive (they have navigation edges), but not full screens (no navigation bar, they overlay the parent screen).

**Document these inside the parent screen's doc** under a dedicated `## Overlay Panels` section. For each panel:

- How to trigger it (e.g., "tap a map pin")
- How to identify it (`landmarks` for the panel, evaluated against the live UI tree)
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

### Documenting Deep Links

Deep links are stored as structured JSON in `deep-links/deep_links.json`, not as individual markdown files. This format serves both agents (reading descriptions and caveats) and test scripts (reading URLs and verification elements).

**Discovery:** Deep links are never found by exploring the UI. They come from:
1. **Source code** — deep link routers, URL handlers, AASA files, entitlements. Offer to extract paths from source as a fast path: "Can you point me to the routing code or AASA file? I can extract the deep link paths directly."
2. **The user** — "Here are our deep link paths."

**JSON schema:** Each entry in the `deep_links` array captures:

```json
{
  "name": "profile",
  "description": "Navigate to the user's profile screen.",
  "path": "/dl/profile",
  "lands_on": "screens/profile",
  "skips_screens": ["home"],
  "verify": {"identifier": "_profile_header"},
  "premium_gated": false,
  "caveats": ["May show onboarding on first visit"]
}
```

Key fields: `path` (appended to domain), `lands_on` (screen doc reference), `verify` (`wait_for_element` kwargs to confirm the landing screen), `premium_gated` (whether Basic accounts see an upsell), `caveats` (edge cases and gotchas).

Use separate arrays for different link categories (e.g. `coord_info_links`, `special_links`) when the app has distinct deep link families with different URL patterns or behaviors.

**Verification workflow:** After populating the JSON:

1. Open each link with `open_url` on a logged-in simulator.
2. Capture the landing screen and fill in the `verify` field with the identifying element.
3. Test with a Basic account to determine `premium_gated` status.
4. Add caveats as discovered (coaching modals, pending deep links after login, web views).

This can be semi-automated: open each link, capture the screen, propose the verification element. The user confirms or corrects.

**What we've learned about deep link testing:**

- **Universal links on simulators:** Always use `open_url` (quern tool). Raw `simctl openurl` often opens Safari instead of the app.
- **Pending deep links:** When a universal link is opened while logged out, the app may hold it as "pending" and execute it after login — landing on the deep link target, not the default home screen. Test setup must account for this.
- **Coaching modals:** Deep links to screens with first-visit coaching need those modals suppressed via plist. The `QUERN_AUTOMATION` env var prevents the app from wiping coaching flags on launch.
- **Per-account behavior:** The same deep link can behave differently on Basic vs Premium. Capture this in `premium_gated` and `basic_shows_upsell` fields.

## Maintaining the Knowledge Base

- **Update when the app changes.** If a screen gains new elements or changes layout, update the doc. The knowledge base lives in the repo and is versioned with the code.
- **Add quirks immediately.** When you encounter something unexpected, create a quirk doc right away, even if brief. A one-line quirk doc is better than no record.
- **Verify before trusting.** If you're reading an existing doc and something doesn't match what you see on screen, the doc is stale. Update it.
- **Keep `app.md` current.** Global navigation changes affect every flow.
