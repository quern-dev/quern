# A slow network or a hot phone: device conditions

*`quern://guide/performance/device-conditions` -- part of `quern://guide/performance` in the Quern agent guide. Start from `quern://guide`.*

- How an app copes with a bad network or a hot phone: `set_device_condition` (physical iPhones; slow and lossy network profiles, and thermal or GPU states with `allow_performance`), then `clear_device_condition` -- it changes the whole phone, and stays until cleared, the hold ends, or the server stops (a server that died clears it at its next start); `get_device_condition` says what the phone is in, including one set in Xcode, and its `note` says when a network profile has ended on its own (the phone drops it when quern's connection to it does). `list_devices` shows quern's conditions as `condition`, so check there when a phone is unexpectedly slow

`profile` is one of the identifiers `get_device_condition` lists, such as `SlowNetwork3G` or `SlowNetwork100PctLoss`. Only one condition is active at a time: setting one clears the last first, and `replaced` says what that was. The answer is the condition as the device reports it active, not as asked for. Because a condition slows the whole phone, clear it when you're done rather than leaving it for the next holder. For a slower response from one endpoint only, hold its flows with an intercept instead (`quern://guide/network`).
