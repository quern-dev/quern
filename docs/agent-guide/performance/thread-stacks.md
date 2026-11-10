# Where the main thread is: `get_thread_stacks`

*`quern://guide/performance/thread-stacks` -- part of `quern://guide/performance` in the Quern agent guide. Start from `quern://guide`.*

- Where a hung or slow app's main thread is right now: `get_thread_stacks` (iOS simulators and physical devices, symbolicated; not Android, which needs root)

Call it while the app is hung. On a simulator, macOS `sample` reads it for about a second, and `samples_on_stack` of `samples` tells a stuck thread (one path in every sample) from a busy one. On a physical iPhone it is one stackshot. `symbols` says per image how far symbolication got, and why not: the app's frames need its dSYM, the system's the symbols Xcode copies into DeviceSupport. `all_threads` returns every thread, not only the main one.
