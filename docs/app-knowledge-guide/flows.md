# Writing Flow Documents

*`quern://app-knowledge-guide/flows` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Flows connect screens into goal-directed sequences. Write them after the relevant screens are documented.

- **Setup section first.** Before the steps, document how to reach the starting state: checkpoint to restore, plist flags to set (use `set_app_plist_values` to suppress tips/coaching in bulk), permissions to grant, plist watcher to start. Each precondition should be verifiable — the agent must be able to confirm it's ready before step 1.
- **Preconditions must be actionable.** "Logged in" is not enough. Specify how to verify ("tab bar visible") and how to get there if not ("restore checkpoint `logged_in` or run [[flows/login]]"). The agent will hit "no tab bar because I'm in a navigation stack" if you don't.
- **Each step = an action + a verification.** The agent should always confirm it arrived where expected before proceeding. Note any interceptors (alerts, coaching tips) that may appear at each step and how to dismiss them.
- **Reference screens by link** — `[[screens/login]]`, not a description.
- **Include failure modes** with recovery steps — these prove their value immediately when interceptors appear.
- **Teardown section.** Flows that create persistent side effects (test data, lists, plist flags, logs) should document how to clean up. Options: restore a checkpoint, delete created data, reset flags, or note "no cleanup needed."
- **Shortcuts section** — deep links, state restoration, or alternative paths that skip steps. Note which steps each shortcut replaces.
