# Reproducing Bug Reports

*`quern://guide/logs/bug-reports` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

1. Use `get_screen_summary` to verify your starting state
2. Follow the reported steps using `tap_element`, `type_text`, `swipe`, etc.
3. After each step, call `get_screen_summary` to verify the expected state before continuing — this catches where the reproduction diverges from expectations
4. Check logs and network flows alongside UI state to build the full picture
5. If the bug reproduces, capture a diagnostic bundle: screenshot, logs, network flows, and UI tree

**To let people watch it live**, pass `viewer=true` with `video=true`: the response's `viewer_url` plays the simulator in a browser on another machine on the LAN, and expires 10 minutes after the recording stops. Needs `viewer.enabled` in `~/.quern/config.json`; see docs/api-reference.md, "The LAN viewer".

**To keep the whole run**, call `start_recording(udid=...)` before step 1 and `stop_recording` after. Quern's buffers are bounded and shared, so a long run outlives them; a recording writes the device's actions, full network flows (including requests that never finished) and app logs to disk, and on a simulator `video=true` films the screen too. Read it back as a timeline with `get_trace(recording=<id>)`, where each action carries the flows and logs it caused, or page through it with `get_recording`. To see what each step cost the app, add `gauges=<bundle id or package>`: every action in the trace then carries its CPU, memory and (on Android) frames, as peaks over the samples that overlap it, live as well as afterwards.

**Key insight**: Verify state at each step. The step where expected and actual diverge is where the bug lives.
