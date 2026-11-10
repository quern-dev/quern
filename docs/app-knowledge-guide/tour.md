# The Guided Tour

*`quern://app-knowledge-guide/tour` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

This is a one-time investment in complete app coverage — not a quick overview. Expect the process to take one or more sessions, especially for apps with many screens. Don't optimize for speed; optimize for thoroughness. Document every screen you can reach, even trivial settings sub-pages, because the goal is a complete map that future agents can rely on without gaps. If you run low on context, stop at a natural boundary and pick up in the next session — partial coverage that's thorough is better than rushed coverage that's shallow.

The guided tour is a collaborative process with the user. You explore the app together, and you document what you find.

## How It Works

1. **You drive the device.** Use quern tools to navigate the app — `get_screen_summary`, `tap_element`, `swipe`, etc.
2. **You document what you see.** Observe the screen, capture elements, edges, and states. Write the document based on what you can determine yourself.
3. **You present a summary and invite corrections.** After writing the doc, share a brief summary with the user: "Here's what I documented for this screen — anything I got wrong, missed, or that you'd add?" The user's context comes in bursts, not per-question — let them correct and add rather than answering a checklist.
4. **You incorporate their input.** Update the doc with domain knowledge, quirks, and context the user provides.

Avoid asking a long list of questions at every screen. The user knows the app — present what you found and let them fill gaps naturally. Save specific questions for things you genuinely can't determine from the UI (deep links, suppression flags, internal terminology).

## Tour Order

Start from the app's entry point and work outward:

1. **Launch screen** — What does the user see on cold launch? Document it.
2. **Primary navigation** — Tab bar? Sidebar? Document `app.md`'s global navigation table.
3. **States and environments** — Before going deep, ask the user about app-wide states (auth, subscription, onboarding) and available environments (staging, production). Fill in `states.md` and `environments.md`. Also update `.quern/config.json`: add each environment to the `environments` object with its domain and deep link pattern.
4. **Each top-level screen** — Visit each tab/section. Document screens as you go. Create stubs for screens you discover but don't visit yet. As you encounter domain-specific terms (codes, ratings, feature names, acronyms), add them to `glossary.md` — don't wait until the end.
5. **Alerts** — As you encounter any dialog, popup, permission prompt, or coaching overlay, document it in `alerts/` immediately. Also ask: "Are there other popups I should know about on this screen?"
6. **Key flows** — After screens are documented, trace the most important user flows (login, core feature, settings changes) and document them.
7. **Deep links** — Deep links are not discoverable from the UI. Ask the user: "Does this app have deep links or universal links? Can you point me to the routing code or AASA file?" Extracting paths from source code is the fastest way to populate `deep-links/deep_links.json`. If source isn't available, the user can provide paths directly. Then verify each link on the simulator (see `quern://app-knowledge-guide/deep-links`).
8. **Quirks** — As you encounter anything unexpected, document it immediately. Also ask: "Are there any known quirks or device-specific issues with this screen?"
