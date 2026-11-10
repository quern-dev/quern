# Starting from a known state: app checkpoints and plists

*`quern://guide/devices/checkpoints` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

Simulators only. These reach into an app's data container, which a physical device doesn't expose.

- **Checkpoints.** `save_app_state` saves a named copy of an app's data container and the app group containers it is entitled to (it terminates the app first; other apps' groups are never touched), and `restore_app_state` puts it back, so a test starts from "logged in" or "onboarding done" without driving the UI there. `list_app_states` and `delete_app_state` manage them.
- **Logged-in states need the keychain.** Auth tokens live in the simulator keychain, outside every app container, so a checkpoint restores to a logged-out app unless it was saved with `include_keychain: true`. That needs the simulator shut down, to save and to restore, because `securityd` holds the keychain open. The restore's `meta.keychain` says which happened.
- **One flag, not a whole state.** `read_app_plist`, `set_app_plist_value` (or `set_app_plist_values` for several), `delete_app_plist_key`, and `diff_app_plist` against a checkpoint. A key is taken literally: `"com.example.flag"` is one top-level key, not a path. On a booted simulator a preference file is written through `cfprefsd`, so a running app sees the change on its next read.
- **Watching a plist.** `start_plist_watch` logs each added, removed or changed key into the log pipeline (`source: "plist_watcher"`), next to the app's logs and flows. `configure_plist_watch` makes it start with every `start_simulator_logging`.
