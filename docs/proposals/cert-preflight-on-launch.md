# Cert Preflight on `launch_app` — Proposal

**Status:** partially implemented. The preflight ships on `configure_system_proxy`
rather than `launch_app` — enabling capture is where the broken state is created, and
catching it there costs no relaunch. `auto_install_cert` (§4.2) ships as proposed, and
is surfaced in `proxy_status` and the menu-bar Settings pane. The `launch_app`
preflight in §4 is **not** implemented; §6's known gap is therefore wider than written.
**Raised by:** a live debugging session on 2026-09-01 (see §1).
**Scope:** `launch_app`, `proxy_status`, one new config field.

---

## 1. The problem, as actually encountered

While testing web-view accessibility against Metatext on an iOS 18.6 simulator, the
app's `WKWebView` rendered blank. Nothing in the app was wrong.

`proxy_status` showed:

```
local_capture: ["Metatext", "MobileSafari", "com.apple.WebKit.Networking"]
flows_captured: 0
cert_setup: { …two other devices… }        # the booted simulator was absent
```

Local capture was intercepting the app's TLS, but the simulator did not trust the
mitmproxy CA. Every HTTPS connection from those processes failed. After
`install_proxy_cert` and a relaunch, the same screen rendered fully and the proxy
captured 39 flows.

Three properties make this worth fixing rather than documenting:

1. **The symptom points away from the cause.** What you see is "this screen is
   blank" or "the app has no network." Nothing mentions the proxy, the
   certificate, or local capture. The natural next move is to debug the app.
2. **The blast radius is wider than it looks.** It is tempting to file this as a
   web-view problem because that is where it was noticed. It is not.
   `com.apple.WebKit.Networking` covers every WebKit request in every app, and a
   named process covers all of that app's HTTPS. Sign-in, timelines, and API
   calls fail exactly the same way; the web view was simply the only network on
   the screen we happened to be looking at.
3. **Quern created the state.** `local_capture` is set by Quern, and the cert is
   installed by Quern. Both halves are ours; only the correlation is missing.

Nothing in the codebase currently correlates `local_capture` with per-device cert
state. `launch_app` performs no proxy-related checks at all.

## 2. Why not just auto-install the cert

The obvious fix — install the CA on every boot — is the wrong default:

- `CLAUDE.md` states: *"Never auto-configure the system proxy. Never leave it
  configured when not actively testing."* Silently installing a MITM root CA is a
  **larger** commitment than a system-proxy toggle, not a smaller one. It
  persists across sessions and survives the capture window that motivated it.
- It makes the simulator trust a CA whose private key sits on disk in
  `~/.mitmproxy`.
- It would fire on the majority of sessions that never capture anything.
- The user has to know it happened in order to undo it.

The certificate is only needed when capture is active. Installing it
unconditionally trades a loud, rare confusion for a quiet, permanent change in
posture.

## 3. Why a plain warning is not enough either

A warning returned *after* `launch_app` succeeds costs a full cycle: the launch
happens, HTTPS fails, the user installs the cert, and the app has to be
relaunched anyway.

That last step is not optional. `simctl keychain add-root-cert` needs no device
reboot, but in the session above the cert was installed while the app was
running and the page only loaded after a relaunch — WebKit's networking process
appears to cache the TLS failure. So the post-hoc path is reliably
*launch → fail → install → relaunch*.

Since both facts needed to predict the failure are known **before** the launch,
the check belongs before it.

## 4. Proposed behaviour

Preflight inside `launch_app`, before anything else happens. The check is free:
the `local_capture` list and `cert_setup[udid].cert_installed` are already in
memory.

| State | Behaviour |
|---|---|
| Capture off, or cert already present | Launch normally — unchanged, no regression for the common case |
| Broken combination, `auto_install_cert: true` | Install the cert inline, then launch. One call, no round trip |
| Broken combination, no policy set | **Return without launching**, with a structured response the agent can act on |

The one-shot behaviour comes from the config flag. For the first encounter, one
round of asking is the correct cost — installing a root CA without consent is
precisely the thing §2 argues against.

### 4.1 The refusal must offer three resolutions

"Install the cert" is not always what the user wants. A response that only offers
that will railroad every user into installing a CA, which reintroduces the
problem §2 avoids. The structured response should name:

1. **Install the certificate** — once, or always (§4.2).
2. **Remove this app from `local_capture`** — they may not want capture for this
   run at all, and this resolves the conflict without touching trust settings.
3. **Launch anyway** — valid when deliberately exercising TLS-failure paths.

### 4.2 The "always" option

`auto_install_cert: true` in `~/.quern/config.json`, mirroring the existing
`update_check: false` field.

Two requirements, both about not recreating the original bug in a new form:

- **Discoverable.** Surface it in `proxy_status`. A silent, persistent
  CA-install policy is worse than the failure it prevents.
- **Revocable.** `quern uninstall` should offer to remove certificates Quern
  installed. Ideally the install manifest tracks them the way it tracks
  `pipx_global` entries today.

### 4.3 Consent pattern to reuse

This already exists in the codebase. `update_quern`'s tool description reads:

> Only call this after the user has confirmed they want to update — typically
> prompted by an `update_available` hint in `ensure_server`'s response.

Same shape: a structured hint in one response, plus a tool description that
instructs the agent to obtain consent. Reuse it rather than inventing a second
mechanism. **This means the `launch_app` MCP tool description needs a matching
paragraph** — without it, agents will treat the refusal as a transient error and
retry blindly.

## 5. Also worth doing regardless

Add a `capture_without_cert` entry to `proxy_status.warnings`
(`server/api/proxy.py:97` already builds that list for `multi_interface_active`).
It is a few lines and covers the diagnostic path for cases the preflight cannot
reach — see §6.

Word it around processes, not web views:

> local capture is enabled for N processes, but device *X* does not trust the
> mitmproxy CA — HTTPS from those processes will fail

## 6. Known gap

The preflight cannot catch an app launched by tapping its icon in the simulator,
or one already running when capture was enabled. `proxy_status.warnings` is the
fallback for those. This is an accepted limitation, not an oversight.

## 7. Open questions

1. **Is refusing to launch acceptable?** It changes `launch_app`'s behaviour in a
   state where it previously "succeeded" (by launching an app that could not
   reach the network). Argued here as a fix rather than a break, but it belongs
   under **Changed** in the CHANGELOG either way, and it should return a
   structured refusal rather than a generic error.
2. **Should the same preflight apply to Android emulators?** The cert story there
   differs — rootable images only, and `install_proxy_cert` also configures the
   HTTP proxy. Probably yes, but the failure mode has not been reproduced.
3. **Does the relaunch requirement hold generally,** or was it specific to
   WebKit's networking process? Worth confirming: if a running app picks up a
   newly installed cert for new connections, the argument in §3 weakens for
   non-WebKit traffic (though not the argument for preflighting).
4. **Per-device or global policy?** `auto_install_cert` as proposed is global.
   Per-device would be more precise but adds state to manage.

## 8. Rough implementation sketch

- Preflight helper in `server/proxy/cert_manager.py` — takes a UDID and the
  capture list, returns an enum-ish verdict.
- Hook in `server/api/device.py::launch_app`, before `controller.launch_app`.
- `auto_install_cert` in `server/config.py`, alongside `update_channel`.
- `capture_without_cert` in the `proxy_status` warnings list.
- Tool-description paragraph in `mcp/src/tools/device.ts`.
- Tests across capture on/off × cert present/absent × policy set/unset, plus one
  asserting the common path is untouched.
