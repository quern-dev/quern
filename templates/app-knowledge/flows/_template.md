---
flow: ""
goal: ""
preconditions: []
tags: []
---

# Flow Name

## Setup

<!-- State and environment prep before starting the flow.
     Prefer: restore_app_state > set_app_plist_values > manual navigation.

     Cover:
     - State restoration (checkpoint to restore, or manual steps to reach starting state)
     - Plist flags to set (e.g., suppress coaching tips with set_app_plist_values)
     - Permissions to grant (grant_permission calls)
     - Plist watcher to start (if observing state changes during the flow)

     Each precondition should be verifiable — how does the agent confirm
     it's in the right state before proceeding to Step 1? -->

## Steps

<!-- Number each step. Include:
     - Which screen the agent should be on
     - The quern tool call to execute
     - A verification step (wait_for_element or get_screen_summary check)
     - Any interceptors (alerts, tips) that may appear and how to dismiss
-->

<!--
1. **Start at** [[screens/...]].
   - If not there: describe how to get there or which deep link to use.

2. **Do the thing.**
   ```
   tap_element label="..." element_type="..."
   ```
   - Verify: wait_for_element label="..." element_type="..."
   - Interceptor: [[alerts/...]] may appear — dismiss with ...
-->

## Failure Modes

| Symptom | Likely Cause | Recovery |
|---|---|---|
| | | |

## Teardown

<!-- Cleanup after the flow completes. Flows that create persistent
     side effects (test data, plist flags, lists, logs) should document
     how to undo them.

     Options:
     - restore_app_state to a pre-flow checkpoint
     - Delete created data (lists, drafts, logs)
     - Reset plist flags with set_app_plist_values
     - Or note: "No cleanup needed" / "Restore checkpoint to reset" -->

## Shortcuts

<!-- Deep links, state restoration, or alternative paths that skip steps.
     Note which steps each shortcut replaces. -->
