# Documenting Alerts

*`quern://app-knowledge-guide/alerts` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

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
