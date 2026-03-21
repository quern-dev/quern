# Workflow: Building an App Knowledge Base

This is the complete walkthrough for building a knowledge base from scratch — from first launch to saved checkpoints and executable test flows. It takes 1-2 hours for a medium-complexity app (20-40 screens), and the result is an artifact that saves that time back on every subsequent agent session.

## Phase 1: Setup (10 minutes)

### Pre-flight

Before you start the tour, prepare the simulator to minimize friction:

> "Grant location permission for my app, disable password autofill on the simulator, and boot a simulator"

Your agent handles the permissions (`grant_permission`), disables autofill (`xcrun simctl spawn ... defaults write -g AutoFillPasswords -bool NO`), and gets a device ready. This prevents system permission prompts and save-password dialogs from interrupting the tour.

If your app has test accounts, create one now:

> "Create a test account with enough activity to skip any first-time-user experience"

Apps often have onboarding flows that trigger below a certain activity threshold (e.g., fewer than 4 finds, no completed orders). A test account that clears these thresholds means the tour focuses on the real app, not onboarding.

### Initialize

> "Initialize an app knowledge base for this project"

Your agent scaffolds the `.quern/knowledge/` directory. The first files to fill in are `app.md` (bundle ID, URL scheme, universal link domains), `states.md` (auth, subscription, onboarding modes), and `environments.md` (staging vs production, how to switch).

Tell the agent these up front — they're things the agent can't discover from the UI:

> "The bundle ID is com.example.myapp. Staging uses staging.example.com, production uses api.example.com. There are Basic and Premium tiers — Premium unlocks filtering, lists, and statistics. Accounts with fewer than 3 orders see a first-time tutorial."

This shapes the entire tour. The agent now knows which features are gated, which screens depend on account state, and what interceptors to expect.

## Phase 2: The Tour (45-90 minutes)

### Launch and Orient

> "Launch the app, log in with the test account, and start documenting screens"

Your agent logs in, handles any onboarding screens, and arrives at the home screen. It documents the tab bar, primary navigation structure, and entry points. As it moves through screens, it creates documents for each one and stubs for screens it discovers but doesn't visit yet.

**Your job during the tour:** Watch the simulator and interject when you notice something the agent got wrong, missed, or wouldn't know. You don't need to answer questions — just correct and add:

- *"That screen has a detail section that expands when you tap the username"*
- *"The first time you interact with each item type, an explainer modal pops up"*
- *"That's a web view — the accessibility tree won't see the content inside it"*
- *"There's also a celebration screen that appears after every successful submission"*

These corrections become the most valuable entries in the knowledge base.

### Top-Level Screens First

The tour works from the outside in. Start with all the top-level screens — every tab, every settings section header — before diving into sub-screens. This builds the navigation graph first, so deeper exploration has a map to work from.

> "Visit each tab and document what you find, then go through Settings"

### Sub-Screens

After the top level is documented, work through the sub-screens systematically. Settings sub-screens, profile sections, detail views for the main content types. The agent can check its progress:

> "How many screens are documented vs stubs?"

`init_app_knowledge` reports the counts and lists which stubs still need visiting.

### Alerts as You Find Them

Every time an unexpected modal appears — stop and document it. Don't dismiss it and move on. Alerts are the single highest-value entry type because they're the most common cause of agent failure in future sessions.

> "Document that coaching tip as an alert before dismissing it"

For each alert, ask yourself: does this appear every time, or just once? Can it be suppressed via a plist flag? Which screens can it appear on?

### Premium and Alternate States

If your app has subscription tiers or role-based features, do a second pass with a different account type:

> "Sign out, create a Premium account, and log in. Let's see what's different."

Document which screens gain new elements, which locked features become accessible, and which new modals appear (coaching tips for premium features, onboarding for newly unlocked sections).

## Phase 3: Flows (30 minutes)

After screens are documented, trace the most important user journeys end-to-end. This is where the knowledge base proves its value — and where you'll discover interceptors that the screen-by-screen tour missed.

