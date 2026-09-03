---
purpose: Domain-specific terms used in the app and this knowledge base.
---

# Glossary

<!-- Add terms as you discover them during the guided tour.
     Focus on terms that have app-specific meaning an agent wouldn't infer. -->

| Term | Meaning | Where It Appears |
|---|---|---|
| | | |

## Knowledge Base Terms

These terms have specific meaning within this knowledge base:

| Term | Meaning |
|---|---|
| screen | A distinct UI state with its own document in `screens/`. Modals and sheets count as separate screens; transient popups and dialogs are alerts instead. |
| flow | An ordered sequence of actions across screens to achieve a goal. Includes setup (state prep), steps (actions + verifications), failure modes, teardown (cleanup), and shortcuts. |
| interceptor | An alert or coaching tip that appears mid-flow and must be dismissed before proceeding. Documented in flow failure modes tables. |
| deep link | A URL (custom scheme or universal link) that jumps directly to a screen, bypassing manual navigation. |
| alert | A transient dialog, popup, permission prompt, or coaching overlay that appears on top of a screen. Documented in `alerts/` when it can appear across multiple screens. |
| state | An app-wide mode (auth, subscription, onboarding, environment) that affects which screens are accessible and how they behave. Defined in `states.md`. |
| environment | A server backend the app connects to (production, staging). Defined in `environments.md`. |
| quirk | A non-obvious behavior that an agent wouldn't predict from the UI alone. |
| stub | A minimal screen file for a screen discovered as a navigation edge but not yet visited. Has `status: stub`. |
| landmarks | Selectors that must all hold for a screen to be recognized — present by default, or absent when an entry sets `absent: true`. `load_landmarks` registers them; `identify_screen` evaluates them against the current UI. |
| precondition | App state that must be true before a screen or flow is reachable. References entries in `states.md`. |
