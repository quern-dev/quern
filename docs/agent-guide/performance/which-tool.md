# Which tool to reach for

*`quern://guide/performance/which-tool` -- part of `quern://guide/performance` in the Quern agent guide. Start from `quern://guide`.*

| Question | Tool | Where it works |
|---|---|---|
| How hard is the app working right now? | `get_performance` | iOS simulators, physical iPhones, Android |
| What did each step of a run cost? | `start_recording(gauges=...)`, then `get_trace` | iOS simulators, physical iPhones, Android |
| Why did it freeze or stutter? | `start_profile`, drive the app, `stop_profile` | iOS simulators, physical iPhones, Android |
| Where is the main thread stuck right now? | `get_thread_stacks` | iOS simulators and physical iPhones; not Android (use `start_profile` with the `hangs` preset) |
| How does it cope with a bad network or a hot phone? | `set_device_condition` | Physical iPhones |

A number that couldn't be read is `null` with the reason, never a `0`, across all of these. Read the reason before reading a gap as "nothing happened".
