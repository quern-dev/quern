# Performance: how hard an app works, and why it stalls

*`quern://guide/performance` -- part of the Quern agent guide. Start from `quern://guide`.*

Measuring an app, finding where it hangs, and testing it under a bad network or a hot phone.
## Tools for performance work

- How hard an app is working: `get_performance` (CPU, memory, threads; Android frames; null with a reason when something can't be read)
- Where a hung or slow app's main thread is right now: `get_thread_stacks` (iOS simulators and physical devices, symbolicated; not Android, which needs root)
- How an app copes with a bad network or a hot phone: `set_device_condition` (physical iPhones; slow and lossy network profiles, and thermal or GPU states with `allow_performance`), then `clear_device_condition` -- it changes the whole phone, and stays until cleared, the hold ends, or the server stops (a server that died clears it at its next start); `get_device_condition` says what the phone is in, including one set in Xcode, and its `note` says when a network profile has ended on its own (the phone drops it when quern's connection to it does). `list_devices` shows quern's conditions as `condition`, so check there when a phone is unexpectedly slow
- Why the app froze or stuttered: `start_profile` (iOS: relaunches the app under Instruments, presets hangs, cpu, hitches, launch; Android: perfetto records without relaunching, presets hangs, frames, cpu -- its deep summary needs trace_processor, which quern downloads on first use and says so in the answer), drive the app, then `stop_profile` for hangs, hitches and hot functions -- on iOS with Xcode 27.2, the ten longest hangs say whether the main thread was `blocked` or `busy`, and the longest three give the stack it was in; a blocked hang in a profile without System Call Trace has no stack (`stack_note` says why), and without triage `unavailable.triage` says why; `download_profile` for the trace itself
