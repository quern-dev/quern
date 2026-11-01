# API Reference

Every MCP tool and the HTTP endpoint behind it, plus the endpoints that have no tool.
This is the authoritative list — the README links here rather than duplicating it, and
agents can read it over MCP as `quern://api-reference`.

## Authentication

All endpoints require `Authorization: Bearer <key>` except these public paths: `/`,
`/health`, `/api/v1/health`, `/tools`, `/docs`, `/redoc`, `/openapi.json`,
`/api/v1/proxy/cert`, and the two pairing steps `/api/v1/remote/pair/hello` and
`/api/v1/remote/pair/finish` -- which answer only on the remote listener (see
[Remote access](#remote-access)).

`/api/v1/proxy/cert` is deliberately unauthenticated — devices and simulators fetch the
mitmproxy CA certificate from it during setup, before they hold a key. It serves only the
public CA certificate; no traffic, logs, or device state are reachable without a key.

The key lives at `~/.quern/api-key`; the server's URL and port are in `~/.quern/state.json`.

## Identifying the caller

Send `X-Quern-Session: <name>` to say who is asking: letters, digits and `. _ : @ -`, up to 64 characters, starting with a letter or digit (#254). Every action records it as `session`, and `GET /api/v1/trace?session=<name>` returns that caller's actions. The MCP server, `quern record` and the `quern-api` skill's examples send one automatically. Under Claude Code it is `claude-<CLAUDE_CODE_SESSION_ID>`; `QUERN_SESSION` overrides it.

It is attribution, not access control: nothing is refused without it. A request that names no valid session gets an `X-Quern-Session-Warning` response header (exposed to browsers through CORS). Its value starts with a reason, `missing` or `malformed`, followed by `; ` and how to fix it. Public paths never warn. A 500 from an unhandled error doesn't carry the header either.

The server log notes unattributed callers that passed authentication: once every five minutes per reason and User-Agent, and at most 20 lines per five minutes in all. A request without the API key gets the header but isn't logged as an unattributed caller.

## Sessions and reservations

When several agents or CI jobs share one server, an agent can **reserve** devices so nobody else changes them (#254). A reservation stands on a **held session**: a client that keeps the session's WebSocket open.

- **The MCP does this for you.** It opens a session under its `X-Quern-Session` id when it starts and holds the socket while it runs. Its tools are `reserve_devices`, `release_devices` and `session_status`.
- **CI uses `quern session run`.** For example, `quern session run --reserve 2 --device-type simulator -- xcodebuild test …` holds a session for the command. It exports `QUERN_SESSION`, `QUERN_SESSION_TOKEN` and `QUERN_DEVICES` to it, and releases the devices when it exits.
  - The session gets its own name (`<caller>.run-<hex>`).
  - A dropped connection is reconnected within the grace period.
  - When the command finishes, the hold is confirmed with the server. If it was lost along the way (a server restart, say), a successful command exits **4**, not 0.
  - `quern session list` and `quern session release <id>` complete the set.
- **Over HTTP:**
  - `POST /api/v1/sessions` (`{name?}`) returns `{id, resume_token, grace_seconds, live_path}`. The token is shown once and only its hash is kept.
  - Then open the WebSocket at `live_path` with the API key, `X-Quern-Session` and `X-Quern-Resume-Token: <token>`. It answers `{"event": "attached", …}`.
  - API calls stay ordinary requests carrying `X-Quern-Session`.

**Lifetime:**

- **A clean close releases at once:** close code 1000, or `DELETE /api/v1/sessions/{id}`.
- **A dropped connection enters a grace period:** a lost network, a killed process, or missed pings (the server pings every 10 s and gives up after 20 s more). The default is 180 s, set with `{"sessions": {"grace_seconds": N}}` in `~/.quern/config.json` and clamped to 120–300.
- **Reconnecting** with the resume token inside the grace period keeps the reservations. On the server's own machine, opening the same name again also takes the session back, which is how an agent whose MCP restarted keeps its devices.
- **Sessions don't survive a server restart.** A reconnect is closed with code 4404 and must reserve again.
- **Other close codes:** 4403 means a wrong resume token, and 4401 a missing API key. 4000 means a newer connection presented the same token and took the session over: one connection holds a session at a time, the newest winning. Releasing a session (`DELETE /api/v1/sessions/{id}`) closes its socket with 4404, so its client knows. When someone else frees some of a session's devices, its socket gets `{"event": "released", "udids": [...], "by": ...}`.
- **A session without a socket can't reserve.** A plain `curl` with `X-Quern-Session` still gets attribution and its own active device, but a reservation request answers 409.

**Reserving:** `POST /api/v1/sessions/{id}/reservations`, sent as the session itself (`X-Quern-Session: {id}`), with exactly one of:

- `udids`;
- `count` plus the pool's criteria (`device_type`, `os_version`, `name`, `device_family`, `auto_boot`). Free devices are picked, booted first, and more are booted to make up the count;
- `group`, a named set in `~/.quern/config.json`, optionally with `count`. For example `{"device_groups": {"small": [{"name": "iPhone SE*"}, "UDID"]}}`: entries are udids or criteria.

It's all or nothing: **409** names the holder of any device another session has. `on_release: "shutdown_booted"` shuts down, at release, the simulators the reservation booted. Reserving exactly one device makes it the session's active device. Release some or all with `DELETE /api/v1/sessions/{id}/reservations[?udid=]`.

**What a reservation stops:**

- **Changes by anyone else are refused with 423:** another session, or a caller with no session at all. The body is `{message, udid, held_by, state, since, yours}`.
- **What counts as a change:** any request that isn't a GET, apart from a few POSTs that only watch a device (UI waits, landmark identify/validate, flow waits, previews, screenshot timelines, log capture).
- **Reads stay open to everyone:** screenshots, the UI tree, logs.
- **Every route is covered:** a device a request names (`udid`, `udids`, `device_id`, `simulator_udid`, `device_serial` or `serial`, in the query string or any body that parses as JSON) is checked before the handler runs, and the device a request doesn't name (the active device, the pool's pick) is checked when it's resolved. Routes that reach a device another way check it themselves: stopping a recording, capture, timeline or every preview; changing a mock rule scoped to a simulator; cert install; boot by name; WDA's automatic restarts; build-and-install, re-checked after the build. The pool never picks a device another session holds.
- **Two known limits:** releasing an intercepted flow isn't tied to a reservation, because held flows carry no device. And a USB iPhone's preview is filed by its name, not its udid, so stopping every preview can't tell it's held.

**Seeing who holds what:**

- `list_devices` gives each device a `reservation` (`null`, or `{held_by, state, since, yours, this_key}`): `yours` when this session holds it, `this_key` when any session of the caller's key does.
- `GET /api/v1/sessions` lists sessions with their state (`live`, or `grace` with `grace_remaining_s`) and devices.
- The server's owner can release any session.

## Client keys

The owner's API key (`~/.quern/api-key`) reaches everything. Anyone else -- another person's agent, a CI job, a second agent you want kept in its lane -- gets a **client key** (#254):

```text
quern clients add ci-runner     # prints the key once; quern keeps only its hash
quern clients list              # label, created, last used, revoked
quern clients revoke ci-runner  # its sessions end and its devices are freed within a second
```

Give it to the client as `QUERN_CLIENT_KEY` (not `QUERN_API_KEY`, which `quern env` sets to the owner's key), which the quern MCP, `quern session run` and `quern record` read in place of the owner's key. The key decides who is calling, not the network address: a client key on the owner's own Mac is still a client.

**What a client key may do:**

- **Watch everything a shared server shows:** devices, screenshots, UI trees, logs, flows, crashes, recordings, the trace.
- **Hold sessions and reserve devices**, as above.
- **Change only devices its own session holds.** A free device is refused with **403** ("reserve it first"), not only one someone else holds (423). A change that names no device goes to the session's active device, which must be one it holds; a session with nothing reserved is refused before the route runs.
- **Nothing Mac-wide**, which is **403 owner-only**: starting or stopping the proxy, the system proxy, local capture, intercepts and bypasses, mock rules and capture sessions that don't name a simulator (by `simulator_udid`, not just any device field), host logs (oslog), log filters and log presets, updates, server-wide landmark sets (a client loads an uploaded knowledge base for its own session only, #560), the device pool's resolve/ensure (reserve instead), **builds** (`build_and_install` runs a project's scripts, so code on the Mac), app-state checkpoints (one store every caller shares), and anything that reads or writes a path on the Mac (`parse-file`, a recording's `output_dir`, recordings and traces by directory, `validate_landmarks`' `source`).

- **Revoking a key** ends its sessions and frees their devices within a second, closes its session sockets, and ends its open streams -- video at the next frame, logs and flows at their next entry or 15-second heartbeat; the next request is refused with 401.
- **Every request must carry `X-Quern-Session`** (400 otherwise). The server puts a client's session ids in its own namespace, `<label>:<id>`, so one client can never name, release or take over another's session or active device. A client's sessions appear under those names in `GET /api/v1/sessions` and in 423 bodies.

**What a client key is not:** a sandbox. It keeps cooperating users from changing each other's devices and the Mac's shared state by mistake. It does not make a key safe to give someone you would not let use the Mac: an app a client installs on its own simulator runs as the Mac's user, and the simulator sandbox is not a security boundary.

Every route is classified in `server/scopes.py`, and a route with no classification is owner-only; a test fails until each new route is given one.

**Blind sharing.** `{"sessions": {"visibility": "blind"}}` in `~/.quern/config.json` (read at start-up; the default is `"shared"`) keeps client keys out of each other's work, for a server shared between people or teams:

- **Devices:** a client sees the devices nobody holds and the ones its own key holds. A device another key holds is never named as held: a read answers 404, as for a device that does not exist; a change answers the same 403 ("not reserved by this session") as a change to a free device; reserving it answers "no such device". Never 423 naming the holder.
- **Data:** logs, flows, crashes, the trace, recordings, mock rules, proxy and certificate status, previews and the screenshot timeline show only what is attributable to the client's own devices, from the moment each was reserved -- so whoever reserves a device next does not read the previous holder's entries still in the buffers. What is tied to no device -- the Mac's own logs, flows quern cannot place, crash reports naming no device -- is not shown: could not tell, so show less.
- **Sessions:** `GET /api/v1/sessions` lists only the key's own.
- **The owner's set-up** -- knowledge bases, bypassed hosts, log filters, intercepted flows, app-state checkpoints, build progress, the Mac's tool layout -- answers 403.
- **The owner sees everything**, as on a shared server. The mode is read at start-up; `quern doctor` reports the mode the running server took, and says when config.json has changed since and a restart is due. A value other than "shared" or "blind" runs shared and says so.
- **Releasing a device** a client held keeps it unavailable to other keys for the moment it takes to end that client's mock rules and recordings on it (normally under a second); a reservation in that window answers 409 "being released".
- **Not shown to a blind client either:** totals that count everyone's data (buffer statistics, the proxy's flow store figures). The `truncated` flag on log summaries still reflects evictions across the whole buffer, so it may say "may be low" about entries the client could not see anyway.

It keeps cooperating users' work apart; it is not a sandbox (see above).

**The API listens on 127.0.0.1.** `quern start --host 0.0.0.0` still exposes it, with a warning from `start`, `status` and `doctor`: every key, the owner's included, crosses the network unencrypted. To reach quern from another machine, use the TLS remote listener instead (see [Remote access](#remote-access)). The proxy (9101) is unaffected and stays reachable from devices on the LAN; a phone fetches the CA from `http://mitm.it/cert/cer` through it (see `proxy_setup_guide`).

## Remote access

To drive quern from another machine -- a laptop reaching a CI Mac, a team sharing one -- turn on the **remote listener** (#254). It serves the same API over TLS, to client keys only, and is off by default:

```json
{"remote": {"enabled": true, "bind": "0.0.0.0:9130", "hosts": ["ci.lan"]}}
```

`hosts` are extra names it answers to besides this Mac's LAN address (a request naming anything else gets 421). The main API stays on 127.0.0.1. `client_viewer_links` (default on) lets a client key issue viewer links for its own recordings; see [The LAN viewer](#the-lan-viewer).

**Pairing** gives the other machine a client key and the listener's certificate fingerprint in one step:

```text
# on the server's Mac
quern pair laptop
#   Pairing code for laptop (one use, 5 minutes):  ABCD-EFGH-JKMN-PQRS
#   On the client machine:  quern connect https://192.168.1.20:9130 --code ABCD-EFGH-JKMN-PQRS

# on the other machine
quern connect https://192.168.1.20:9130 --code ABCD-EFGH-JKMN-PQRS --name office
quern mcp-install --remote office      # or QUERN_REMOTE=office for `session run` / `record`
```

- **No CA, no domain.** Quern makes its own certificate the first time the listener starts, and clients pin its SHA-256 fingerprint. Every connection -- each request and the held-session socket, from the MCP and from `quern session run` and `quern record` -- checks it before anything is sent, and refuses a mismatch naming both fingerprints. `quern remote rotate-cert` replaces it; every client then pairs again.
- **The code never crosses the network.** The client connects without trusting the certificate, and the two sides exchange HMAC proofs keyed by the code and bound to the certificate the client actually sees, so a machine in the middle presenting its own certificate can neither pass the server's proof nor turn the client's into one the server accepts. A code lasts five minutes, works once, and is void after five failed attempts. Pairing answers only on the remote listener.
- **The owner's key is refused** on the remote listener, with 403: the key that reaches everything stays on the server's Mac. Pair even the owner's own laptop as a client.
- **What a remote client may do** is what any client key may do (above): blind sharing, scopes and revocation apply unchanged.
- **Remotes on the client** are kept in `~/.quern/remotes.json` (0600): `quern remote list`, `quern remote forget NAME`. `quern remote list` asks each remote now, with its own key and pinned certificate, in parallel -- each request may take 4 s, and a remote with no complete answer after 10 s is listed as not answering: whether it answers (or why not: unreachable, timed out, certificate changed, key refused), its version and whether it differs from this Mac's, and its devices with which are free and which this key holds. A remote that could not be asked is never shown as having no devices, and the command exits 1 when any could not be asked. `--devices` lists each device under its remote -- name, model, family, OS version, type, state, and free, yours or who holds it -- free and booted first. `--with-local` lists this Mac's own server first; its not running is said, and is not a failure. `--json` gives the same for scripts, and `--no-check` lists the records without asking (with `--json`, the same `{"remotes": [...]}` shape, records only). A remote can't be named `local`, which is this Mac's own entry. The `list_remotes` MCP tool runs it with `--with-local` on the MCP's machine and marks the server the MCP talks to `"current": true`, so an agent can compare every machine's devices and knows which it can reserve now; `use_remote` switches the MCP to another of them. A key issued by hand with `quern clients add` can be recorded with `quern connect URL --key KEY --fingerprint SHA256:...`.
- **`QUERN_REMOTE` that cannot be read fails**, rather than falling back to this machine's own server: an agent's work must not land on a machine nobody named. `ensure_server` checks a remote's health and never starts a server locally for it.
- **`/health` carries `host_id`**, a random id made once per installation, so a client can tell whether it shares the server's disk -- which decides what a path it sends means.

**Raw REST, without the MCP** (#561). A reservation lasts as long as its holder's connection, plus a grace period for reconnecting after a drop, so a client that vanishes frees its devices (#520). REST calls alone cannot hold one: reserving answers 409 until the session's live socket is open, and the 409 says where it is. The MCP and `quern session run` hold it for you. For anything else, `quern session hold NAME` does:

```text
quern session hold ci-job --background        # prints the session id once its socket is live
curl -X POST "$URL/api/v1/sessions/<id>/reservations" \
     -H "Authorization: Bearer $KEY" -H "X-Quern-Session: <id>" \
     -H "Content-Type: application/json" -d '{"count": 1}'
# ... REST calls as X-Quern-Session: <id> ...
quern session release <id>                    # ends the hold and frees its devices
```

It opens session NAME (or, on the server's own Mac, takes over one of yours in its grace period; from another machine only the session's resume token reclaims one), keeps its socket open -- in the foreground until Ctrl-C, or `--background`, where it returns once the server reports the session live and logs to `~/.quern/session-hold-<id>.log` -- and reconnects through blips within the grace period. Releasing the session ends it quietly; a hold lost for any other reason exits 4 -- and so does a release that lands while the socket is reconnecting, which reaches `hold` as the same 4404 as an expired session. A failed start releases the session only if `hold` opened it, not one it took over. It uses the paired remote given by `--remote NAME` or `QUERN_REMOTE`. A session another connection holds is refused. There is no REST-only lease: a reservation renewed by heartbeats would be a second way to hold a device, with its own expiry rules.

**Paths, when the disk is not shared.** A path in a request is read on the server's disk, so from another machine it names nothing, and from a client key on this one it would be the server reading whatever it is told to:

- **Apps are uploaded.** `POST /api/v1/uploads?name=MyApp.ipa` with the file as the raw body -- an `.ipa` or `.apk` as it is, a `.app` bundle as a gzipped tar named `MyApp.app.tar.gz` with the bundle at its top -- answers an `upload_id`, which `install_app` takes in place of `app_path`. The MCP does this for you whenever it uses a client key. `app_path` is owner-only. Uploads are kept per key: one key never sees, installs or deletes another's. They are deleted once installed (a failed install keeps it, to try again), after an hour, or when the key is revoked, and none survive a restart. What one key holds at once is capped at 2048 MB, unpacked, set with `{"uploads": {"max_mb": 2048}}`. Whatever the keys hold together, the server keeps 4096 MB free on the disk holding the uploads (`"min_free_mb"`, 0 for none); an upload that would go below that is refused with 507.
- **Screenshots come inline.** Screenshots the server saved for an answer -- `capture_screenshots`' before and after, `screen_context.screenshot` when an element was not found, the screenshot timeline -- are recorded against the session that caused them and served at `GET /api/v1/screenshots?path=`. When the server is another machine the MCP fetches them and returns the images with the answer.
- **Recordings are downloaded.** A recording's `output_dir` is on the server. `download_recording` copies its files to a directory on the agent's machine, from `GET /api/v1/recordings/{id}/files`; a client key gets its own recordings only.
- **The project's knowledge base is uploaded and loaded for the session** when the server is another machine or the MCP uses a client key (#560). The MCP sends `app.md` and `screens/*.md` as `<app>.knowledge.tar.gz` and calls `load_landmarks` with its `upload_id`; `load_landmarks` with a path does the same from such an MCP. The set belongs to that held session alone: for its app the session sees only its own screens, in place of any server-wide set for that app (replaced, not merged), other apps fall back to the server-wide sets, and no other caller sees it. It ends when the session does, after the reconnect grace period, and is never remembered. Server-wide sets stay the owner's, loaded by path on the server's Mac. A knowledge-base upload may unpack to `uploads.knowledge_max_mb` (5) and `uploads.knowledge_max_files` (2000), holds no links, and files the loader does not read are ignored.
- **Other paths in answers** -- a crash report's `file_path`, a build's `app_path`, dSYMs -- stay as they are, and the MCP adds a line saying they are on the server's disk.

## The LAN viewer

`start_recording` with `video` and `viewer: true` (or `quern record start --video --viewer`) returns `viewer_url`. The link plays the simulator live in a browser on another machine on the LAN, for example for a CI job to post when a UI test starts (#449). It's off unless enabled in `~/.quern/config.json`:

```json
{"viewer": {"enabled": true, "bind": "0.0.0.0:9120", "hosts": ["ci.lan"]}}
```

- **A separate listener**, port 9120 by default, serving only viewer pages and streams: no API route is reachable through it. quern's own port scan never takes the viewer's port. If the port is busy the viewer doesn't move; it reports the reason instead.
- **Each link carries a token** that is view-only and scoped to one recording. It expires 10 minutes after the recording stops and doesn't survive a server restart. Only its hash is kept. Revoke a recording's links with `DELETE /api/v1/recordings/{id}/viewer`, and issue more with `POST` there. Revoking or expiry also ends the streams already open on a link, within about a second.
- **The token is in the URL**, so it ends up in the viewer's browser history and wherever the link is posted, such as a CI log or a chat. Revoke the links when the run they were for is over, rather than waiting for the expiry.
- **Requests must name an expected Host** (the Mac's LAN address as it is now, `localhost`, or a name in `viewer.hosts`). A browser's Origin must match too, and cross-site requests and `Origin: null` are refused. An unknown, revoked or expired token is a plain 404. The page is served with a Content-Security-Policy that allows no inline script.
- **The listener is limited**, since anyone on the LAN can reach it without a token: 64 connections, a connection that sends no complete request within 5 seconds is closed, and 8 streams per recording (32 in all) before it answers 503 "too many viewers". A viewer's keyframe requests are limited to one a second per recording and aren't counted in the recording's keyframes.
- **The page decodes with WebCodecs where the browser has it**, and otherwise with Media Source Extensions through quern's own fMP4 muxer (`server/device/media/web/fmp4.js`). That covers Chrome on a plain-HTTP LAN origin, which hides WebCodecs. `?muxer=quern` or `?muxer=jmuxer` forces an MSE path for comparison; jmuxer is served only when `viewer.jmuxer_path` points at a local copy.
- **Anyone who has the link can watch** until it expires, so treat it like the screen it shows.
- **A client key may issue links for its own recordings** (#559) -- `viewer: true` at the start, or `POST` / `DELETE` on the recording's `viewer` -- since it can already download those recordings in full. Another principal's recording answers 403 (404 to a blind client). The owner can turn this off with `{"remote": {"client_viewer_links": false}}`; it is on when unset or `true`, and off for any other value or a config.json that cannot be read. When it is off a client asking for a link is refused with a 403 saying so; links already issued keep working until they expire, so revoke them if that matters. A client may always revoke its own recording's links. The owner may issue a link for any recording.

## Tools and their endpoints

### Server and updates

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `ensure_server` | GET | `/api/v1/system/update-status` | Most recent persisted update-check result |
| `list_remotes` | — | — | No route: runs `quern remote list --json --with-local` on the MCP's own machine, so this Mac comes first as `local`, where the paired remotes are recorded. Each paired remote is asked now: whether it answers or why not, its version, and its devices with which are free. See "Remote access" |
| `use_remote` | — | — | No route: switches this MCP to another server -- a paired remote by name, or `local` -- so every later tool call goes there, and moves its held session with it. Refused while the session holds devices on the current server, or when it cannot tell, unless `release` is true. When the current server doesn't answer at all, it is asked once: the release and the mock deletes are skipped, and the mocks are named in `carried.still_on_old` (#580). The answer carries `current`, `server_url`, `version`, `released` when devices were released, and the fields below. The target is checked before anything is switched, the switch succeeds only once a session is live there (otherwise the MCP goes back), and switches run one at a time. Notices about the old server are dropped. Only the MCP is switched, not the `quern` CLI. What the agent set up follows it: the mocks it set and still has (`set_mock`, as `update_mock` left them on the server; not those it cleared, nor a client key's mocks the server ended at `release_devices`) are removed from the old server and, with its `add_session_capture` processes, recreated on the simulators of its next reservation on the new one -- once per simulator, or once unscoped where the mock was unscoped and the key may mock every device -- and that reservation's answer carries `carried` with the new rule ids, failures, and anything still waiting, which a later reservation retries on the simulators still missing it. Rules it could not remove from the server it left are named (`carried.still_on_old`); the next switch back to that server tries again, and `left_here_earlier` says which were removed and which are `still_there` -- a mock such a rule would shadow is not set up again on that simulator until the rule is gone. The project's knowledge base is loaded on the new server (`knowledge`), and `proxy` says whether its proxy runs. Machine-wide proxy settings, recordings, logging and device state are not moved |
| `update_quern` | POST | `/api/v1/system/update` | Launch `quern update` in a detached child; returns immediately |
| `set_update_channel` | PUT | `/api/v1/system/channel` | Set the update channel (`stable` or `beta`) |

### Logs

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `tail_logs` | GET | `/api/v1/logs/query` | Query logs with filters and pagination |
| `query_logs` | GET | `/api/v1/logs/query` | Query logs with filters and pagination |
| `get_trace` | GET | `/api/v1/trace` | Quern's actions with the flows and log lines each one caused. Each action carries `session`: the `X-Quern-Session` id of the caller that asked for it, one per agent session, empty for callers that send none. `session=<id>` returns only that caller's actions, live or over a recording (#254). The MCP tool also takes `mine: true`, which filters to the calling agent's own session, and returns `your_session`. With `recording` (an id or a recording's directory) and optional `until`, the same trace built from a recording instead of the live buffers; `recording.holes` lists the spans it says it does not cover, and the `*_truncated` fields are set when one overlaps the window. `proxy_running` is null for a recording. Each flow carries `started_monotonic`, on the clock video frames are stamped with, and `error` when it has no response: in flight live, or with a start and no end in a recording (hung, or cut off by a gap). Over a recording made with video, each action and flow carries `video: {path, offset_s}` (null when no movie of its quern run covers it), and `recording.video` lists the movies, each with an `error` if it was lost |
| `start_recording` | POST | `/api/v1/recordings` | Record one device's actions, flows (full detail) and app logs to `<output_dir>/events.jsonl` and `manifest.json` until stopped, with no time limit (#364). Body: `udid`, `output_dir`, `kinds` (any of `actions`, `flows`, `logs`; default all — `["flows"]` for network calls only), `hosts`, `exclude_hosts`, `include_unattributed` (default false: only work identified as this device's), `requested_by` (who is asking, e.g. `ci-ui-tests`: kept in the recording's summary, manifest and first line -- a label, not ownership). With flows, a simulator under local capture or the system proxy is first asked whether it trusts the CA: it is installed when `auto_install_cert` is set, otherwise **428** unless `allow_passthrough: true` -- under which its HTTPS is passed through under local capture, and fails for the whole run under the system proxy -- and the response's `simulator_tls` and `warnings` say what is in effect. A recording whose simulator refused the proxy's certificate says so in its `warnings`. `video` (simulators only) also records the screen to `video-<n>.mp4`, one movie per quern run, with a keyframe at each action's start and at requests (see `keyframes`); the trace over the recording then gives each action and flow `video: {path, offset_s}` when a movie of its quern run covers it, and `video: null` when none does, and the events file and manifest each finished segment's `start_host_time`. Video that cannot start is a 400, and no recording is left in `output_dir` (the directory itself may have been created) -- including for a simulator that is not booted, or one another recording is already filming. quern-media writes to `video-<n>.log` and its pid is in the events file as soon as it starts, so when quern is killed the next quern finishes a movie still recording, or reads one that finished by itself; a movie whose quern-media died without finishing is reported lost. Lost video makes `complete` false and `video_lost` true. While the recording runs, `video_lost` is null until a finished movie is known lost, as `complete` is null. Selection follows the trace's own rules (`server.trace.owns`). Every line carries `monotonic` on `time.monotonic()`, the clock video frames are stamped with. A `dropped` line marks where the writer fell behind; quern stopping writes `paused` and starting again `resumed` with the gap, so a recording survives a restart. `warnings` names an unknown device or a stopped proxy. Flow bodies are trimmed per recording, every flow's metadata kept: `bodies` (`all`, `errors` -- response bodies only for non-2xx and unanswered requests; a request's own body stays on its start line -- or `none`), `max_body_bytes`, `exclude_content_types` (prefixes, e.g. `image/`); a dropped body says why in `body_omitted` and keeps `body_size`. `hosts` and `exclude_hosts` take a domain and its subdomains, or a glob (`*.s3.*.amazonaws.com`). With video, `keyframes` (`actions`, `requests`; default both) chooses what makes a seek point -- a request only when no keyframe was asked for in the last second, so the requests an action sets off add none, for runs quern does not drive; request keyframes need `flows` in `kinds`, and `[]` asks for none. With `video`, `viewer: true` also returns `viewer_url`, a link that plays the simulator live in a browser on another machine (#449); see "The LAN viewer" below. With the viewer off the recording still starts, `viewer_url` is null and `warnings` says why. |
| `stop_recording` | POST | `/api/v1/recordings/{id}/stop` | Write what is queued, a `stopped` line and the manifest; returns `counts`, `dropped`, `gaps` and `complete` (true only with nothing dropped and no gap) |
| `recording_keyframe` | POST | `/api/v1/recordings/{id}/keyframe` | Ask the recording's movie for a keyframe now, so this moment is a seek point -- for a driver quern does not see, such as a CI step or a test. Body: optional `label`. The moment is written into the recording as a `mark` line (returned with the `actions` kind). `{"requested": true}` when quern-media was asked; **409** when the recording is not filming, **404** for an unknown id |
| `get_recording` | GET | `/api/v1/recordings/events` | A recording's events for any combination of `kinds` (`actions,flows,logs`) and window (`since`, `until`), in written order, a page at a time (`cursor` from `next_cursor`, `limit` up to 2000). `recording` is an id or a recording's directory. `detail=summary` gives one line per event (the MCP tool's default); `flow_id` gives one flow in full. Markers (`dropped`, `paused`, `resumed`, `stopped`) come with every page, and `holes` lists what the recording says it does not hold in the window. A flow the store updated appears twice: read the last |
| `list_recordings` | GET | `/api/v1/recordings` | Recordings this server is making or has made since it started; `requested_by` lists only those started with it |
| `download_recording` | GET | `/api/v1/recordings/{id}/files` | A recording's files (`events.jsonl`, `manifest.json`, `video-N.mp4`, `video-N.log`) with their sizes, and `live` while it is still being written. The owner's for any recording, a client key's for its own only -- another's is 404. The MCP tool copies them all to a directory on its own machine |
| `download_recording` | GET | `/api/v1/recordings/{id}/files/{name}` | One of those files, streamed |
| — | GET | `/api/v1/screenshots` | `?path=`: a screenshot the server saved and named in an answer (`screenshots.before`/`after`, `screen_context.screenshot`, a timeline entry), for the session whose request took it, or the owner; 404 for anything else. A remote MCP fetches these and returns them inline |
| `get_log_summary` | GET | `/api/v1/logs/summary` | LLM-optimized summary with cursor support |
| `get_errors` | GET | `/api/v1/logs/errors` | Errors and crashes only |
| `get_build_result` | GET | `/api/v1/builds/latest` | Most recent build result |
| `parse_build_output` | POST | `/api/v1/builds/parse-file` | Parse a build log file from disk |
| `record_android_build` | POST | `/api/v1/builds/android/record` | Record a Gradle build (`module_path`, absolute; `variant`) so its crashes can be symbolicated: the APK's package and version codes, R8's `mapping.txt` and `pg_map_id` (checked against the APK's own), and the unstripped native libraries by BuildId, copied. quern does not run Gradle (#347), so record right after building; outputs more than an hour old are said to be. A variant with no APK output is a 404 naming the variants built |
| `get_latest_crash` | GET | `/api/v1/crashes/latest` | Recent parsed crash reports; with `udid`, fetched from an iPhone (USB, the last `days`, default 3) or an Android device first |
| `clear_crashes` | DELETE | `/api/v1/crashes` | Delete the crash reports quern stored on the Mac, for one `udid` or all; the device keeps its own |
| `clear_device_crashes` | POST | `/api/v1/crashes/device/clear` | Permanently delete every crash report on an iPhone (USB); Android and simulators are refused with the reason |
| `set_log_filter` | POST | `/api/v1/logs/filter` | Reconfigure capture filters. `quiet_subsystems` (subsystem prefixes) with `quiet_below` (default `error`) keeps those subsystems' entries only at that level and above -- their chatter dropped, their errors kept. With a `process`, running captures restart with it at the source; a restart that fails answers `status: applied_capture_failed` with `restart_errors` |
| `get_log_filter` | GET | `/api/v1/logs/filter` | Current ingestion filter config at all scopes (global, per-source, per-device) |
| `list_log_sources` | GET | `/api/v1/logs/sources` | Active log source adapters, and what each log buffer holds and has evicted |
| `start_simulator_logging` | POST | `/api/v1/device/logging/start` | Start simulator log capture. 409, with simctl's reason, when the stream exits as it starts -- a simulator that is not booted, a filter simctl rejects |
| `stop_simulator_logging` | POST | `/api/v1/device/logging/stop` | Stop simulator log capture |
| `start_device_logging` | POST | `/api/v1/device/logging/device/start` | Start physical device log capture |
| `stop_device_logging` | POST | `/api/v1/device/logging/device/stop` | Stop physical device log capture |
| `start_oslog_streaming` | POST | `/api/v1/logs/oslog/start` | Start streaming the host Mac's unified log |
| `stop_oslog_streaming` | POST | `/api/v1/logs/oslog/stop` | Stop host oslog streaming |

**An empty answer is not always a true negative.** Logs live in fixed-size
buffers that evict their oldest entries: a shared one, and one each for crash
reports and quern's own logs. An unfiltered simulator can turn the shared buffer over in seconds.
`query_logs`, `tail_logs`, `get_errors` and `get_log_summary` therefore return
`truncated` and `complete_after`. `truncated: true` means entries stamped inside
the requested window were evicted before the call, so the result *may* be
missing some. `false` is a guarantee that nothing in the window was lost. For
a tail (`tail_logs`, or `query_logs` with `tail`) it means something narrower:
the entries returned really are the newest N, though older ones may have been
evicted. Entries removed by a filter change are not evictions; the call that
changed the filter reports them as `purged`.
`complete_after` is the time after which nothing has been evicted. The check is
narrowed by the query's `source` and `level`, so shed debug lines do not flag a
search for errors. `get_trace` reports the same thing as
`log_window_truncated` and `action_window_truncated`. `list_log_sources`
returns a `buffers` object with each buffer's capacity, intake, evictions by
source, and the oldest entry it still holds. A source's `entries_captured` is
intake, not retention.

The live streams, `/logs/stream` and `/proxy/flows/stream`, tell a client
that falls behind what it missed. They send a `dropped` event at most once a
second, with the count, the running `total_dropped`, and `missed_from` /
`missed_to`: the span missed since the previous notice, so consecutive
notices do not overlap. Backfill it with `query_logs` on the log stream and
`query_flows` on the flow stream. Only entries matching the stream's own
filter are counted. Every heartbeat also carries `total_dropped`.

Crash reports have a buffer of their own, so a busy source cannot evict them.
On Android, while capture is running, the logcat adapter emits a crash entry for
each Java crash, native crash and ANR as it happens. Find them with `query_logs`
(`source=crash`) or `get_errors`. `get_latest_crash` reads Android crash reports
too; see below.

**Summary cursors follow arrival order.** `get_log_summary` and
`get_flow_summary` return a `cursor`; pass it back as `since_cursor` to get only
what arrived since. The cursor counts arrivals, not timestamps, so the next delta
also includes entries stamped earlier than the last summary: a device clock
running ahead, a crash report written after the crash, a request that started
before the summary and finished after it. A cursor from before a server restart,
or one ahead of anything the server has numbered, comes back with
`cursor_reset: true`, and the summary then covers the requested window. On the
flow summary, a string that is not a cursor at all is refused with 400. Older timestamp cursors are still accepted, and the response always
returns an arrival cursor.

**Where it crashed.** Each crash report carries `app_frame`: where in the app's own
code it happened, with source file and line where the report has them.
- **Which frame.** It comes from an uncaught exception's backtrace when the report has
  one, and otherwise from the crashing thread. A crash reporter's signal handler and
  the app's entry point are skipped.
- **Source lines.** A simulator's Debug build has them, because macOS resolves the
  frames on the Mac. A phone names the function for a Debug build and gives only an
  offset for a stripped one.
- **`reason`** is the report's own explanation: an uncaught exception's reason, or
  Android's abort message or root cause. A Swift `fatalError` writes its message to
  the app's log, not the report, so query the logs for it.
- **`killed_by`** names the process whose signal ended the app, when it was not
  the app itself, such as a kill from a shell or `devicectl`. It is decided by
  pid, because the name is truncated to 32 characters. In that case `app_frame`
  is null, because the frames only say where the app was waiting. A watchdog or
  memory termination is not a kill: it keeps its frames, which are where the app
  hung, and its explanation goes in `reason`.
- **`.crash` text reports** (iOS 14 and older) get neither `killed_by` nor the
  exception backtrace. Their frames are the crashed thread's.

The response is compact by default: `app_frame`, `reason`, `killed_by`, the top
frames, and the app's `bundle_id`, `app_version` and `build_version`.
- **`detail=true`** adds `frames` (the exception's backtrace or the crashing thread,
  each frame with image, offset, symbol and source line) and `images` (the UUID and
  load address of each binary they point into). Symbolicating a frame needs these.
- **`include_raw=true`** adds `raw_text`.

**A device report is symbolicated against the build that crashed.** The phone
names a Debug build's functions but records no file or line, and names nothing in
a stripped build. So each app image is matched by UUID, first to quern's build
records (`build_and_install` to a device keeps dSYMs), then to a dSYM Spotlight
indexed (`mdfind "com_apple_xcode_dsym_uuids == <UUID>"`: Xcode's DerivedData and
archives), and its frames are resolved with `atos`. Every frame but the crashing
thread's top one, and the frame a signal interrupted (just below `_sigtramp`), is a
return address and is looked up one byte before it, as crash tools do: at the
address itself a `fatalError`'s own frame resolves to compiler-generated code. `symbols` says, per image, where the symbols came from
(`source`, `build_id`, `dwarf`), how many frames gained a line
(`frames_resolved` of `frames_total`), and otherwise why not: no match on this Mac
for that UUID, a build whose dSYMs have expired, Spotlight or `atos` unavailable.
Only the crashed app's own frames that lack a line are sent, so a simulator's
report, which macOS usually symbolicates, costs nothing unless one of them does,
and the Mac's own processes are left alone (Android reports are covered below). An image is
settled once `atos` has answered for it, and kept until the server restarts; an
image whose symbols were not found, or could not be read, is looked up again on the
next read (a records scan and one Spotlight query), so a build or an index that
appears later is picked up. `symbolicate=false` does no new work; frames an earlier
read resolved stay resolved.

**Android reports are symbolicated against a recorded Gradle build**
(`record_android_build`). When a record's `mapping.txt` matches the crash, the Java
frames are retraced with `retrace` from the Android SDK command-line tools: the whole
trace, past the 30 frames `frames` shows, in one run, each block after its exception
line, because retrace rewrites a NullPointerException's frames and resolves outlined
frames only with that context. An inlined frame becomes one frame per function,
innermost first; frames retrace writes nothing for (outlines, usually) are left out;
and the app frame is chosen again from the real names, as the innermost cause's first
app frame. `frames_resolved` of `frames_total` counts the frames retrace renamed or
that carry R8's marks. The mapping is chosen by the `r8-map-id` stamp when a frame
carries one, and otherwise by package and version, which a note always says, since
local builds share a version. A version-only match is applied only with proof the
trace is that build's: a frame's own class renamed, a file retrace gives back for a
frame printed without one (R8 stripped it; kotlinc's source-less classes come back
without one), or a `SourceFile` / `r8-map-id-` frame. Nor is it applied when the
newest record of that version is unminified and the trace shows no sign of R8. When no
record matches, `symbols` asks for one only if the trace shows R8's marks: a
`SourceFile` or `r8-map-id-<id>` source file, `Unknown Source` with a line on a
minified class name (`l82`, `Activity$b`), or every one of the app's frames printed
without a source file, where the rules strip them. At record time the APK's own marker
is read: a mapping from another R8 build, or one left beside a D8 (unminified) build,
is not kept. Native frames in the app's own libraries are matched by BuildId to the
record's unstripped copies, each checked against its BuildId before use, and resolved
with the NDK's `llvm-symbolizer` at the pc the tombstone gives; a frame that gains a
line takes the symbolizer's function name with it. A missing tool, record or mapping,
a tool that fails or cannot run, and records that cannot be read are said in `symbols`
and looked up again on the next read, as is a trace with no record yet. A tool's
answer is kept, including one that cannot be matched to what was sent, which asking
again would only repeat.

A frame counts as the app's when:
- **iOS:** its binary is inside the app bundle.
- **Android native:** it was installed with the app.
- **Java:** its class is in the app's package, or in a shorter prefix of it (a
  Debug build's `.debug` suffix, or a module), falling back to excluding platform
  and common-library packages.

**Crash reports on both platforms.** `get_latest_crash` with a `udid` fetches
that device's crashes first. An iPhone is read over USB with `pymobiledevice3`:
the reports dated within the last `days` (default 3), and `pull` says what it
left on the phone (`older_on_device`, `oldest_on_device`, and a `note` pointing at
`days` and `clear_device_crashes`). An Android device or emulator is read from its DropBox,
which needs no root, and yields Java crashes, native crashes and ANRs; each
report's `kind` says which. The response's `pull` says whether the fetch
happened: `pulled`, `skipped` (with the reason, for example an iPhone that is not
on USB) or `failed` (with the error). Only `pulled` means the list reflects the
device (for an iPhone, within the pull's window; `older_on_device` says what lay beyond it). A `failed` Android pull can still add the reports it did read, for
example when one DropBox tag could not be read, or when the device's timezone
could not be read and records without a time of their own were skipped. A
simulator is `skipped`: its crash reports are written on the Mac and read from
`~/Library/Logs/DiagnosticReports` continuously, unless the server was started
with `--no-simulator-crashes`, which the reason then says. With a `udid`, the
list is that device's crashes, plus reports quern cannot place on any device. A
simulator's crash names its simulator by the app's path, or for a system app or
extension (which runs from the runtime volume) by the report's `coalitionName`,
so it is listed under that simulator only. A crash of one of the Mac's own
processes has `mac_process: true` and is listed only without a `udid`.
`raw_text` is left out of the response unless
`include_raw=true`: it runs to about a thousand tokens of JSON per crash. For an
iOS report the whole file is on disk at `file_path`. An Android report has no
file, and `file_path` names its DropBox record.

Crash reports are left on the iPhone (the pull copies, it never deletes), so a
pull does not take them away from Xcode or Finder. Each phone's reports are kept in
`~/.quern/crashes/devices/<device id>/`, so after a restart they are listed
against the right phone again, as an Android device's DropBox history is. A crash from before the server started,
on either platform, is listed but does not become a new log entry or run the
on-crash hook.
The hook runs for every newer crash, including one logcat already reported.

**Clearing.** `clear_crashes` deletes quern's stored copies on the Mac, for one
`udid` or all: only the files quern's own pulls wrote, under
`~/.quern/crashes/devices/`. Nothing else in the crash directory is deleted, and
never `~/Library/Logs/DiagnosticReports`. It does not clear the device: a later
pull lists again whatever the device still holds within its window, without
logging it as a new crash. An unknown `udid` is a 404, and an empty one a 400.
Pulled reports not copied for 30 days are also removed automatically, at start-up
and hourly; set `crash_retention_days` in `~/.quern/config.json` (0 keeps them
forever). The removals are logged in the server log only.

`clear_device_crashes` permanently deletes an iPhone's own crash reports, for
Xcode and Finder too: every `.ips`/`.crash` report at the top of its crash
directory, each by name. DiagnosticLogs (sysdiagnose archives) and other files
are left; `pymobiledevice3 crash clear` would remove them too, and is not used.
It returns how many it removed, how many remain and any that failed. It refuses a
phone matched to USB by name rather than by its hardware UDID (#323), Android (an
unrooted device's DropBox can only be read), and a simulator, whose reports are
files on the Mac. A phone with a long history lists more slowly; that cost has
not been measured.

On Android, `pull.open_dialogs` lists processes showing a crash ("keeps
stopping") dialog right now (`kind: "crash"`), and processes Android is treating
as not responding (`kind: "anr"`). The second starts when Android notices, about
13 seconds before the ANR dialog and its report appear (measured on API 32), and
lasts until the dialog is answered. While a crash dialog is open, Android drops
every further crash of that process, with no report and no log line, so no new
reports does not mean it stopped crashing. Dismiss the dialog or force-stop the
app. `[]` means none; `null` means it was not checked (iOS, or a pull that could
not run at all) or the process listing could not be read. A pull that is `failed`
only because some DropBox tags could not be read still reports it.

### Network proxy

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `query_flows` | GET | `/api/v1/proxy/flows` | Query captured flows |
| `list_pending_requests` | GET | `/api/v1/proxy/flows/pending` | Requests that have started and have no response yet, oldest first, with `age_s` and `started_monotonic` (#364). A flow is stored only when it is answered or fails, so a hung request appears here and nowhere else while it hangs. Filter by `simulator_udid`, `device_serial` or `client_ip`; `evicted` counts in-flight requests pushed out by the bound (2,000 at once). Also included in `get_trace`'s live flows, each with an `error` saying how long it has waited |
| `get_flow_detail` | GET | `/api/v1/proxy/flows/{id}` | Full flow detail |
| `wait_for_flow` | POST | `/api/v1/proxy/flows/wait` | Block until a matching flow appears, or time out |
| `get_flow_summary` | GET | `/api/v1/proxy/flows/summary` | Traffic digest |
| `start_capture_session` | POST | `/api/v1/proxy/capture/start` | Start a capture session to bracket a UI action. With `simulator_udid` naming a simulator under local capture or the system proxy, it first asks that simulator whether it trusts the CA: installs it when `auto_install_cert` is set, otherwise **428** unless `allow_passthrough: true` (its HTTPS then passed through under local capture, failing under the system proxy). `simulator_tls_note` says when its TLS is passed through, or when it has refused the proxy's certificate |
| `stop_capture_session` | POST | `/api/v1/proxy/capture/stop` | Stop the session and return only the flows from that window |
| `proxy_status` | GET | `/api/v1/proxy/status` | Proxy status and config |
| `start_proxy` | POST | `/api/v1/proxy/start` | Start the proxy. With `system_proxy: true` it also configures the macOS system proxy and takes the same certificate check as `configure_system_proxy` — **428** when a booted simulator does not trust the CA. `skip_cert_check` is deprecated, and a response to a request using it carries `deprecations`. Starting the listener alone is never refused |
| `stop_proxy` | POST | `/api/v1/proxy/stop` | Stop the proxy |
| `proxy_setup_guide` | GET | `/api/v1/proxy/setup-guide` | Device setup instructions |
| `verify_proxy_setup` | POST | `/api/v1/proxy/cert/verify` | Verify CA cert installation (defaults to booted simulators) |
| `install_proxy_cert` | POST | `/api/v1/proxy/cert/install` | Install CA certificate on simulators and emulators |
| `record_device_proxy_config` | POST | `/api/v1/proxy/device-proxy-config` | Record a physical device's Wi-Fi proxy config (per SSID); on Android, `apply=true` writes it over adb and `clear=true` unsets it. An applied answer carries `https`: `ca_install`, `ca_trusted` (null when quern cannot see) and a `note` on making HTTPS work there. A proxy applied by a session that holds the device is cleared when that hold ends, reported as a `device_proxy_cleared` action |
| `set_local_capture` | POST | `/api/v1/proxy/local-capture` | Set local capture list — entries are mitmproxy local-mode syntax: a name (case-sensitive substring of the executable path in `source_process`), a PID, or either prefixed `!` to exclude; a list *starting* with an exclusion captures every other process on the Mac, so it returns **400** unless `whole_mac: true` is passed. Sets it, but always keeps a minimum (`MobileSafari`, `com.apple.WebKit.Networking`) so webview and OAuth traffic is not silently lost. `capture_added` and `capture_removed` on the response say what changed. `only: true` captures exactly the list given. The **flag** is not stored, but the **list is** — it is written to `config.json` like any other, and start-up later widens it back to include the minimum, because nothing on disk records that you meant it narrowly. So `only` is a one-shot narrowing of the running server, not a temporary view: it permanently replaces whatever list was configured before. Does **not** refuse over the CA: a booted simulator that does not trust it has its TLS passed through, undecrypted, and `simulator_tls` on the response says which simulators are decrypted and which are not (flow queries filtered to a passed-through simulator carry `simulator_tls_note`). `skip_cert_check` is deprecated: it decrypts simulators the trust check could not vouch for, never one known not to trust the CA, and the response carries `deprecations`. Disabling (empty list) is never refused. The list is a launch argument, so a running proxy restarts to apply it: mocks, bypass patterns and the intercept are re-sent to the new process (#562), while open connections are reset and held flows are released unmodified. The response's `restart` says so -- `connections_reset`, `held_flows_released`, and `restored` (`mock_rules`, `bypass_patterns`, `intercept`), which is null when the proxy did not come back up or could not be sent them; the rules are then kept and sent at the next start. The call returns once the new mitmdump reports it is serving (its `running` event) and has echoed every re-sent rule, about a second, and `confirmed: true` says so; `confirmed: false` means that did not happen within 5 s, so traffic may not yet be captured or mocked. Every start sends the rules the server holds, so `start_proxy` after a crash restores them too. `restart` is null when the proxy was not running |
| `add_session_capture` | POST | `/api/v1/proxy/session-capture` | Capture apps on a simulator the caller holds without changing the local capture list or restarting the proxy (#558). An Android or physical iOS device is refused with **422**, before anything starts: route an Android device with `device-proxy-config` instead. Body: `simulator_udid` (required; a client must hold it, else **403** naming what to reserve; the owner need not), `processes` (names; a client may not use exclusions or PIDs, **400**), `only` (skip the web-view minimum, which is added otherwise), `allow_passthrough`. Names the main proxy already captures are served by it (`main_proxy`); names another session asked for share its session proxy (`shared`); the rest get a new session proxy, a second mitmdump in local mode only (`started`), and `confirmed` says it reported capturing with the rules applied. Every proxy takes the main proxy's mocks, intercepts, bypass patterns and trusted-simulator set, and feeds the same flow store, tagged by simulator. Then the CA gate runs as for a capture session (`simulator_tls`; **428** unless `allow_passthrough`, the names handed back); `warnings` notes that connections an app already had open stay uncaptured until it reconnects. **409** when the proxy is not running. A client key may be using at most 4 session proxies at once, shared ones included (**429** naming them, with `using`), and ask for at most 16 names in a request (**400**); the owner is not limited. Capture lasts as long as the key holds the simulator; the owner's capture on a simulator it doesn't hold ends with the session that asked for it (#579), including a `use_remote` switch away; a session proxy stops once no capture uses it. Not persisted: a server restart ends them, and stops any a crashed server left running. Capture is by process name across simulators, as the main proxy's is: a session proxy captures its names on every simulator, so one shared by two holders keeps capturing on a simulator one of them released, while the other's claim lasts. Who sees those flows is the server's visibility mode (#254), not which proxy carried them: every caller sees every device's in shared mode (the default), and a blind client only those of devices it holds; `proxy_status`'s `session_proxies` follows the same split. A session proxy never captures the Mac's own processes |
| `remove_session_capture` | DELETE | `/api/v1/proxy/session-capture` | Hand back the caller's session capture: all of it, or `simulator_udid`'s, or only `processes`. Answers `stopped` (the names whose session proxies stopped) and the remaining `claims` |
| — | GET | `/api/v1/proxy/session-capture` | The caller's session capture `claims` and the `session_proxies` they use; the owner sees every session's. `proxy_status` carries `session_proxies` too |
| `set_bypass` | POST | `/api/v1/proxy/bypass` | Add domain patterns to the bypass allowlist |
| `clear_bypass` | DELETE | `/api/v1/proxy/bypass` | Remove bypass patterns, or clear all |
| `configure_system_proxy` | POST | `/api/v1/proxy/configure-system` | Auto-configure macOS system proxy. Returns **428** when a booted simulator does not trust the mitmproxy CA. `skip_cert_check` is deprecated, and a response to a request using it carries `deprecations` |
| `unconfigure_system_proxy` | POST | `/api/v1/proxy/unconfigure-system` | Restore original proxy settings |

**Flow answers say when the store has evicted.** The flow store holds 5,000
flows and evicts the oldest-completed at capacity. `query_flows`,
`get_flow_summary`, `wait_for_flow` and `stop_capture_session` return
`truncated` and `complete_after`, with the meaning they have on the log tools.
They are narrowed by `simulator_udid`, `client_ip` or `device_serial` when the
query filters on one. Prefer `device_serial` for Android: an emulator's traffic
arrives from the host's own address because QEMU NATs it, so every emulator on
a machine shares one `client_ip` and filtering on it cannot tell them apart.
The serial is resolved from the process that owns the host socket, so it
distinguishes two emulators where nothing else can. `truncated` covers counts as well as entries: a page can hold the newest
flows and still carry a `total` that is short by what was evicted. To ask only
about recent traffic, pass `since` (for example, just before the action you
triggered); evictions from before it do not flag the answer. `proxy_status`
reports the store's capacity, intake (`added`), evictions and the span it still
holds in `flow_store`. `flows_captured` is only what survived.

### Intercept and mock

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `set_intercept` | POST | `/api/v1/proxy/intercept` | Set intercept pattern |
| `clear_intercept` | DELETE | `/api/v1/proxy/intercept` | Clear intercept |
| `list_held_flows` | GET | `/api/v1/proxy/intercept/held` | List held flows |
| `release_flow` | POST | `/api/v1/proxy/intercept/release` | Release a held flow |
| `replay_flow` | POST | `/api/v1/proxy/replay/{id}` | Replay a captured flow |
| `set_mock` | POST | `/api/v1/proxy/mocks` | Add mock rule. Optional `simulator_udid` mocks only that device's requests -- a simulator's UDID, or an Android emulator's or phone's serial -- failing closed when a request cannot be attributed; a UDID that is not a booted simulator, or an Android device not routed through the proxy, is accepted with a `warning`. A serial that is not an Android device quern can see, or a phone whose network address cannot be found (route it with `device-proxy-config` first), is a 422. The answer's `platform` says which kind of device the scope is (`ios`, `android`, or null when unscoped). |
| `list_mocks` | GET | `/api/v1/proxy/mocks` | List mock rules, each with its `simulator_udid` scope (null = every device), and for a rule scoped to a phone the `client_ip` it is matched by, as when the rule was set |
| `update_mock` | PATCH | `/api/v1/proxy/mocks/{id}` | Update a mock rule's pattern, response or `simulator_udid` (a simulator UDID or Android serial, checked as for `set_mock`). Only fields named change: response fields given (nested `response`, or flat `status_code` / `headers` / `body`) are merged into the stored response, and a flat field named `null` takes the spec's default (#578). `headers`, when given, replaces all headers. `simulator_udid`: omit to keep the scope, a UDID to scope it, null to clear it (owner only). 404 for an unknown id; 400 when nothing is given |
| `clear_mocks` | DELETE | `/api/v1/proxy/mocks/{id}` | Delete a specific mock rule |

### Device

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `list_devices` | GET | `/api/v1/device/list` | List simulators, emulators, and physical devices. Each carries `model`: what the hardware is, as Apple names it ("iPad Pro 11-inch (M4)"), whatever the device is called -- a simulator's from its device type, a physical device's marketing name or product type ("iPhone16,1") -- or "" when unknown. An Android emulator's model is its AVD's hardware profile ("Pixel 7"), a phone's its product model. A `name` criterion in `list_devices`, `reserve_devices` and `resolve_device` matches the model too, so a renamed simulator is still found by what it is. `platform` picks on one platform (see "Choosing a platform"). An Android device also carries `ca_install` -- `automatic` (a rootable build: quern installs its CA), `manual` (not rootable: a user certificate, for apps that trust them), or `unknown` -- read from a shut-down AVD's image without booting it. An Android emulator also carries `avd_name`, its AVD, running or not: a running one is listed by its serial (`emulator-5554`), and a `device_groups` entry `avd:<name>` finds it by this. It is "" when the emulator's console cannot be asked -- one attached over TCP (`adb connect`), or one whose console did not answer -- and an `avd:` group entry cannot find such an emulator. Through the MCP, a list filtered by name, OS, family or type that comes back empty carries `elsewhere`: which paired servers have matching free devices, and the `use_remote` call to get there |
| `boot_device` | POST | `/api/v1/device/boot` | Boot simulator |
| `shutdown_device` | POST | `/api/v1/device/shutdown` | Shutdown simulator |
| `erase_device` | POST | `/api/v1/device/erase` | Erase a simulator or Android emulator, resetting it to factory state (an emulator is relaunched, possibly on a new serial) |
| `install_app` | POST | `/api/v1/device/app/install` | Install an app: `app_path` on the server (owner only), or `upload_id` from `POST /api/v1/uploads` |
| `launch_app` | POST | `/api/v1/device/app/launch` | Launch app. `env` sets environment variables on an iOS simulator or physical iPhone; since they reach only a starting process, a running app is restarted, and the response carries `env_applied` and `restarted` (null when it could not tell whether the app was running). On a physical iPhone, `launch_confirmed` is null with `launch_check_error` when quern could not read whether the app reached the foreground. Android takes no environment: `env_applied` is false, with a `warning`. An env name that is empty or contains `=` or NUL is a 422 |
| `terminate_app` | POST | `/api/v1/device/app/terminate` | Terminate app |
| `uninstall_app` | POST | `/api/v1/device/app/uninstall` | Uninstall app |
| `list_apps` | GET | `/api/v1/device/app/list` | List installed apps |
| `get_performance` | GET | `/api/v1/device/app/performance` | CPU, memory and threads of one app; Android frame stats; iOS-device display FPS |
| `start_profile` | POST | `/api/v1/profiles` | Record an iOS app with Instruments (xctrace) on a simulator or physical device (#245). Body: `bundle_id`, `udid`, `preset` (`hangs` default: Time Profiler plus Hangs; `cpu`; `hitches`: Animation Hitches, physical devices only -- simulators refuse it, so it is a 400 there; `launch`: App Launch), `time_limit_s` (1-300, default 30). **xctrace launches the app** (attaching to a running app was measured broken on a physical iPhone) **and terminates it when the profile stops**; the answer's `notes` says so. A physical device's CoreDevice tunnel is brought up with one `devicectl` call first, since xctrace waits for it rather than asking. One profile per device: a second is 409. Kept profiles share `profiles.max_mb` (config.json, default 2048): the oldest finished are removed to make room -- `notes` names a client's own and counts other keys' -- and when profiles still recording or being downloaded fill it the start is 507 and nothing is removed. Directories no record knows (a server killed mid-start, an unreadable `profiles.json`, which is kept aside) count against it and go first. The owner also gets `disk`. xctrace that never says it is recording (an offline device, an app it cannot launch) is 502 with its own last lines. Needs the device reserved on a shared server. Android is a 400: use `get_performance`. |
| `stop_profile` | POST | `/api/v1/profiles/{id}/stop` | Stop early (SIGINT), wait for xctrace to finalise the trace, read it, and return the profile with its `summary`: `hangs` (`start_s`, `duration_ms`, `kind`, `thread`) with `hangs_count`, `hitches` (`start_s`, `duration_ms`, `process`, `explanation`) with `hitches_count`, `time_profile` (`samples`, `top_functions` -- innermost frames -- and `top_app_functions`, the app's own frame nearest each sample), `launch` for the `launch` preset (`phases`: `start_s`, `duration_ms`, `phase`, `explanation`; and `first_frame_ms`, when Initial Frame Rendering ended -- a physical device records these, a simulator none, which `unavailable` says), plus `template`, `end_reason`, `duration_s`, `process`, `device`. A section the template did not record, or that could not be read, is null with its reason in `unavailable`. Lists are capped at 50. A 504 after 240 s means it is still finishing; read it later with `get_profile`. A stop of an xctrace that has already ended records no reason of its own -- the profile says how it ended -- and a 503 means `ps` could not say whether it was still running. |
| `get_profile` | GET | `/api/v1/profiles` | The kept profiles, oldest first, without summaries. A client key sees its own only |
| `get_profile` | GET | `/api/v1/profiles/{id}` | One profile: `state` (`recording`; `finalizing` from the moment xctrace says it is saving, after which a stop signals nothing; `finished`; `failed` when xctrace never said it saved the trace -- its bundle exists from the start, so a directory proves nothing -- or the trace cannot be read), `stop_reason` (`time_limit`, `stopped`, `session_ended`, `server_restart`, `killed`), `error`, `bytes`, `files`, and `summary` once finished. A profile still recording when the server stopped carries on under xctrace's own time limit and is picked up by the next server; a client key's ends with its session |
| `download_profile` | GET | `/api/v1/profiles/{id}/files/{name}` | `summary.json`, `xctrace.log`, or the trace bundle as `profile.trace.zip` (zipped on request, for a finished profile only: 409 otherwise). The MCP tool copies them to a directory on its own machine |
| `build_and_install` | POST | `/api/v1/device/build-and-install` | Build an app and install it on one or more devices: a Gradle project on Android (see below), or an Xcode scheme on iOS. For Xcode: each successful build is recorded in `build_records` and in `~/.quern/build-records/`: bundle id, version, configuration and each binary's UUID, kept after the next build overwrites DerivedData. A device build also keeps dSYMs of the binaries this build compiled, and those Xcode or a vendor supplied with matching UUIDs, for the newest 10 device builds of each scheme; records are kept 30 days. Each binary's `dwarf` is the file inside its dSYM to pass to `atos -o`: one dSYM can cover several binaries (`MyApp.app.dSYM` holds the app and its `.debug.dylib`), and `atos` given the bundle can read the wrong one and resolve nothing. A dSYM that could not be made is said per binary (`dsym_error`), and never fails the build. Each project, scheme and platform builds into its own DerivedData, `~/.quern/builds/<project>-<hash of its path>/<scheme>/<iphoneos|iphonesimulator>`, so two checkouts of one app never share one; a request for a phone and a simulator builds both at once, each in its own, and a second request for the same project and platform waits for the first to finish installing. A failed package resolution carries its reason (the missing repository, path or version), and a non-zero exit from xcodebuild is a failed build whatever it printed (#442). `skip_plugin_validation=true` builds even when a Swift package plug-in or macro has not been approved in Xcode (`-skipPackagePluginValidation -skipMacroValidation`); it is off by default, and a build refused for that says so in its errors. **Gradle projects (#347):** `project_path` is the build root or a module, `variant` names the build variant (`stagingDebug`), and `module` defaults to `app`. Runs the project's own `./gradlew :<module>:assemble<Variant>` with Gradle's daemon kept, installs the APK fitting each Android target's ABI in parallel, and records the build as `record_android_build` does. The response names the JDK and SDK used (`java`, `android_sdk`), which quern finds without a shell. When the machine rather than the code stops the build, `environment` lists each problem (`kind`, `summary`, `found`, `options`) instead of building. Without `variant`, a 400 lists the module's variants (from Gradle's `tasks --all`); a name that is not one variant, such as a build type a flavoured app shares, is refused the same way before building. An APK with no signature (read from the APK, not its name) is reported as unsigned rather than installed. `env` (an object of names to values) sets environment variables for one Gradle build, for the JDK and SDK search and the variant listing too; `env_vars` reports `read_by_build` (the names its build scripts and convention plugins read), `unset` (those not set where Gradle ran), `passed` and `unnamed_reads`. Names only: values are never returned. `env` on an Xcode project is a 400. The kinds are `signing` (a keystore missing, or a password or key alias wrong), `jdk` (none found, or Gradle refusing the one it ran on; quern prefers a JDK in the range the project's Gradle supports and uses one outside it only when nothing else is found or it was chosen, and `java` then says so), `toolchain_jdk`, `android_sdk`, `sdk_packages`, `ndk`, `gradle_wrapper` and `gradle_distribution`. `org.gradle.java.home` is honoured where Gradle reads it: `-Dorg.gradle.java.home` in `gradle_args`, then `~/.gradle/gradle.properties`, then the project's. The Android Gradle plugin's own SDK downloads are off (`-Pandroid.builder.sdkDownload=false`) unless `gradle_args` sets them. `variant` is matched case-insensitively; a task name is refused with the variant it meant, and a build type that several flavours share names them. An APK this run did not package is not installed. With no `udid`/`udids`, the target is the active device if it is a booted Android device, else the one booted Android device. A dry run in `gradle_args` (`-m`) is refused. Some options can be applied by calling again with `java_home` or `gradle_args`; the others change the user's machine and are theirs to decide. Install refusals are read from adb's output, not its exit code: a different signing key can be got past with `uninstall_on_signature_mismatch=true`, which erases the app's data (said in `note`, or in `error` if the reinstall then fails), and a higher installed versionCode with `allow_downgrade=true`, for debuggable builds, which keeps it |
| (used by `build_and_install`) | GET | `/api/v1/device/build-progress` | Gradle builds running now: `{builds: [{project, task, started_at, elapsed_s, current, tasks_run, stage, progress_id}]}`, `current` being the last `> Task` line Gradle printed and `stage` what quern is doing (checking the variant, building, installing). A build is listed from the variant check until its installs finish. `progress_id` is whatever the caller passed as `progress_id` to build-and-install, so a client running several builds at once reads its own build's progress. The MCP tool polls it while a build runs and forwards it as progress notifications, so a long build is seen to move rather than look hung |

### UI interaction

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `get_ui_tree` | GET | `/api/v1/device/ui` | Accessibility tree |
| `get_element_state` | GET | `/api/v1/device/ui/element` | Query specific element state. Takes `snapshot_depth` and `source_timeout`; elements read through WDA carry `on_screen`. Through WDA a miss looks again at depth 25, even with `snapshot_depth` given (a floor); `deepen=false` keeps the read exact, and the 404 says how deep it looked |
| `wait_for_element` | POST | `/api/v1/device/ui/wait-for-element` | Poll until element appears. A timeout on web content carries `web_content_hint`, as on `tap_element`; through WDA, a timeout on an element never seen carries `snapshot_depth` and `depth_note`. Takes `deepen`, as on `tap_element` |
| `get_screen_summary` | GET | `/api/v1/device/screen-summary` | LLM-optimized screen description. With `identify=true`, `identified_as` and `confidence`; when identification could not run, `identify_error` and `identify_hint` say why and what to do |
| `tap` | POST | `/api/v1/device/ui/tap` | Tap at coordinates. `duration` (seconds, up to 10) holds the touch: a long press, on every backend -- sim-bridge, idb, WDA (touchAndHold, never re-sent after a timeout) and uiautomator2 (#251). 1.0 clears the long-press threshold on iOS (0.5s) and Android (~0.4s). On an iOS simulator the response says what the point `landed_on`, and the tap first waits for a recent screen change to settle (`waited_for_settle_ms`; `skip_settle` turns it off) |
| `restore_simulator_input` | POST | `/api/v1/device/ui/restore-input` | Take a simulator's touch, button and keyboard services back from Xcode 27's Device Hub. Restarts SpringBoard, so running apps are killed |
| `set_hinge` | POST | `/api/v1/device/hinge` | Fold, unfold or turn a foldable simulator (iPhone Duo, Xcode 27.1+) from inside it, as Device Hub does (#537). Body: `pose` (`closed` 0°, `open` 130°, `flat` 180°) or `angle` (0–180), swept over `duration` seconds (default 0.8; 0 sets it at once) from the angle the hinge reads now; `orientation` (`portrait`, `pud`, `landscape-left`, `landscape-right` — physical, as Device Hub's picker names them) is applied after any move. The answer is the outcome: `lit_panel` (and `lit_panel_before`), the panel taps and reads now go to, with `screen_id`, `orientation` and `size` in points, and `angle` read back. SpringBoard lights a panel from how the hinge moved, not only where it stopped, so a pose whose panel did not light within 3s carries a `warning`. A fold counts as a screen change: the next tap waits for the screen to settle, and so does one after a fold made in Device Hub. 400 on any other device |
| `get_hinge` | GET | `/api/v1/device/hinge` | A foldable simulator's hinge `angle` (degrees; null when the guest gives no reading, as after SpringBoard restarts, until the hinge next moves) and `lit_panel`. 400 on any other device |
| `tap_element` | POST | `/api/v1/device/ui/tap-element` | Tap element by label/identifier. Through WDA it taps only an element that is on screen and hittable, and a not-found lists the unreachable matches; `snapshot_depth` (WDA default 12) sets how deep the first read goes -- a miss looks again at 25 unless `deepen` is false -- and a physical iPhone without `setup_wda` is a 400. With `scroll_to_find` (the default) this can sweep for a long time; a client that disconnects abandons it rather than leaving it running. The request is closed **499** server-side, which a caller that has hung up does not receive — the observable effect is that the device stops being driven. `duration` holds the tap as a long press, as on `tap`, on every path -- the Android selector fast path, a web element and the tree. On an iOS simulator it first waits for the screen to settle if an action changed it in the last 1.5s (`waited_for_settle_ms`; `skip_stability_check` turns it off), then hit-tests the target's centre: a **409** with `status: "obstructed"`, `reason` (`covered` with `covered_by`, or `off_screen`) and `detail` means nothing was tapped. On Android's selector fast path a tap within 1.5s of a change -- including a previous tap -- first reads the target back to back until every read across 200ms agrees -- at least three, a slow read starting it over -- for up to about a second (`waited_for_rest_ms`; `skip_stability_check` turns it off, the tap after a scroll sweep included, #514). A target that never settles is tapped anyway and the response carries `target_moving: true` and a `warning`; the tree path -- every iOS simulator and physical iPhone, and Android when the fast path does not apply -- answers the same when the target's own frame changed across its stability wait (#529), with `waited_for_rest_ms` the length of that wait, and on an iOS simulator a target that moved off its point is refused, **409** `obstructed` with `reason: "moving"` and `target_moving: true`, rather than tapped; one whose match count changes meanwhile answers as the tree does, and one that vanishes is looked for again in the full tree, saying so in `fast_path`. A miss on web content -- Safari in front, or a tree collapsed to a bare `Application` -- carries `web_content_hint`, saying to call `get_web_content` first (#436). A control and its own decoration with the same label -- a tab and its icon, a cell and its text -- are one match: the control is tapped and `collapsed` lists what was folded into it. A control inside a container (a switch in its row, a button in an alert titled like it), unlabelled matches and matches side by side stay `ambiguous` (#490). A delivered tap the knowledge base documents as a `leads_to` edge is remembered, at no cost: a later `tap_element` 404 or `wait_for_element` timeout on that device, on a screen the tap is documented to leave and with no screen change through quern in between, carries `previous_tap` -- `tapped`, `expected`, `on`, `seconds_ago` and a `note` -- saying the tap probably did not register. It waits out the edge's `timeout:` (else 1s), and an exact identification names the app it judged by (`identified_app` on the screen context). With `verify_navigation: true` the tap is checked at once instead: when the screen identified before it is the edge's, the response carries `navigation` -- `expected` (a screen, or a list when the tap may lead to several), `from`, `arrived` and `waited_ms`, plus `on`, `stayed` and `timeout_s` on a miss -- and a missed one (`arrived: false`) a top-level `warning`, with `status` still `ok`. `arrived: null` with a `reason` (`unidentified`, `ambiguous`, `read_failed`, `no_settled_read`, `conditional`) makes no claim. The wait is the edge's `timeout:`, else 3s; `navigation_timeout` overrides it for the call. (#491) Every call except a long press first captures a frame of the screen (#502); a delivered tap keeps it. A later 404 or `wait_for_element` timeout on that device -- at least 1s after the tap (or the documented edge's `timeout:`), with no screen change through quern in between, on a screen that looks the same to within a few pixels -- carries `screen_unchanged_since_tap` (`tapped`, `seconds_ago`, `change`, `capture_ms`, `note`), unless `previous_tap` already explains it. An animated screen reads as changed and gets none. `note_unchanged: false` skips the capture. |
| `swipe` | POST | `/api/v1/device/ui/swipe` | Swipe gesture. `edge` (`left`, `right`, `top`, `bottom`) makes it a swipe from that screen edge, which the system takes: back from the left, home from the bottom, Control Centre or the notification shade from the top (#251). It must start at the edge -- within 3% of the screen across that axis -- or it is a **400**, on every backend, so it means the same thing on each. A simulator is told by a flag on every event, which only sim-bridge sets -- so on a simulator any other backend (idb, or WDA in driver mode) answers **400** -- while a real device decides from the start position. On a simulator the edge is checked against its portrait screen: quern does not follow a simulator's rotation |
| `gesture` | POST | `/api/v1/device/ui/gesture` | Multi-finger gestures and timed taps (#252). `type`: `pinch` (`scale`; >1 spreads, <1 squeezes; `distance` is the separation at the narrow end, default 60), `rotate` (`degrees`, positive clockwise; `distance` is the radius, default 80), `pan` (`dx`/`dy`, two fingers `distance` apart, default 40), `double_tap` (`count` taps, default 2, `interval` apart, default 0.08s -- inside the double-tap interval, which separate `tap` calls can miss), `two_finger_tap`. Centred on `x`/`y`, or on the element `label`/`identifier` names (one of the two); `angle` turns a pinch's line or a rotation's start from horizontal; `duration` (default 0.6s) for the moving ones. Returns the `center` and where each finger went (`fingers`), and `element` when centred on one. A point off the screen is refused, not clamped. iOS simulators through sim-bridge, physical iPhones through WDA's W3C pointer actions (one touch source per finger, never re-sent after a timeout), Android emulators and devices through scrcpy's server (control only, one per device, kept for the next gesture; without scrcpy a **400** names `brew install scrcpy`). On Android coordinates are pixels: the default distances are scaled by density, and a pinch's narrow end defaults to 30mm along the long axis, because ScaleGestureDetector ignores fingers closer than ~27mm. The idb backend answers **400** rather than sending one finger |
| `scroll_to_element` | POST | `/api/v1/device/ui/scroll-to-element` | Scroll a container until the target is in view, without tapping it. On iOS, bounded by a wall-clock deadline as well as `max_swipes`. A client that disconnects abandons the sweep instead of leaving it driving the device. The request is closed **499** server-side; a caller that has hung up does not receive it, so the observable effect is that the device is released. Takes `snapshot_depth` and `deepen`, as on `tap_element`; a not-found through WDA says how deep it looked |
| `get_web_content` | POST | `/api/v1/device/ui/web-content` | Read WKWebView content the accessibility tree cannot see (iOS simulator only) |
| `wait_for_settle` | POST | `/api/v1/device/ui/wait-settled` | Wait until the screen stops changing, by comparing successive screenshots |
| `type_text` | POST | `/api/v1/device/ui/type` | Type text |
| `clear_text` | POST | `/api/v1/device/ui/clear` | Clear text field |
| `press_button` | POST | `/api/v1/device/ui/press` | Press hardware button |

### Screenshots and preview

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `take_screenshot` | GET | `/api/v1/device/screenshot` | Capture screenshot |
| `take_annotated_screenshot` | GET | `/api/v1/device/screenshot/annotated` | Screenshot with accessibility overlays |
| `start_screenshot_timeline` | POST | `/api/v1/device/screenshot/timeline/start` | Auto-capture a screenshot after every UI action |
| `stop_screenshot_timeline` | POST | `/api/v1/device/screenshot/timeline/stop` | Stop the timeline and return its manifest |
| `get_screenshot_timeline` | GET | `/api/v1/device/screenshot/timeline` | Manifest of the active timeline, without stopping it |
| `preview_device` | POST | `/api/v1/device/preview/start` | Add a device preview (or all USB devices if no UDID; simulators must be named). A UDID already being previewed answers `already_active` with the original `started_at`, on every path (#351). An iOS response about one device carries `kind`: `device` or `simulator` |
| `stop_preview` | POST | `/api/v1/device/preview/stop` | Remove a device preview (or stop all if no UDID) |
| `preview_status` | GET | `/api/v1/device/preview/status` | Per-device preview state and available devices. `ios.available` lists booted simulators always, each with `kind` (#285); `ios.simulators_error` means they could not be listed, which is not the same as none booted. `ios.quern_media_built` says whether the simulator preview engine is built (setup builds it) |

### Device configuration

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `set_location` | POST | `/api/v1/device/location` | Set GPS location |
| `open_url` | POST | `/api/v1/device/open-url` | Open a URL the way a tapped link does, through the system's routing: `simctl openurl` on a simulator whatever its UI backend, WDA `/url` on a physical iPhone, and on Android a VIEW intent with no package, with the BROWSABLE category for an http(s) link -- so universal links and App Links are checked as for a user. `direct=true` (Android only, needs `bundle_id`; 400 on iOS) delivers to the package instead, bypassing App Links verification, for staging builds. `bundle_id` names the app the link should open in: the response reports `opened_in_app` (null with `opened_in_app_error` when it could not tell), `foreground_app`, `foreground_activity` on Android, `crashed` on Android (from the crash buffer; null with `crash_check_error` when it could not be read), and a `warning` when it went elsewhere. `via` names the transport, `route` the routing |
| `grant_permission` | POST | `/api/v1/device/permission` | Grant app permission |
| `set_locale` | POST | `/api/v1/device/locale` | Set the system locale (Android) |
| `set_hardware_keyboard` | POST | `/api/v1/device/keyboard` | Attach/detach the simulated hardware keyboard (iOS simulators) |
| `get_simulator_settings` | GET | `/api/v1/device/settings` | State of each simulator setting quern can change, and which runtimes each is verified on |
| `set_simulator_setting` | POST | `/api/v1/device/settings` | Turn auto-correction, auto-capitalization, password AutoFill and other simulator settings on or off; 409 on a booted simulator unless `reboot: true` |
| `set_font_scale` | POST | `/api/v1/device/font-scale` | Set the font scale (Android) |
| `set_display_density` | POST | `/api/v1/device/display-density` | Set the display density / DPI (Android) |

### App state and plist

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `save_app_state` | POST | `/api/v1/device/app/state/save` | Save a named checkpoint (data container + app groups, optionally the keychain) |
| `restore_app_state` | POST | `/api/v1/device/app/state/restore` | Restore a named checkpoint |
| `list_app_states` | GET | `/api/v1/device/app/state/list` | List saved checkpoints for a bundle ID |
| `delete_app_state` | DELETE | `/api/v1/device/app/state/{id}` | Delete a named checkpoint |
| `read_app_plist` | GET | `/api/v1/device/app/state/plist` | Read a plist, or a single key, from an app container |
| `set_app_plist_value` | POST | `/api/v1/device/app/state/plist` | Set a plist key |
| `set_app_plist_values` | POST | `/api/v1/device/app/state/plist/batch` | Set multiple plist keys in one call |
| `diff_app_plist` | GET | `/api/v1/device/app/state/plist/diff` | Compare a live plist against a saved checkpoint |
| `delete_app_plist_key` | DELETE | `/api/v1/device/app/state/plist/key` | Remove a key from a plist |
| `start_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/start` | Poll a plist and emit per-key changes as log entries |
| `stop_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/stop` | Stop polling a plist |
| `configure_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/configure` | Save a persistent watch config for a bundle ID |
| `get_plist_watch_config` | GET | `/api/v1/device/app/state/plist/watch/config` | Read the persistent watch configuration |
| `unconfigure_plist_watch` | DELETE | `/api/v1/device/app/state/plist/watch/configure` | Remove a persistent watch config |

### Device pool

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `resolve_device` | POST | `/api/v1/devices/resolve` | Resolve a device by criteria *or* by explicit `udid` (sets active device). Picks on one platform -- see "Choosing a platform" below |
| `ensure_devices` | POST | `/api/v1/devices/ensure` | Ensure N devices matching criteria are booted, on one platform as `resolve_device`. Fewer usable than asked for is a 404 when none match, and a 409 when some do but too few, booting, or shut down with `auto_boot` off. None matching while listing that platform's devices failed (simctl ran and errored) is a 503: it could not tell |

#### Choosing a platform

iOS 16 and Android 16 both exist, so a bare `os_version` can mean either (#582). `resolve_device`, `ensure_devices`, `reserve_devices` by count, and `list_devices` take `platform` (`ios` or `android`) and `platform_source`, and pick on one platform, first that applies:

1. **The request names it**: `platform` with no `platform_source`, a `device_type`, or a prefixed `os_version` ("Android 16", "ios18.6"). This restricts the pick; naming two platforms is a 400.
2. **The project names it**: `platform` with a `platform_source`. The MCP sends the agent's project's platform this way when the agent didn't give one: `.quern/config.json`'s `"platform"` (`knowledge_base`), else read from its files (`project`) -- an Xcode project is iOS, an Android app module Android, a project with both is neither. It is a default: a request with criteria that matches nothing on it gets the other platform's matches when they are no more than it needs (one, or `count`); more are a 409 `other_platform` listing them. A request with no criteria never crosses, and `resolve_device` with no criteria does not return an active device on the other platform. `platform_source` is one of `request`, `knowledge_base`, `project`.
3. **The criteria name it**: matches on one platform only are that platform's. Matches on both are a 409 `ambiguous_platform`, with `matches` (up to 10 per platform), `match_counts`, and `retry_with` (the `platform` values that would choose). `list_devices` lists both instead of refusing.
4. **Nothing does**: as before. `resolve_device` and `ensure_devices` default to a virtual device -- an iOS simulator of the configured family, or on an Android pick an emulator -- so a physical phone is picked only when `device_type` asks for one.

Answers say which platform was used and why: `platform`, and `platform_source` -- `request`, the project's source, or `only_match`. Names match a device's name or model, with underscores read as spaces, so "Pixel 7" finds the AVD `Pixel_7_API_34`; an exact match on either platform wins over substrings on both.

### Sessions and reservations

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `reserve_devices` | POST | `/api/v1/sessions/{id}/reservations` | Reserve devices for this session (udids, count plus criteria, or a group), all or nothing; 409 names a holder. Each reserved device comes back with its name, model, OS version, family and type, so a caller that reserved by criteria knows what it got. When the pool has too few free devices for the criteria, the MCP adds which paired servers have enough -- booted, or shut down when `auto_boot` allows -- and how to switch (`use_remote`). See "Sessions and reservations" above. After a `use_remote` switch, the MCP adds `carried`: the mocks and session capture moved from the previous server, set up on the simulators just reserved |
| `release_devices` | DELETE | `/api/v1/sessions/{id}/reservations` | Release one device (`?udid=`) or all |
| `session_status` | GET | `/api/v1/sessions/{id}` | One session: state (`live`, `grace` with `grace_remaining_s`) and reservations. With `all`, the MCP tool lists every session |

### Remote access

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| — | POST | `/api/v1/remote/pairing-codes` | Owner only, on the server's own Mac (`quern pair LABEL`): a one-time pairing code, valid 5 minutes, for a client to be labelled `label`; answers `{code, expires_in, url, fingerprint}`. 409 when the remote listener is off |
| — | GET | `/api/v1/remote/status` | Owner only (`quern remote status`): whether the remote listener is on and listening, its `url` and certificate `fingerprint` |
| — | POST | `/api/v1/remote/pair/hello` | No key, remote listener only: the server's half of pairing, `{code_id, nonce}` → `{nonce, proof}`. See "Remote access" above |
| — | POST | `/api/v1/remote/pair/finish` | No key, remote listener only: the client's proof → `{key, label, fingerprint}`, a new client key |
| `install_app` | POST | `/api/v1/uploads` | `?name=MyApp.ipa`: an app to install, as the raw body (`.ipa`, `.apk`, or `.app.tar.gz`), or a knowledge base as `<app>.knowledge.tar.gz` for `load_landmarks` (#560): → `{upload_id, name, kind, bytes, expires_in}`. 413 past the key's cap, 507 past the disk's free-space floor. Kept for the key that sent it. See "Remote access" above |
| — | GET | `/api/v1/uploads` | The caller's own uploads, and `max_bytes` |
| — | DELETE | `/api/v1/uploads/{id}` | Delete one of the caller's own uploads |

### App knowledge and landmarks

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `init_app_knowledge` | — | — | Scaffolds or detects a `.quern/knowledge/` directory on disk; performs no server call |
| `load_landmarks` | POST | `/api/v1/landmarks/load` | Load landmarks from a knowledge base path or inline JSON (the owner's), or -- with `upload_id` and `app`, for the caller's held session only -- from an uploaded knowledge base (#560; 409 without a held session, 400 for an upload that is not a knowledge base or has no screens; the response says `"scope": "session"`). Returns `screens` count, a categorized `skipped[]` array (legacy-format files, stubs, malformed YAML), and a `conventions` block: each file's declared `landmark_conventions` beside the findings quern computed. A `navigation` block counts the `leads_to` edges `tap_element` will check, and lists under `unusable` the entries it will not: an action that is not a `tap_element` call with a label or identifier, or a link to no loaded screen or one with no landmarks. An edge whose `timeout:` is unusable is still checked, at the default wait, and is listed under `timeouts_ignored`. The inline form takes `leads_to` beside `landmarks`. Edges available from every screen come from `app.md`'s `global_leads_to` (inline: a top-level `global_leads_to`), are applied to each screen except the targets of edges for the same element and their `except:` list -- a screen's own edge for the element wins -- and the number declared is `navigation.global` (#503). Besides `unusable` and `timeouts_ignored`, the block can carry `except_ignored` (edges kept with an unreadable `except:`) and `app_md` (a problem with `app.md` itself; reported only when it mentions `global_leads_to`). `remember=true` loads it again at every server start (kept in `~/.quern/config.json`); `app` may be omitted for a path in a project's `.quern/`, whose `config.json` names it. A path that is not a directory is a 400, and so is one with no screens when the app already has a loaded set, which is left in place |
| `identify_screen` | POST | `/api/v1/landmarks/identify` | Match the live UI tree against loaded landmarks. Returns matched screen, confidence, and full per-landmark detail in `partial_matches`. With nothing loaded: `error: "no_landmarks_loaded"` and a `hint` saying how to load a knowledge base, or to suggest building one |
| `list_landmarks` | GET | `/api/v1/landmarks` | List loaded landmark sets per app, `sources` (the path each set was loaded from, or `inline`), and under `remembered` each knowledge base loaded at every start: its path, whether it is loaded, and `at_start` (screens loaded at this start, or why it did not load) |
| `unload_landmarks` | DELETE | `/api/v1/landmarks` | Unload landmarks for an app or all apps. The caller's own session sets go first (`"scope": "session"`); a client has only those, and gets 404 when it has none. `forget=true` also stops loading it at every start, and applies to server-wide sets only |
| `validate_landmarks` | POST | `/api/v1/landmarks/validate` | Detect collisions between screens with overlapping landmark sets, and report the same `conventions` block as `load_landmarks` |

### WebDriverAgent (physical devices, and simulators on request)

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `setup_wda` | POST | `/api/v1/device/wda/setup` | Build and install WDA on a physical device; on a simulator, build the unsigned simulator artifact (optional — `start_driver` builds on first use). WDA is the version quern pins (`server/device/ios/wda-pin.json`), fetched from npm's `appium-webdriveragent` and checked against its sha512; a build of another version is rebuilt here. If the rebuild fails, the build already there is installed instead and `wda_update_failed` says why, provided it was made for the same team and the rebuild was not forced; a failed `force` rebuild returns the error, leaving the old build on disk but not reinstalling it. The answer carries `wda_version` and `wda_pinned`. The signing team comes from Xcode's account list, or — when that is empty, as on Xcode 27 — from the installed iOS development provisioning profiles, Xcode's last-used team deciding between several |
| `start_driver` | POST | `/api/v1/device/wda/start` | Start WDA driver. On a simulator this puts it in WDA mode: once WDA answers, its UI reads and actions go through WDA, elements read from it carry `xcui_type`, and `backend` reports `wda`. A runner that does not answer leaves the simulator on the default backend, with `status: failed`. The answer carries `wda_version` and `wda_pinned`, and `wda_update` when they differ: a phone's build is rebuilt only by `setup_wda`, a simulator's on its next start (keeping the old one, with `wda_update_failed`, if that fails) |
| `stop_driver` | POST | `/api/v1/device/wda/stop` | Stop WDA driver. A simulator returns to the default backend, reported as `backend`; it leaves WDA mode even if stopping fails |

## Endpoints with no MCP tool

Reachable over HTTP only — streaming endpoints (an MCP tool can't hold an SSE connection),
public probes, and a few operations the CLI uses directly.

| Method | Path | Description |
|---|---|---|
| DELETE | `/api/v1/proxy/mocks` | Clear all mock rules |
| DELETE | `/api/v1/sessions/{id}` | End a session and free its devices: the holder finishing, or the owner releasing a stuck one (#254) |
| GET | `/api/v1/sessions` | Every held session, its state and devices (#254) |
| POST | `/api/v1/sessions` | Open a session (`{name?}`), or locally take one in grace back by name; returns `resume_token` and `live_path`. The MCP and `quern session run` call it (#254) |
| DELETE | `/api/v1/recordings/{id}/viewer` | Revoke every LAN viewer link for a recording at once (#449). `{"revoked": n}` |
| GET | `/` | Redirects to `/docs` (public) |
| GET | `/api/v1/device/preview/devices` | List what can be previewed: USB iOS devices and booted simulators, each with `kind` |
| GET | `/api/v1/device/video` | Live MJPEG video stream |
| GET | `/api/v1/health` | Same as `/health` (public) |
| GET | `/api/v1/logs/stream` | SSE real-time log stream |
| GET | `/api/v1/proxy/bypass` | List bypass patterns |
| GET | `/api/v1/proxy/cert` | Download CA certificate (public — see note above) |
| GET | `/api/v1/proxy/cert/status` | Check certificate installation status |
| GET | `/api/v1/proxy/flows/stream` | SSE real-time flow stream |
| GET | `/api/v1/system/channel` | Current update channel preference |
| GET | `/health` | Fast liveness ping (public). Does no device-tool probing — kept sub-millisecond so CLI health checks can't time out |
| GET | `/tools` | Device-tool availability and UI cache stats (public). Backs `quern doctor` and `quern status`. Probes are bounded and run concurrently; the response also re-syncs the server's simulator UI backend |
| GET | `/api/v1/device/tools/sites` | Per-install-site tool versions and provenance. Authenticated: it returns absolute paths, which `/tools` deliberately does not |
| POST | `/api/v1/builds/parse` | Submit xcodebuild output |
| POST | `/api/v1/device/active` | Set the active device by UDID, for the calling session (`X-Quern-Session`); with no session, the shared one |
| POST | `/api/v1/devices/refresh` | Refresh pool from simctl |
| POST | `/api/v1/proxy/filter` | Set proxy capture filters |
| POST | `/api/v1/proxy/intercept/release-all` | Release all held flows |
| POST | `/api/v1/recordings/{id}/viewer` | Another LAN viewer link for a recording that is filming (#449): `{"viewer_url"}`. **409** when it isn't filming, **503** when the viewer is off or its listener isn't running, with the reason |
