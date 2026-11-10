# Logs, crashes and recordings

*`quern://guide/logs` -- part of the Quern agent guide. Start from `quern://guide`.*

Reading app logs, investigating crashes, and recording a run to read back afterwards.

This topic is split into pages, one per subject. Read the one you need:

- `quern://guide/logs/visibility` -- Can quern see this app's logs?: whether an app's logs reach quern: print() vs os_log/Logger, and the empty-answer note
- `quern://guide/logs/reading` -- Reading logs: which log tool for which question: tail_logs, get_log_summary, query_logs, get_errors
- `quern://guide/logs/mistakes` -- Mistakes to avoid: tail_logs vs query_logs, and what each log source name means
- `quern://guide/logs/screenshot-timelines` -- A screenshot after every action: screenshot timelines: screenshot timelines: a screenshot per action
- `quern://guide/logs/crashes` -- Debugging Crashes: get_latest_crash, how crashes are found, crash hooks
- `quern://guide/logs/bug-reports` -- Reproducing Bug Reports: recording a run (start_recording), live viewers, keeping the whole run
- `quern://guide/logs/trace` -- Which request did an action cause: `get_trace`: get_trace: the requests and log lines one action caused, and its caveats
