# Performance: how hard an app works, and why it stalls

*`quern://guide/performance` -- part of the Quern agent guide. Start from `quern://guide`.*

Measuring an app, finding where it hangs, and testing it under a bad network or a hot phone.

## Which tool to reach for

| Question | Tool | Where it works |
|---|---|---|
| How hard is the app working right now? | `get_performance` | iOS simulators, physical iPhones, Android |
| What did each step of a run cost? | `start_recording(gauges=...)`, then `get_trace` | iOS simulators, physical iPhones, Android |
| Why did it freeze or stutter? | `start_profile`, drive the app, `stop_profile` | iOS simulators, physical iPhones, Android |
| Where is the main thread stuck right now? | `get_thread_stacks` | iOS simulators and physical iPhones; not Android (use `start_profile` with the `hangs` preset) |
| How does it cope with a bad network or a hot phone? | `set_device_condition` | Physical iPhones |

A number that couldn't be read is `null` with the reason, never a `0`, across all of these. Read the reason before reading a gap as "nothing happened".

## A snapshot: `get_performance`

CPU (percent of one core, so a busy multi-threaded app can read above 100), memory and thread count for one running app, over a short sample (0.2–5 s; `sample_seconds` in the answer is the window measured). Memory is what `memory_kind` says: `phys_footprint` on iOS, the number Xcode shows, or `pss` on Android, Android Studio's. On Android it also returns frames from `gfxinfo` -- total, janky, and frame-time percentiles since the app started -- so subtracting the counts of two calls around an interaction shows roughly what it rendered (the frame read runs alongside the CPU sample, so the edges of the window are approximate) -- the percentiles are cumulative too, so compare counts, not percentiles, for one interaction; `janky_method` says which jank count it is. A physical iPhone also returns the whole display's frames per second.

An app that isn't running answers `running: false`; when quern can't tell, the answer is an error instead.

## Per-action costs: gauges on a recording

`start_recording(gauges=<bundle id or package>)` samples that app every `gauge_interval_s` (1 s by default) for the whole recording, and `get_trace` gives each action the peaks over the samples that overlap it: CPU peak and mean, memory peak with its first and last reading, peak threads, frames drawn and janky on Android, display FPS on a physical iPhone. It works live as well as afterwards (`get_trace(recording=...)`). A sample is a window, and each summary carries `sample_seconds`: a 100 ms tap inside a one-second sample is given that second's figures. See `quern://guide/logs` for recordings themselves.

## Profiles: hangs, hitches and hot functions

- Why the app froze or stuttered: `start_profile` (iOS: relaunches the app under Instruments, presets hangs, cpu, hitches, launch; Android: perfetto records without relaunching, presets hangs, frames, cpu -- its deep summary needs trace_processor, which quern downloads on first use and says so in the answer), drive the app, then `stop_profile` for hangs, hitches and hot functions -- on iOS with Xcode 27.2, the ten longest hangs say whether the main thread was `blocked`, `busy` or `mixed`, and the longest three give the stack it was in; a blocked hang in a profile without System Call Trace has no stack (`stack_note` says why), and without triage `unavailable.triage` says why; `download_profile` for the trace itself

A profile records for `time_limit_s` (30 s by default, up to 300) and finishes by itself at the limit, so give it long enough for the interaction; read a profile that ended that way with `get_profile`. On iOS the app launches slowly under Instruments: wait for its first screen (`wait_for_element`) before driving it, and relaunch it after the profile stops, since xctrace terminates it. `hitches` is physical devices only; simulators refuse Animation Hitches. `stop_profile` can take a minute or more while the trace is finished; past 4 minutes it answers 504 and the profile keeps finishing, so read it later with `get_profile`. A hang in the first few seconds can be the app's own cold launch. On Android 12 and later, `frame_timeline` says which frames were janky and whose fault it was: the `hangs` and `frames` presets record it, `cpu` doesn't, and reading it needs trace_processor. `unavailable.frame_timeline` says why when it's missing. An Android trace can also stop at perfetto's file limit before `time_limit_s` (`profiles.android_max_trace_mb` raises it): check `trace_data_loss` and `trace_note`, and read their absence as no loss detected, not as proof the trace is whole. Kept profiles share `profiles.max_mb`, and the oldest finished ones make room.

## Where the main thread is: `get_thread_stacks`

- Where a hung or slow app's main thread is right now: `get_thread_stacks` (iOS simulators and physical devices, symbolicated; not Android, which needs root)

Call it while the app is hung. On a simulator, macOS `sample` reads it for about a second, and `samples_on_stack` of `samples` tells a stuck thread (one path in every sample) from a busy one. On a physical iPhone it is one stackshot. `symbols` says per image how far symbolication got, and why not: the app's frames need its dSYM, the system's the symbols Xcode copies into DeviceSupport. `all_threads` returns every thread, not only the main one.

## A slow network or a hot phone: device conditions

- How an app copes with a bad network or a hot phone: `set_device_condition` (physical iPhones; slow and lossy network profiles, and thermal or GPU states with `allow_performance`), then `clear_device_condition` -- it changes the whole phone, and stays until cleared, the hold ends, or the server stops (a server that died clears it at its next start); `get_device_condition` says what the phone is in, including one set in Xcode, and its `note` says when a network profile has ended on its own (the phone drops it when quern's connection to it does). `list_devices` shows quern's conditions as `condition`, so check there when a phone is unexpectedly slow

`profile` is one of the identifiers `get_device_condition` lists, such as `SlowNetwork3G` or `SlowNetwork100PctLoss`. Only one condition is active at a time: setting one clears the last first, and `replaced` says what that was. The answer is the condition as the device reports it active, not as asked for. Because a condition slows the whole phone, clear it when you're done rather than leaving it for the next holder. For a slower response from one endpoint only, hold its flows with an intercept instead (`quern://guide/network`).
