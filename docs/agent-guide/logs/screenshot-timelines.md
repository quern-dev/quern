# A screenshot after every action: screenshot timelines

*`quern://guide/logs/screenshot-timelines` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

`start_screenshot_timeline` captures a screenshot after every UI action quern performs (tap, type, swipe, launch, `open_url`...), each labelled with the action, and `stop_screenshot_timeline` returns the manifest: timestamp, action, screenshot path and HTTP status for every step. `get_screenshot_timeline` reads it without stopping. Use it for a visual record of a run for a person to review; for actions joined to their flows and logs, use `get_trace` or a recording instead.