### Write the Flow

> "Write a flow document for logging in and reaching the home screen"

Your agent drafts the flow using the screen docs it already has: step-by-step tool calls with verification at each hop.

### Execute the Flow

> "Now actually execute that flow on the simulator"

This is the critical test. The flow will encounter things the screen docs didn't predict:

- A coaching tip that only appears during multi-step actions
- A celebration/survey screen after completing an action
- A permission prompt triggered by a specific feature, not at launch
- An action sheet that appears between two screens

Each of these becomes an alert document, and the flow document gets updated with the recovery steps. After execution, the flow is *verified* — an agent can follow it and it will work.

### Core Flows to Document

Most apps need these flows documented:

1. **Login** — from app launch to the home screen, handling onboarding, permissions, and environment selection
2. **The primary user action** — whatever the app's main purpose is (placing an order, posting content, completing a task)
3. **Navigation to a specific item** — the fastest path to view a particular piece of content
4. **Settings change** — toggling a preference that affects app behavior

Each flow should note shortcuts (state restoration, deep links) that skip early steps.

## Phase 4: State Discovery (20 minutes)

This is where the knowledge base goes from "navigation guide" to "test infrastructure."

### Find the State Flags

> "Read the app's preferences plist and look for flags that control coaching modals, onboarding state, or feature toggles"

Your agent reads the plist files in the app's data container and app group containers. Look for keys with patterns like:

- `hasShown*`, `hasSeen*`, `didComplete*` — coaching/onboarding flags
- `isStaging*`, `environment*` — server environment
- `featureFlags*`, `experimentSettings*` — feature toggles

Many apps store their coaching state in an app group container (shared with widgets/extensions) rather than the main app plist. If the main plist is mostly third-party SDK data, check for an app group:

> "Check for app group containers and read their plists too"

### Document the Flags

Create a quirk document mapping each flag to the behavior it controls:

- `kHasSeenWelcomeScreen` → onboarding slides
- `kHasSeenHelpPopUpTraditional` → cache type explainer for Traditional caches
- `kHasSeenFavoriteViaLogIntroMomentHelp` → favorite point coaching tip

Update the alert documents with suppression information — which flag to set, in which container, to prevent the alert from appearing.

### Verify the Round-Trip

Test that setting a flag actually changes behavior:

> "Set the coaching flag for the Mystery explainer to true, relaunch the app, and tap a Mystery item. Does the explainer still appear?"

If it doesn't appear — the flag works. The agent can now pre-configure any combination of coaching states.

### Save Checkpoints

Once everything is configured — all coaching modals dismissed, correct account logged in, environment selected — save the state:

> "Save the app state as 'premium-clean-all-tips-dismissed'"

This checkpoint is the foundation for all future testing. Restoring it takes seconds and puts the app in a known, clean state with no modals, no onboarding, no surprises.

Save multiple checkpoints for different scenarios:

- `logged-out-staging` — for testing login flows
- `basic-clean` — Basic account, all tips dismissed
- `premium-clean` — Premium account, all tips dismissed
- `fresh-install` — no state at all, for testing onboarding

## Phase 5: Using the Knowledge Base

### Live Agent Sessions

An agent starting a test session reads the knowledge base and immediately has:

- A map of every screen and how to reach it
- Stable element selectors for every interactive element
- Advance knowledge of every modal that might interrupt
- Pre-built flows for common tasks
- Checkpoint restoration for instant setup

Instead of:

> "Navigate to the profile screen"
> *(agent taps around, hits a modal, recovers, finds the tab bar, taps Profile)*

The agent executes:

```
restore_app_state label="premium-clean"
launch_app bundle_id="com.example.myapp"
tap_element label="Profile" element_type="radioButton"
```

### Programmatic Test Scripts

The knowledge base entries translate directly to test scripts. A flow document is essentially pseudocode for a test:

