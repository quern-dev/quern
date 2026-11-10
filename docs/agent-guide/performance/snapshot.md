# A snapshot: `get_performance`

*`quern://guide/performance/snapshot` -- part of `quern://guide/performance` in the Quern agent guide. Start from `quern://guide`.*

CPU (percent of one core, so a busy multi-threaded app can read above 100), memory and thread count for one running app, over a short sample (0.2–5 s; `sample_seconds` in the answer is the window measured). Memory is what `memory_kind` says: `phys_footprint` on iOS, the number Xcode shows, or `pss` on Android, Android Studio's. On Android it also returns frames from `gfxinfo` -- total, janky, and frame-time percentiles since the app started -- so subtracting the counts of two calls around an interaction shows roughly what it rendered (the frame read runs alongside the CPU sample, so the edges of the window are approximate) -- the percentiles are cumulative too, so compare counts, not percentiles, for one interaction; `janky_method` says which jank count it is. A physical iPhone also returns the whole display's frames per second.

An app that isn't running answers `running: false`; when quern can't tell, the answer is an error instead.
