# App Knowledge Base — Agent Guide

You have an `app-knowledge/` directory in this project for documenting the app under test. This guide explains how to build and maintain it.

## What This Is

The app knowledge base is a set of markdown files that capture everything you need to navigate, test, and debug this app: screen identification, navigation graphs, deep link shortcuts, device quirks, and domain terminology. It is optimized for agent consumption — precise, structured, and directly actionable with quern tool calls.

## Getting Started

If `app-knowledge/` was just initialized from templates, start with:

1. **Fill in `app.md`** — Ask the user for the bundle ID, URL scheme, and universal link domain. Use `list_apps` on a device where the app is installed to find the bundle ID if unknown.
2. **Launch the app** and begin the guided tour (see below).

If `app-knowledge/` already has content, read the existing files before making changes. Your job is to extend and correct, not overwrite.

## The Guided Tour

The guided tour is a collaborative process with the user. You explore the app together, and you document what you find.

### How It Works

1. **You drive the device.** Use quern tools to navigate the app — `get_screen_summary`, `tap_element`, `swipe`, etc.
2. **You describe what you see.** After each screen, summarize the key elements, navigation edges, and states you can identify.
3. **The user fills in the gaps.** They tell you the domain meaning, known quirks, edge cases, deep links, and things that aren't visible from the UI alone.
4. **You write the document.** Create or update the screen/flow/quirk file using the templates in `app-knowledge/`.

### Tour Order

Start from the app's entry point and work outward:

1. **Launch screen** — What does the user see on cold launch? Document it.
2. **Primary navigation** — Tab bar? Sidebar? Document `app.md`'s global navigation table.
3. **States and environments** — Before going deep, ask the user about app-wide states (auth, subscription, onboarding) and available environments (staging, production). Fill in `states.md` and `environments.md`. This shapes the rest of the tour.
4. **Each top-level screen** — Visit each tab/section. Document screens as you go. Create stubs for screens you discover but don't visit yet.
5. **Alerts** — As you encounter any dialog, popup, permission prompt, or coaching overlay, document it in `alerts/` immediately. Also ask: "Are there other popups I should know about on this screen?"
6. **Key flows** — After screens are documented, trace the most important user flows (login, core feature, settings changes) and document them.
7. **Deep links** — Ask the user: "Are there deep links or URL schemes I should know about?" Document each one, verify it works by launching with `launch_app url=...`.
8. **Quirks** — As you encounter anything unexpected, document it immediately. Also ask: "Are there any known quirks or device-specific issues with this screen?"

### What to Capture Per Screen

Use `get_screen_summary` for a quick overview, then `get_ui_tree` for the full element list. For each screen:

1. **Identification** — Find the most unique, stable element(s) that distinguish this screen. A navigation bar title is ideal. Avoid dynamic content.
2. **Key elements** — List interactive elements with their types and labels. Note which elements are conditional (appear only in certain states).
3. **Navigation edges** — Where does this screen lead? What action triggers each transition? Where can you arrive from?
4. **States** — What are the distinct states? (empty, loading, error, populated, etc.) How does the agent recognize each?
5. **Dynamic content** — If there's a list/feed/collection: what are the items? What do they represent? What happens when you tap one?
6. **Quirks** — Anything non-obvious. Ask the user.

### What to Ask the User

At each screen, consider asking:

- "What is this screen called internally? Any domain-specific terminology?"
- "Are there states I can't easily trigger right now?" (e.g., error states, empty states, permission prompts)
- "Are there deep links that reach this screen directly?"
- "Are there any popups, tooltips, or coaching overlays that can appear here?"
- "Does this screen behave differently for different user states?" (free vs premium, new vs returning, etc.)
- "Any known quirks — layout issues on small devices, timing problems, undocumented behaviors?"
- "What's the most important thing to test on this screen?"

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
- **Be precise about element types.** Use the exact types from `get_ui_tree`: `button`, `staticText`, `textField`, `secureTextField`, `tabBarButton`, `navigationBar`, etc.
- **Document failure modes.** What alerts, errors, or unexpected states can occur? How should the agent recover?

### Writing Flow Documents

Flows connect screens into goal-directed sequences. Write them after the relevant screens are documented.

- **Reference screens by link** — `[[screens/login]]`, not a description.
- **Each step = an action + a verification.** The agent should always confirm it arrived where expected before proceeding.
- **Include failure modes** with recovery steps.
- **Note shortcuts** — if a deep link can skip the first N steps, say so.

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
