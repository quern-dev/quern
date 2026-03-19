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
3. **Each top-level screen** — Visit each tab/section. Document screens as you go.
4. **Key flows** — After screens are documented, trace the most important user flows (login, core feature, settings changes) and document them.
5. **Deep links** — Ask the user: "Are there deep links or URL schemes I should know about?" Document each one, verify it works by launching with `launch_app url=...`.
6. **Quirks** — As you encounter anything unexpected, document it immediately. Also ask: "Are there any known quirks or device-specific issues with this screen?"

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
- "Any known quirks — layout issues on small devices, timing problems, undocumented behaviors?"
- "What's the most important thing to test on this screen?"

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

## Maintaining the Knowledge Base

- **Update when the app changes.** If a screen gains new elements or changes layout, update the doc. The knowledge base lives in the repo and is versioned with the code.
- **Add quirks immediately.** When you encounter something unexpected, create a quirk doc right away, even if brief. A one-line quirk doc is better than no record.
- **Verify before trusting.** If you're reading an existing doc and something doesn't match what you see on screen, the doc is stale. Update it.
- **Keep `app.md` current.** Global navigation changes affect every flow.