```python
# From flows/find-and-log-cache.md, step by step:
launch_app("com.example.myapp")
tap_element(label="Map", element_type="radioButton")
tap_element(identifier="_Search button")
tap_element(identifier="_Geocache button")
type_text("GC8AB9F\n")
wait_for_element(identifier="_Log button", timeout=10)
# ...handle cache type explainer if it appears...
tap_element(identifier="_Log button")
tap_element(identifier="_Found it button")
# ...handle favorite point tip if premium...
type_text("TFTC! Great cache.")
tap_element(label="Post", element_type="button")
# ...handle post-log celebration...
tap_element(identifier="_Continue button")
```

The knowledge base tells the script exactly which interceptors to handle and how. See [Agent-Generated Test Scripts](workflow-test-scripts.md) for the full pattern.

### State-Driven Test Scenarios

The plist flags documented in Phase 4 enable precise test scenarios:

**Test the onboarding flow:**
```python
# Delete all coaching flags to trigger fresh onboarding
delete_app_plist_key(key="kHasSeenWelcomeScreen")
delete_app_plist_key(key="kHasSeenDisclaimerScreen")
launch_app("com.example.myapp")
# Verify onboarding screens appear
```

**Test a specific coaching modal:**
```python
# Restore clean state, then clear one specific flag
restore_app_state(label="premium-clean")
delete_app_plist_key(key="kHasSeenFavoriteViaLogIntroMomentHelp")
launch_app("com.example.myapp")
# Navigate to log entry and verify the tip appears
```

**Test environment switching:**
```python
# Flip from staging to production via plist
set_app_plist_value(key="INTERNAL_isStagingServerEnvironmentKey", value=False)
launch_app("com.example.myapp")
# Verify production behavior
```

This is surgical control. Instead of manually navigating to trigger a specific state, you set exactly the flags you need and verify exactly the behavior you expect.

### Identifier Audit

The tour naturally produces an inventory of accessibility identifier issues — missing identifiers, shared identifiers, misleading identifiers. Collect these in a quirk document and share with the dev team. Each fix is usually a single line of code, and the return is immediate: more reliable automation for both agents and traditional UI tests.

## Maintenance

### When to Update

- **New screen added** — run a mini-tour for just that screen
- **Screen layout changed** — visit the screen, compare to the doc, update
- **New coaching modal added** — document the alert, find the plist flag
- **Accessibility identifiers changed** — update the screen doc and the identifier audit

### Staleness Detection

The knowledge base docs reference specific accessibility identifiers and element labels. When these change in the app code, the docs are stale. If your project has an `AccessibilityIdentifiers.swift` or similar central file, diffs to that file are a reliable signal that screen docs may need updating.

### Incremental Tours

You don't need to redo the full tour. When the agent encounters something on screen that doesn't match the knowledge base, it updates the doc. Over time, the knowledge base stays current through natural use — every agent session that touches a screen verifies and corrects the documentation for that screen.

## Tips

- **The first tour takes the longest.** Subsequent updates are much faster because the agent already has the navigation graph and can reach any screen directly.
- **Don't document everything to the same depth.** Core screens (home, detail views, login) deserve full documentation. Niche settings sub-screens might only need a stub with their `reachable_from` edge.
- **Flow testing is non-negotiable.** Flows find interceptors that screen-by-screen tours miss. Always execute your flows on the simulator before considering them done.
- **Save checkpoints aggressively.** A checkpoint takes seconds to create and saves minutes every time it's restored. Save one after any multi-step setup you don't want to repeat.
- **The plist discovery is worth the time.** 20 minutes mapping coaching flags gives you programmatic control over every modal in the app. That's a permanent capability.
- **Web view screens are a known limitation.** The accessibility tree can't see into web views. Document these prominently so agents know to use coordinate-based tapping or skip them.
- **Share the knowledge base with the team.** It's not just for agents — the identifier audit, state flag reference, and flow documentation are useful for any developer working on the app.