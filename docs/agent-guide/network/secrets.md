# Secrets in captured traffic

*`quern://guide/network/secrets` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

Quern redacts an app's credentials in everything it **shows** (#742), so a token does not end up in your transcript. It keeps the real values, and uses them wherever a request is actually sent.

**What is redacted:**
- the headers `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key` and `x-auth-token`, matched case-insensitively;
- token-like query-string parameters: `access_token`, `refresh_token`, `id_token`, `token`, `api_key`, `apikey`, `key`, `sig`, `signature`, `client_secret`, `password`, `secret`.

This covers every place a flow is shown:
- `query_flows`, in full and summary detail;
- `get_flow_detail`, `wait_for_flow`, `stop_capture_session`, `get_flow_summary` and `list_held_flows`;
- the proxy's lines in the log buffer;
- the trace;
- recordings with flows.

**Bodies are not redacted** (#749), and answers say so.

**What a placeholder tells you.** A redacted value reads like `Bearer ‹redacted, 1,184 chars, id 3f2a9c1e04bd›`:
- **The scheme and length stay,** so you can still tell an authenticated request from an unauthenticated one.
- **A cookie keeps its names,** and a `Set-Cookie` its attributes.
- **The id is the same for the same value on this server,** so you can answer "is this the same token as before?" or "did it change after the refresh?" without seeing it. It is a keyed hash, useless on any other server.

Every answer that redacted anything lists the names in `redacted` and carries a `redaction_note`.

**When the value itself is the bug,** ask for it:
- Pass `reveal` with the names you need (`["authorization"]`), or `true` for everything. The answer's `revealed` says what you were shown.
- The trace and log lines have no `reveal`: read the flow with `get_flow_detail`.

**Passing a placeholder back is fine.** If you copy the headers you were shown into `release_flow`'s `modifications`, `replay_flow`'s `modify_headers`, or a mock's response headers, quern sends the real value each placeholder stands for. The answer names them in `restored`.

A placeholder this server never showed is refused with a 400, rather than sent on and failing auth in a way that looks like the app's own bug. That happens when it came from another server, or from long enough ago to have been let go. Pass the real value, or read the flow again with `reveal`.

A mock set from placeholders holds the real values, so `GET /mocks` lists its secret headers redacted too.

**Recordings** are redacted by default. A recording made with `redact: false` (`start_recording`) keeps the real values. Use that only for a recording that stays on this machine.

The manifest's `filters.redact` says what the events hold, fixed when the recording starts:
- with the owner's `"redact": false` (below), a recording is unredacted whatever was asked, and says `false`;
- a recording that started redacted stays redacted, even if that setting changes while it runs.

**The owner's settings,** in `~/.quern/config.json`:

```json
{"proxy": {"redact_headers": ["X-Shazam-Token"], "redact_params": ["session"]}}
```

- `redact_headers` and `redact_params` **add** names to the defaults above. They never replace them, so naming your app's own header cannot stop `authorization` being redacted.
- `"redact": false` turns redaction off on this Mac. Only a literal `false` does: anything else, typos included, keeps it on.
