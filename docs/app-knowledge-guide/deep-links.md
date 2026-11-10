# Documenting Deep Links

*`quern://app-knowledge-guide/deep-links` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

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
- **Pass `bundle_id`, and read `opened_in_app`.** `open_url` takes the route a tapped link takes, so a path the domain does not claim opens in the browser, and the open itself still succeeds. With `bundle_id`, quern reports whether the app ended up in front, and a `warning` names where the link went instead -- including Android's app chooser (two activities claim the path), and on Android `crashed` says whether the app crashed on it. A `verify` that times out is easier to read once you know which of those happened.
- **Staging links on Android need `direct=true`.** Debug and staging builds are rarely verified App Links, so the system route opens them in the browser. `direct=true` delivers to the package, as Espresso does. It skips verification, so it cannot find a link a user's tap would not reach, and it can show a chooser a tap would not: test production links without it.
- **Pending deep links:** When a universal link is opened while logged out, the app may hold it as "pending" and execute it after login — landing on the deep link target, not the default home screen. Test setup must account for this.
- **Coaching modals:** Deep links to screens with first-visit coaching need those modals suppressed via plist. The `QUERN_AUTOMATION` env var prevents the app from wiping coaching flags on launch.
- **Per-account behavior:** The same deep link can behave differently on Basic vs Premium. Capture this in `premium_gated` and `basic_shows_upsell` fields.
