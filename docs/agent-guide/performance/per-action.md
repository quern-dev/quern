# Per-action costs: gauges on a recording

*`quern://guide/performance/per-action` -- part of `quern://guide/performance` in the Quern agent guide. Start from `quern://guide`.*

`start_recording(gauges=<bundle id or package>)` samples that app every `gauge_interval_s` (1 s by default) for the whole recording, and `get_trace` gives each action the peaks over the samples that overlap it: CPU peak and mean, memory peak with its first and last reading, peak threads, frames drawn and janky on Android, display FPS on a physical iPhone. It works live as well as afterwards (`get_trace(recording=...)`). A sample is a window, and each summary carries `sample_seconds`: a 100 ms tap inside a one-second sample is given that second's figures. See `quern://guide/logs` for recordings themselves.
