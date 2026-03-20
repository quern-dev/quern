# App Knowledge Base

When an AI agent tests your app, it spends most of its time *navigating* — figuring out how to get to the right screen, recovering from unexpected modals, re-orienting after a wrong tap. The app knowledge base eliminates that overhead by giving the agent a pre-built map of your app: every screen, every navigation path, every interceptor that might appear along the way.

## What It Is

A directory of structured markdown files that live in your repo alongside the app code. Each file documents one thing — a screen, a user flow, an alert, a quirk — in a format optimized for agent consumption. The agent reads these files at the start of a session and immediately knows how to get anywhere in the app, what to expect when it arrives, and how to handle surprises.

```
app-knowledge/
├── app.md              # Bundle ID, entry points, global navigation
├── states.md           # Auth, subscription tier, onboarding — app-wide modes
├── environments.md     # Staging vs production, how to switch
├── glossary.md         # Domain terminology
├── screens/            # One file per screen
│   ├── home.md
│   ├── product-detail.md
│   ├── login.md
│   └── ...
├── flows/              # Multi-step sequences (login, purchase, etc.)
├── alerts/             # Modals, dialogs, coaching tips that can appear unexpectedly
├── deep-links/         # URL schemes and universal links
└── quirks/             # Non-obvious behaviors, workarounds, device-specific issues
```

## Why It Matters

Without a knowledge base, every agent session starts from scratch. The agent taps around, reads the accessibility tree, and slowly builds a mental model of the app. It hits a subscription upsell modal and has to figure out how to dismiss it. It tries to find a specific screen and takes a wrong turn. Every session pays this orientation tax, and then throws it away when the session ends.

With a knowledge base, the agent skips straight to the work. It knows that `tap_element label="Home" element_type="radioButton"` switches to the home tab. It knows that tapping a premium-locked feature triggers an upsell modal, and that `tap_element identifier="_close_button"` gets past it. It knows that the fastest way to reach a specific item is through the search screen, not by scrolling the feed.

The difference is measurable: a "find an item and complete an action" flow that takes an agent 3 minutes of fumbling takes 15 seconds with the knowledge base.

## How It's Built

The knowledge base is built through a **guided tour** — a collaborative session where you and your agent explore the app together.

### Initialize

> "Initialize an app knowledge base for this project"

Your agent runs `init_app_knowledge`, which creates the directory structure and templates. Then it reads the guide resource (`quern://app-knowledge-guide`) and begins the tour.

### The Tour

Your agent drives the simulator while you watch and provide context. The process is iterative:

1. **Agent visits a screen**, captures the elements, and writes a document
2. **Agent shares a summary** — "Here's what I found on this screen, anything I missed?"
3. **You fill in the gaps** — domain terminology, hidden states, known quirks, deep links
4. **Agent updates the document** and moves to the next screen

The tour typically starts with global navigation (tab bar, sidebar), then works outward through each section of the app. As the agent documents screens, it creates **stubs** for screens it discovers but hasn't visited yet — tracking the backlog as it grows.

### What You Contribute

The agent can read the accessibility tree and figure out what's on screen. What it can't infer:

- **Domain meaning** — "That ID is searchable, prices are in cents not dollars, status codes map to these labels"
- **Hidden states** — "New accounts with no activity see a guided tutorial instead of the normal home screen"
- **Interceptors** — "The first time you use each feature category, a coaching tooltip appears"
- **Shortcuts** — "You can search by ID to jump directly to a detail screen without navigating the feed"
- **Suppression** — "That coaching tip can be pre-suppressed via a plist flag"

These pieces of domain knowledge are the most valuable entries in the knowledge base, and they can only come from you.

## What Gets Documented

### Screens

Each screen file captures:

- **Identification** — the unique elements that tell the agent which screen it's on
- **Key elements** — buttons, fields, labels with their accessibility identifiers and labels
- **Navigation edges** — where this screen leads to, and how to get there
- **States** — empty, populated, error, premium-locked, etc.
- **Overlay panels** — floating UI like bottom sheets or summary cards that aren't full screens

Screen files include actual tool calls that the agent can copy-paste:

```yaml
leads_to:
  - screen: "[[screens/item-detail]]"
    action: 'tap_element label_prefix="Order #" element_type="button"'
```

### Alerts

Alerts are the biggest source of agent confusion — an unexpected modal blocks interaction with the screen underneath. Alert files document:

- What triggers the alert
- Which screens it can appear on
- How to identify it (elements visible)
- How to dismiss it
- Whether it can be suppressed (plist flag, first-use only, etc.)

Common alerts: permission prompts, subscription upsell modals, coaching tips, rate-the-app prompts, error dialogs, success/celebration screens after completing actions.

### Flows

Flows stitch screens into goal-directed sequences — "log in and reach the home screen," "search for an item and complete a purchase." Each step includes an action and a verification, plus a failure modes table documenting every interceptor that might appear and how to recover.

Flows are where the knowledge base proves its value most dramatically. A login flow that accounts for onboarding screens, permission prompts, and environment selection just *works* — every edge case is pre-documented with a recovery path.

### Quirks

Anything non-obvious: misleading accessibility identifiers, shared identifiers on different elements, web view screens that can't be automated through the accessibility tree, simulator-specific behaviors, and workarounds for each.

### States and Environments

App-wide modes that change what the agent sees — authentication state, subscription tier, onboarding progress, server environment. Each documents how to detect the state, how to enter it, and how to exit it — including plist flags that can be set directly.

## Connecting to App State

The knowledge base becomes even more powerful when connected to the app's stored state. Many coaching modals, onboarding flags, and feature toggles are stored in plist files that can be read and written directly.

During the tour, investigate the app's plist files (see [App State Management](app-state.md)):

> "Read the app's preferences plist and look for flags related to onboarding or coaching tips"

When you find them, document the key names in the alert files and in a dedicated quirk document. This enables:

- **Pre-suppressing all modals** before a test run — set every `hasShown*` flag to `true`
- **Triggering specific modals** for testing — delete a single flag and relaunch
- **Saving known-good checkpoints** — `save_app_state` after setting all flags, restore before each test

See [Building an App Knowledge Base](workflow-app-knowledge.md) for a detailed walkthrough of this process.

## Maintaining the Knowledge Base

The knowledge base lives in version control alongside your app code. When screens change, the docs update with them. The maintenance cost is low because:

- **Most updates are incremental.** A new button on an existing screen is one line added to a table.
- **Stubs track the backlog.** You always know what's documented and what isn't.
- **Source changes signal staleness.** When your accessibility identifiers file or navigation router changes, the corresponding screen docs may need updating.

The guided tour isn't a one-time thing — it's a workflow you run when a new screen ships or an existing screen changes significantly. Spend 10 minutes with an agent on the new screen, and it's permanently documented.

## Tips

- **Start with the core flows.** You don't need to document every screen before the knowledge base is useful. Document login, the home screen, and the one feature you're testing — that covers 80% of agent sessions.
- **Document alerts immediately.** When something unexpected pops up during the tour, stop and write the alert doc before moving on. Alerts are the highest-value entries because they're the most common source of agent failure.
- **Use `label_prefix` for dynamic labels.** Many elements have labels that include dynamic content (data previews, counts, dates). Document the stable prefix and use `label_prefix` matching in tool calls.
- **Investigate the plist early.** The connection between the knowledge base and the app's stored state is where the real power lies. Don't save this for the end.
- **Save state checkpoints as you go.** When you've dismissed all modals and have a clean, logged-in state — save it. Future sessions start there instantly.