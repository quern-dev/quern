# Updating config.json During the Tour

*`quern://app-knowledge-guide/config-json` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

As you discover app details during the tour, keep `.quern/config.json` updated alongside the markdown knowledge base files:

- **Plist watch targets.** When you investigate plist files (app containers, app group containers), add watch entries to `plist_watch.watches`. Each entry needs `container` (e.g. `"data"`, `"group.com.example.app"`), `plist_path`, and optionally `ignore_prefixes` for noisy third-party SDK keys.
- **State flags.** When you discover plist flags that control coaching modals, onboarding state, environment switching, or feature toggles, add them to `state_flags`. Group them by category (e.g. `"coaching"`, `"environment"`) with the key name mapped to a human-readable description of what it controls.
- **Saved checkpoints.** When you save an app state checkpoint with `save_app_state`, add it to `saved_checkpoints` with the label as the key and a description of the state (account info, environment, what's been dismissed).

This structured data in config.json complements the prose in the knowledge base markdown files. Config.json is designed for future tool consumption — keeping it accurate means tools will eventually be able to auto-configure plist watches, restore checkpoints, and set state flags without parsing markdown.
