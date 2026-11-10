# Working across machines

*`quern://guide/sharing/remote-servers` -- part of `quern://guide/sharing` in the Quern agent guide. Start from `quern://guide`.*

The user may have paired other Macs running quern (`quern pair` / `quern connect`), so a device you need may be on another server.

- **`list_remotes`** lists every server you could use: this Mac first (`local`), then each paired one, with whether it answers, its version, and its devices (name, model, OS version, family, state, and free / yours / held). The one marked `"current": true` is the server your tools talk to now. Only its devices can be reserved.
- **When `reserve_devices` finds nothing, or a filtered `list_devices` comes back empty, read the hint** (the reserve error's text, or `elsewhere` on the list). It names the paired servers with matching free devices and the exact `use_remote(name=...)` call. Don't conclude the device doesn't exist, and don't offer to download a runtime, before reading it.
- **`use_remote(name)`** -- or `use_remote(name="local")` to come back -- switches every later tool call, and your held session, to that server. It switches only once your session is live there; otherwise you stay where you were, and the answer says why.
- **It refuses while you hold devices** on the current server, or when the current server can't say what you hold -- not retryable as-is. Call `release_devices` first, or `use_remote(name, release=true)`.
- **What follows you:** your mocks -- removed from the old server and recreated on your next reservation there; read `carried` on that `reserve_devices` answer for the new rule ids and anything still waiting -- your `add_session_capture` processes, and the project's knowledge base (`knowledge` in the answer).
- **Android proxy routing:** an Android device you routed through the proxy (`record_device_proxy_config` with `apply=true`) while holding it is routed again on the new server's next Android reservation when you switch with `use_remote(release=true)`; a separate `release_devices` first unroutes it instead. A routed device needs the new server's proxy running, or it loses its network.
- **What stays:** local capture, the system proxy, intercepts, bypass, recordings, logging and device settings. Check `proxy` in the answer: mocks and capture need the new server's proxy running, and only its owner can start it.
- **Mocks that couldn't be removed** from an unreachable server are listed in `carried.still_on_old`; switching back there tries again (`left_here_earlier`).
- **The `quern` CLI in your shell isn't switched.** It still talks to this Mac, so don't shell out to `quern` expecting the remote.
