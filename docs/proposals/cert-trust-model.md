# Certificate trust: current model, its failures, and where to land — Spec

**Status:** specification for the refactor in #149. Nothing here is implemented.
**Supersedes nothing.** `cert-preflight-on-launch.md` describes the *gate*; this
describes the *answer the gate asks for* — and widens the question it asks.
**The question is "will capture work, and what is the next step?"** — not "is the
CA trusted". That decision (§6, settled 2026-09-13) is what makes this a
different document from the preflight proposal rather than an extension of it.
**Raised by:** three field reports and one week of fixes on 2026-09-11 → 2026-09-13,
during which two fixes had to be walked back. §2 is a catalogue rather than a
narrative because the same shape produced all seventeen entries in it.
**Related:** #149 (the boolean), #147 (`DeviceState` is simulator vocabulary),
#150 (`unverified_no_traffic` ambiguity).

---

## 0. The scenario this all serves

Everything below exists to prevent one thing, described by the user who asked
for it before any of the code was written:

> I ask Claude to enable the proxy for a device and it will do so, but if the
> cert isn't installed, the network calls fail and everything seems broken
> until I realize I need to prompt Claude to install the cert.

Two properties make it worth structural effort rather than a warning:

- **The symptom points away from the cause.** A blank screen, or an app with no
  network. Nothing mentions the proxy or the certificate, so the natural next
  move is to debug the app. Three separate field reports describe exactly this,
  one of which cost an hour and ended in a wrong conclusion ("staging
  authentication is down") reported to a colleague.
- **Quern created the state.** It sets `local_capture`, it configures the system
  proxy, and it installs the CA. Every half is ours; only the correlation is
  missing.

## 1. What the question actually is

Every caller in this area is asking one of four different questions, and today
they all call something that returns `cert_installed: bool`:

| # | Question | Who asks it | Cost of a wrong answer |
|---|---|---|---|
| Q1 | *Would capture through this device work right now?* | the preflight gate on `configure_system` and `local_capture` | every HTTPS request fails with nothing pointing at the proxy |
| Q2 | *What did we last record, and when?* | `GET /proxy/cert/status`, `proxy_status` display | a stale record read as fact |
| Q3 | *Is it true right now — go and check.* | `POST /proxy/cert/verify`, setup wizard | a slow call, or a silent fallback to Q2 |
| Q4 | *Which devices match `cert_installed=X`?* | `list_devices(cert_installed=…)` filter | the wrong device set, silently |

Q1 and Q3 must never be answered from a record. Q2 must never be presented as
Q3. Q4 is Q3 applied to a set, and today degrades to Q2 for anything not a
booted simulator.

### 1.1 And "does it trust the CA" is only the middle layer

Capture failing produces one symptom — no traffic, blank screen — from three
independent causes, and the current model can only see one of them:

| Layer | Question | Fails when | Modelled today |
|---|---|---|---|
| **L1 routing** | does this device's traffic reach us at all? | WiFi proxy points at a stale host or port; system proxy off; device left the network | only indirectly, via `wifi_proxy_stale` |
| **L2 trust** | does it accept our CA? | never installed; erased; CA regenerated | **this is the whole of `cert_installed`** |
| **L3 app** | does the *app* permit interception? | certificate pinning | **not at all** — one line of prose in the agent guide |

#### 1.1.1 Measured: `www.apple.com` is a real, free rejection fixture

Run on 2026-09-13 against a booted iOS 18.6 simulator that trusts the CA, with
`local_capture` active on `MobileSafari` / `com.apple.WebKit.Networking`:

| Host | Flows captured | TLS | Outcome |
|---|---|---|---|
| `example.com` | 2 | decrypted | 200 / 404 |
| `www.iana.org` | 8 | decrypted | fine |
| `neverssl.com` (http) | 5 | n/a | fine |
| `support.apple.com` | 23 | decrypted | fine |
| `km.support.apple.com`, `supportmetrics.apple.com` | 7 | decrypted | fine |
| **`www.apple.com`** | **0** | — | **client refused the certificate** |

Safari displayed *"This Connection Is Not Private — This website may be
impersonating www.apple.com"*. So the rejection is **host-specific, not blanket
Apple**: the same CA that decrypts `support.apple.com` 23 times is refused for
`www.apple.com`. Pinning is still a client-side property, but the client doing
it here is Safari/WebKit on behalf of specific Apple properties, not the app.

**Consequences for the matrix:**

- `www.apple.com` is a **usable real-world rejection fixture requiring no app at
  all** — free, zero-setup, and it exercises the full L2 path on any device that
  can open a browser. It is a *corroborating* fixture, not the primary: it is a
  third party's production behaviour and can change without notice, and it is
  host-specific, so `support.apple.com` would silently test nothing.
- `QuernProbe` with a switchable pin stays the **primary** L3 fixture (row 62),
  because only a pin we control gives a deterministic, inspectable failure.

**And it is the strongest available evidence for D16.** The device stated the
problem unambiguously on screen. Quern recorded **nothing**: zero flows for the
host, no error entry, nothing in `server.log`, and `proxy_status` still
`warnings: []`. A client rejecting our certificate is currently
indistinguishable from no traffic having been sent — which is precisely §0's
failure mode, reproduced from the inside. The only thing that revealed the truth
was the simulator's screen, which is oracle #2 of §4.5.1 proving itself in the
same run that motivated it.

**A second measurement from the same run, and an L1 trap.** All 45 flows were
attributed to `com.apple.WebKit.Networking` and **none to `MobileSafari`** —
even though Safari is the app that was driven. Safari's requests egress through
the WebKit networking extension, not through the process bearing the app's name.

**The product already knows this.** The default pairs them deliberately —
`config.py:279` and `main.py:1427` both resolve an unspecified list to
`["MobileSafari", "com.apple.WebKit.Networking"]`. Anyone who takes the default
is fine, which is why this has not bitten.

The gap is in **what tells someone how to override it**, and all three are
wrong in the same direction:

| Where | Says | Actual |
|---|---|---|
| `main.py:1577` CLI help | `default: MobileSafari` | the pair, set at `main.py:1427` |
| `proxy.ts:313` | `e.g. ["MobileSafari"]` | captures no web traffic |
| `proxy.ts:920` | `e.g. ["MobileSafari", "Metatext"]` | captures no web traffic |

So the failure needs someone to override the default while following the
documentation — and the MCP descriptions are the ones an **agent** reads, which
for this project is the primary consumer. The result is §0's failure mode one
layer lower: the list is accepted, read back verbatim by `proxy_status`, and
captures nothing.

This belongs in the L1 diagnosis: *"capture is configured for processes that
have produced no traffic — did you mean the networking extension?"* is
answerable, because the proxy can see which processes flows actually arrive
from.

Those same domains matter for one more reason: **quern bypasses nothing by
default** (`bypass_patterns: []`; `*.apple.com` appears only as an example
string in the `set_bypass` docstring), so with capture on, Apple system traffic
is intercepted too. Failures there are a **known non-cause** — not a cert
problem and not the user's app — and the diagnosis in §3.4 should say so rather
than letting L2 take the blame.

A green `cert_installed` promises nothing about L1 or L3, and the user's actual
question spans all three. This is why `cert_installed` keeps being read as a
promise it cannot make: it is the only layer with a field.

The layers are **ordered and individually observable**, which is what makes a
single probe (§3.4) able to separate them: no connection at all is L1; a
connection whose TLS handshake the client rejects is L2; a successful handshake
to other hosts but not this one is L3.

**The single boolean cannot distinguish these, and neither can the function
names.** `is_cert_installed` answers Q1, Q2, Q3 and Q4 depending on a keyword
argument, the device kind, and the age of a file.

## 2. Catalogue of defects and limitations

Numbered so the test matrix in §5 can cite them. "Fixed" means the symptom is
gone; unless stated, the shape that produced it is not.

### 2.1 Fixed, but only at the call site

| ID | Defect | Evidence | Status |
|---|---|---|---|
| D1 | `set_local_capture` had no cert preflight at all, while `configure_system` had one. Local capture routes traffic through the proxy just as surely. | Field report: an hour spent concluding staging auth was down. | Fixed in `525d9e0` by extracting `_ensure_ca_is_trusted` and calling it from both. **A third capture-enabling path would have to remember.** |
| D2 | The preflight read `cert-state.json` directly, bypassing the cache expiry and TrustStore fallback `is_cert_installed` already had. | Erased simulator reported as trusting the CA; record 10.5 h older than the erase. | Fixed. Was the third instance that week of "the correct function exists, the caller never switched to it". |
| D3 | The preflight then asked with `verify=False`, so a record written in the last hour was returned unchecked. | Measured end to end: `simctl erase` at 17:50, TrustStore empty, `POST /proxy/local-capture` → **200**, record 3 minutes old. | Fixed in `525d9e0` (`verify=True`). The TrustStore query costs **0.6 ms**; the cache was never worth it on this path. |
| D4 | `proxy_status` reported the recorded `cert_installed` as current fact with no staleness signal. | Same field report: "I read `cert_installed: true` and moved on, which is the natural thing to do." | Fixed by adding `cert_trust_stale`, computed per device by `trust_is_stale`. |
| D5 | Widening verification to *all booted devices* sent physical iOS devices to the simulator TrustStore path, which answers `false` — and `is_cert_installed` **writes** what it learns, destroying the record `_verify_physical_device` reads. | Caught by the user noticing a WiFi-configured phone flip to untrusted. | Fixed by scoping to `SIMULATOR` + `BOOTED` at each call site. **That is now the third site that has to know the kind-specific rule.** |

### 2.2 Live, unfixed

| ID | Defect | Evidence |
|---|---|---|
| **D6** ([#151](https://github.com/quern-dev/quern/issues/151)) | **A cached answer survives the CA being regenerated.** `is_cert_installed` computes `expected_fingerprint`, then returns `cached["cert_installed"]` without ever comparing it to `cached["fingerprint"]`. Regenerate the mitmproxy CA and every device reports trusted — for the *old* CA — for up to an hour. | Confirmed empirically: record with fingerprint `0000…`, current CA `9b6fc9af…`, TrustStore stubbed `False` → `is_cert_installed` returned **True** and never consulted the TrustStore. |
| **D7** | **`get_device_cert_state` stamps `verified_at=now()` unconditionally**, including when it answered from cache and verified nothing. The timestamp that staleness detection depends on is written by a path that did not verify. | Confirmed empirically: `verify=False`, TrustStore never consulted, returned `verified_at` = now. |
| **D8** | **A read-shaped call writes to disk.** `is_cert_installed` ends its slow path with `update_cert_state`. There is no read-only variant; `get_device_cert_state` is a different shape and also writes (via D7). | The mechanism behind D5's data loss. |
| **D9** | **The gate does not cover Android.** `simulators_without_cert` filters `device_type != DeviceType.SIMULATOR`, so a booted Android emulator with no system cert passes the preflight that exists to stop exactly that. | Code read: `cert_preflight.py`, the `continue` in the device loop. |
| **D10** | **Android gets a contradictory status pair.** `verify_cert` routes anything not `DeviceType.DEVICE` to `_verify_simulator`, which calls `check_truststore_status(udid, …)` — a `CoreSimulator/` path lookup. An Android device with the cert installed returns `cert_installed: true` with `status: "never_booted"`. | Code read: `proxy_certs.py:229-234`. |
| **D11** | **The preflight fails open silently.** `except Exception: return []` means any bug inside it disables the gate and reports "nothing missing". | Hit while writing the tests for D3: a controller double without `_is_android` produced a green test for a gate that never ran. |
| **D12** | **`unverified_no_traffic` conflates "no requests yet" with "the recorded `client_ip` is on a network this device left".** Opposite remedies: wait, versus reconfigure. | Filed as #150. `wifi_proxy_stale` / `active_wifi_network` are already computed at read time and would tell them apart. |
| **D13** | **Six-plus callers read the record directly and do not mean the same thing by it.** Some say "cached installation state" and point at `POST /cert/verify` (honest, Q2). Others render it as current fact (Q3). Telling them apart required reading each one. | `read_cert_state*` call sites in `api/proxy.py`, `api/device.py`, `api/proxy_certs.py` (×3), `lifecycle/setup.py`. |
| **D14** | **Erase detection exists twice at different fidelity.** `is_cert_installed` logs a warning; `_verify_simulator` populates `erased_devices` in the response. Neither feeds the other, and the log is the only signal on the preflight path. | Code read. |
| **D16** | **The proxy discards the one direct observation of the failure.** mitmproxy 12.2.3 offers `tls_failed_client` (`conn.peername` + SNI). The addon implements `tls_clienthello`, `request`, `response`, `error`, `client_disconnected` — and not this one. The exact event §0 is about is seen and thrown away. | `grep -c tls_failed_client server/proxy/addon.py` → **0**. Hook confirmed present in the installed mitmproxy. |
| **D17** | **Physical devices cannot be asked to generate traffic, so their trust is unknowable in principle today.** `controller.open_url` calls `_require_simulator`; it is simulator and Android only. There is no probe, which is why `unverified_no_traffic` exists at all. | Code read, `controller.py:687-692`. The capability exists unused: `devicectl device process launch --payload-url`. |
| **D15** | **The refusal cannot distinguish "never trusted the CA" from "trusted it until it was erased".** `refusal_detail` produces the same text for both: *"N booted simulator(s) do not trust the mitmproxy CA."* The second case has an obvious answer and evidence of prior consent for that specific device; the first is a genuine decision. **Agreed during the fix work and never implemented** — the user proposed auto-installing on a device that had recorded one, and this was the counter-offer that gets the same outcome without breaking I1. | Design discussion, 2026-09-13. The record needed to tell them apart is already in `cert-state.json` (`installed_at`, `cert_installed`). |

### 2.3 The shape underneath all of them

`cert_installed` is one boolean over **four** device kinds whose truth is
established in completely different ways and invalidated by different events:

| Kind | Ground truth | Invalidated by | Knowable without the device? |
|---|---|---|---|
| iOS simulator | SQLite: `CoreSimulator/Devices/<udid>/data/private/var/protected/trustd/private/TrustStore.sqlite3` | erase; CA regeneration | yes — local file, 0.6 ms |
| iOS physical | successful intercepted HTTPS flows from a recorded `client_ip` | erase; **WiFi change**; host IP change; profile removal | no — requires traffic |
| Android emulator | `adb` against the system cert store | wipe; CA regeneration | no — requires `adb` |
| Android physical | `adb` against the system cert store | wipe; CA regeneration; rootability loss | no — requires `adb` |

Two things follow that no amount of call-site scoping fixes:

1. **Verification is not uniformly available.** For a simulator it is cheap and
   always possible. For a phone it is *impossible on demand* — you cannot
   verify a device that has sent no traffic. "Not verifiable right now" is a
   first-class answer that the type cannot express.
2. **The same event means opposite things.** A WiFi change is inconsequential
   for a simulator (it routes through the host's stack) and invalidating for a
   phone (it carries per-network proxy config). One flag cannot carry both.

### 2.4 The meta-pattern: the correct function exists, the caller never switched

Worth recording because it is the strongest argument for changing the shape
rather than adding another correct function.

`is_cert_installed` already expired its cache after an hour, already fell
through to a real TrustStore query, and its docstring already claimed to
*detect device erasure*. It had done all of that for months. The field report's
erased simulator got through anyway, because six call sites read
`cert-state.json` directly and bypassed every bit of it.

This happened **twice in one day** in this codebase: the same session had
`installed_plist_drift()` written specifically to catch a tunneld
configuration drift, with the caller never switched to it.

The lesson the refactor has to absorb: **a correct function that is easy not to
call is not a fix.** Every acceptance criterion in §4 that sounds like
over-engineering — exhaustive dispatch, no read that writes, a claim that
carries its own basis — is chosen because it makes the wrong call site fail
loudly instead of quietly returning a plausible boolean.

## 3. Where we want to land

### 3.0 Invariants the refactor must not break

These are settled, and are not open questions. Three of them were nearly
traded away during the week of fixes.

**I1 — `auto_install_cert: false` means false, always.** It is tempting to
treat a prior recorded install as standing consent and silently reinstall
after an erase. Rejected, and the reasons are already written down in the
project's own words:

- `CLAUDE.md`: *"Never auto-configure the system proxy. Never leave it
  configured when not actively testing."* Installing a MITM root CA is a
  **larger** commitment than a system-proxy toggle, not a smaller one — it
  persists across sessions and outlives the capture window.
- `CONTRIBUTING.md`: *"a silent, persistent CA-install policy would be worse
  than the failure it prevents"*, and offering only "install the certificate"
  *"railroads every user into trusting a CA"*.
- An erase is a deliberate reset, and wiping a device is sometimes exactly how
  you test a clean-device flow. Consent was given for the device as it was.

**I2 — The refusal keeps offering every resolution.** Install, set
`auto_install_cert`, or `skip_cert_check`. A refusal that names only the first
is the railroading I1 forbids.

**I3 — Disabling capture is never refused.** Refusing to *stop* capturing
because of a cert traps someone in the state they are trying to leave.

**I4 — The preflight belongs at the routing boundary, not at proxy start.**
Starting the proxy only binds a listener; nothing routes through it until
`configure_system` or `set_local_capture` is called. That is why there are
exactly two gated entry points and not three, and it is the test for whether a
future entry point needs one: *does it cause a device's traffic to traverse the
proxy?*

### 3.0.1 The two patterns already in the tree that are right

Neither is exotic; the refactor is largely "apply these everywhere".

**`_verify_physical_device` returns the stored value *with* a status saying it
could not be confirmed.** This is the only place in the codebase that
distinguishes "not trusted" from "not determinable", and it is the seed of
`trusted: bool | None`:

| Situation | Reports | Status |
|---|---|---|
| No proxy config recorded | `false` | `proxy_not_configured` |
| No client IP | stored value | `unverified_no_client_ip` |
| Proxy not running | stored value | `unverified_proxy_not_running` |
| No flows found from the recorded IP | stored value | `unverified_no_traffic` |
| Flows found | `true` | `verified_via_traffic` |

**`quern setup` is the reference implementation of the install path.** It is the
only caller that gets all three right at once: it scopes to *booted* simulators,
it verifies against the TrustStore rather than the record (`verify=True`), and
it **asks before installing**. Every runtime path should be able to say the
same.

### 3.1 Replace the boolean with a claim

```python
class TrustClaim:
    trusted: bool | None          # None == not determinable right now
    basis: Basis                  # how this answer was reached
    as_of: datetime               # when THAT basis was established, not now()
    method: Method                # truststore | adb | traffic | none
    fingerprint: str | None       # WHICH CA this claim is about
    blocked_by: Reason | None     # why `trusted is None`
```

- **`trusted: None` is the headline change.** `unverified_no_traffic`,
  `unverified_no_client_ip`, `unverified_proxy_not_running`, "shutdown
  simulator", "adb unavailable" are all *this*, and today they are all `False`
  or a stale `True` depending on which function you happened to call.
- **`as_of` is the timestamp of the basis**, never `now()`. Fixes D7 and makes
  staleness computable by the caller rather than by a flag someone remembered
  to set.
- **`fingerprint` makes the claim CA-specific.** A claim about a CA that is no
  longer ours is not a claim. Fixes D6 structurally.
- **`basis`** distinguishes `verified` / `recorded` / `inferred`, so Q2 cannot
  be mistaken for Q3 by any caller (D13).

### 3.2 One dispatcher, four verifiers, no fallthrough

```python
async def verify_trust(controller, device: DeviceInfo, *, allow_record: bool) -> TrustClaim
```

- Dispatches on `device.device_type` **exhaustively**; an unhandled kind raises
  rather than defaulting to the simulator path. D5 and D10 both come from a
  default branch.
- `allow_record=False` is Q1/Q3 — never consults `cert-state.json`.
- `allow_record=True` is Q2 — returns `basis=recorded` and never claims
  otherwise.
- **Never writes.** Persisting is a separate, explicit `record_claim(...)`
  call. Fixes D8.

### 3.3 The gate asks one question

```python
async def devices_that_would_fail_capture(controller) -> list[CaptureRisk]
```

- Covers **every** booted device kind, not just simulators (D9).
- Distinguishes three outcomes, because they need different handling:
  `will_fail` (refuse / auto-install), `cannot_determine` (warn, proceed),
  `will_work`.
- **Fails loud, not open.** A preflight that cannot run must say so and be
  visible in `proxy_status`, not return an empty list indistinguishable from
  "all clear" (D11).

### 3.4 `None` is a question to resolve, not a state to route around

**The decision that reorganises this document.** The earlier framing asked
whether `trusted: None` should block capture, and both answers are bad: block
and every physical-device session starts with a refusal, do not block and the
kind with the most failure modes gets no protection at all.

Both answers assume the device's silence is a fact we must accept. It is not.
**Make the device talk.** Ask it to load a page, watch what arrives, and read
the layer that broke:

| What the proxy sees | Layer | Meaning | Next step |
|---|---|---|---|
| no connection at all | L1 | traffic is not reaching us | WiFi proxy host/port wrong, or device off this network — reconfigure and re-probe |
| connection, client aborts the TLS handshake | L2 | device does not trust the CA | install it (`tls_failed_client` gives the exact moment and client IP) |
| handshake completes, flow decrypts | L2 ✓ | trusted, right now, proven | none |
| handshake completes for the probe, app's own hosts fail | L3 | the *app* is pinning | bypass that host or use a build without pinning |

This turns `unverified_no_traffic` from a shrug into a diagnosis, and it makes
D12 (#150) moot rather than fixed: there is no longer any need to *infer*
whether a recorded IP is on a network the device left, because the probe finds
out.

**The probe must actually be attributable**, which imposes three constraints
worth stating because two of them are already violated by the obvious
implementation:

1. **The target cannot be `quern.dev`.** `ALWAYS_BYPASS = ("quern.dev",
   "*.quern.dev")` passes it through untouched by design, so a successful load
   proves L1 only and says nothing about L2. Use a host reserved for exactly
   this purpose — `example.com` is IANA-reserved for documentation and testing
   — and never a third party's production service.
2. **`open_url` does not work on physical iOS devices.**
   `controller.open_url` calls `_require_simulator`; it is simulator and
   Android only. For a physical device the mechanism is `xcrun devicectl device
   process launch --payload-url <url>`, which exists and needs no WDA build.
   This is the difference between the probe being feasible and needing the
   heavyweight automation stack.
3. **iOS already probes on its own.** The OS hits `captive.apple.com`
   constantly for captive-portal detection. A device that is routing to us at
   all will produce that traffic without being asked, which makes it a free
   passive L1 signal before any deliberate probe is issued.

**Where the probe fires.** By I4 a phone's routing boundary is the WiFi proxy
setting the user types in by hand — which quern does not perform and cannot
gate. What it can do is verify immediately afterwards:
`record_device_proxy_config` **triggers a probe** rather than gating anything.
The user has just done the multi-step configuration; the call that records it
is the natural place to say whether it worked and, if not, at which layer.

**Consequence for the gate.** `trusted: None` does not block. It does not need
to, because a device that can be probed does not stay `None` for long, and one
that cannot be probed is reported with the reason. Refusal stays what it always
was: the answer for devices whose ground truth is cheap and available *before*
traffic, which is simulators.

### 3.5 `tls_failed_client` is the missing observation

mitmproxy 12.2.3 exposes `tls_failed_client`, carrying `conn.peername` and the
SNI host. Quern's addon implements `tls_clienthello`, `request`, `response`,
`error` and `client_disconnected`, and **not** this one — so the single most
direct evidence of the failure this whole document is about is discarded at the
moment it occurs.

It is not primarily a warning to display. **It is an input to "what do I do
next".** A rejected handshake tells you the device, the time, the host, and the
layer, which is everything needed to name the next configuration step instead
of reporting a state and leaving the reader to infer one. It also closes D14:
erase detection stops being an inference from a file timestamp and becomes an
observed event.

It is ground truth of the strongest kind available for a physical device — a
client that rejects our certificate has *demonstrated* it does not trust it —
so it writes back to the claim with `basis=verified`.

### 3.6 Kind-specific staleness, named as such

Replace the single `wifi_proxy_stale` with per-kind invalidation signals whose
names say which kind they apply to, so a reader is never asked to work out
whether a simulator can be affected by a WiFi change (it cannot).

**Build on `network_monitor`, do not duplicate it.** It already polls SSID and
local IP every ~15 s and reports `last_change_reason` (`ssid_changed`,
`ip_changed_same_ssid`, `ssid_and_ip_changed`). That is the invalidation *event*
for the physical-device kind and for no other; what is missing is the link from
the event to the affected devices' claims, and — now that §3.4 exists — a
re-probe triggered by it.

### 3.7 Non-goals

- Not changing `cert-state.json`'s on-disk shape in the same change. Add
  fields; migrate readers; remove the boolean last.
- Not making physical-device verification synchronous or reliable. It is
  traffic-based by nature; the goal is to *say so*, not to fix it.
- Not touching `install_cert`'s behaviour, only its interaction with the
  record.

## 4. Acceptance criteria

1. No function that reads trust also writes it.
2. `trusted is None` is reachable and distinguishable for every kind.
3. The preflight gate covers every booted device kind, and its failure to run
   is visible in `proxy_status`.
4. A claim carries the fingerprint of the CA it is about; a CA regeneration
   invalidates every outstanding claim with no TTL involved.
5. `as_of` never records a time at which nothing was checked.
6. Adding a fifth device kind produces a compile/type error or an explicit
   raise, not a silently wrong simulator answer.
7. Every current caller is explicitly re-pointed at Q1, Q2, Q3 or Q4 — the
   audit in D13 is done once and encoded, not repeated.
8. The refusal distinguishes a device that never trusted the CA from one that
   trusted it and was erased, and says when (D15).
9. `auto_install_cert: false` is honoured on every path, with no exception for
   prior recorded consent (I1).
10. A new capture-enabling entry point cannot be added without gating — the
    gate is a property of the routing boundary, not a call each endpoint
    remembers to make (I4, D1).
11. A device that can be probed never stays `None`. `None` is reported with a
    reason and a next step, never as a bare unknown (§3.4).
12. **The whole matrix executes against real hardware**, not a mock of the
    thing under test, on a setup that fails loudly when incomplete (§4.5.1).
13. **The interpretive prose shrinks.** `proxy_status`'s MCP description is
    **56 lines** today, mentioning `client_ip` 6×, `record_device_proxy_config`
    6×, `wifi_proxy_stale` 4× and DHCP 3× — all of it teaching an agent to
    reconstruct one meaning from a boolean plus five side-channel fields. That
    prose is the real measure of the current model's complexity, already paid
    for in tokens on every session. **If a design does not make most of those
    lines deletable, it has not resolved anything** — it has moved the
    reasoning rather than removing it. This is the most honest single test of
    the refactor, and the easiest to check.

Criterion 12 also settles a question `verification-apps.md` left open: the
fixture apps are for **regression detection**, not demonstration, and a pinning
surface in `QuernProbe` is the concrete next item that unblocks row 62.

### 4.5 Migration sequence

Deliberately ordered so each step is independently landable and independently
revertable, and so the risky shared-file change comes last.

| Phase | Change | Lands behind | Unblocks |
|---|---|---|---|
| **1** | `tls_failed_client` in the addon; record the event with client IP, SNI, timestamp | nothing — pure addition, no reader changes | the observation §3.5 needs; the third oracle in §4.5.1 |
| **2** | Delete the trust cache (ADR 1); closes #151 by removal | nothing | one fewer way to get a stale answer |
| **3** | `TrustClaim` as an internal type; `verify_trust` dispatcher; **no API change** | existing fields kept, populated from the claim | callers migrate one at a time |
| **4** | Re-point the ~7 callers, one per commit, each with its Q1/Q2/Q3/Q4 label | phase 3 | D13 encoded rather than re-audited |
| **5** | The probe (§3.4) + `record_device_proxy_config` triggering it | phase 1 | D12/#150 becomes moot; physical devices get real answers; rows 57–68 become runnable |
| **6** | API surface: `TrustClaim` on the wire; rewrite the 56 lines | phases 3–5 | criterion 12 |
| **7** | Remove `cert_installed` from `cert-state.json`; migrate the file | everything above | the shape is gone |

Phases 1 and 2 are worth doing whether or not the rest happens. Phase 6 is the
one that touches the menu bar and the MCP layer, and is the only one that
should need coordination.

#### 4.5.1 The verification suite runs against real devices, driven by quern

**Settled 2026-09-13: the suite may require a real working setup.** That
removes the constraint the previous draft was written around. There is no
fallback ladder of fixtures and approximations — the rows that need a phone are
*required* to run against a phone, and a run without one fails rather than
quietly passes.

This matters more here than almost anywhere else in the codebase, because
`verification-apps.md` already made the argument with evidence: **nothing in CI
has ever executed a quern tool against a real device**, and #78 is what that
costs — `am start` exits 0 when it cannot resolve an intent, so `open_url`
returned `{"status": "ok"}` for a URL nothing could open, byte-for-byte
identical to success, for the entire life of Android support. No amount of
reading found it; booting an emulator found it in four minutes. Every defect in
§2.2 that is still live sits in the same blind spot.

**Use the marker that exists.** `pyproject.toml` already declares
`integration: tests that require real devices/simulators and external tools`
and already deselects it by default (`addopts = "-m 'not integration'"`, 16
tests today, `tests/test_cert_integration.py` among them). This is an extension
of that suite, not a new mechanism.

**Drive it through quern's public surface, not its internals.** The existing
cert integration tests call `cert_manager` directly. The verification suite
should go through the HTTP/MCP API, because **that is the surface all three
field reports came through.** Every live defect in §2.2 is reachable from the
API and invisible from the internals — D10's contradictory `cert_installed:
true` + `status: never_booted` pair only exists in the response body. Dogfooding
is not a nicety here; it is the only place several of these are visible.

##### The oracle problem, and three answers

Quern driving a test of quern means a bug can mask itself. Every assertion
about trust must therefore resolve to something **quern does not author**:

1. **The TrustStore SQLite file, read directly.** This is what made today's
   live run conclusive: the CA was proven installed by a sha256 match against
   `~/.mitmproxy/mitmproxy-ca-cert.pem`, not by quern reporting success. Quern
   had in fact reported success once already while nothing had been installed.
2. **The device's own screen.** A rendered page versus a TLS interstitial.
   Quern captures the screenshot; the device decides what to draw. This is the
   only oracle that works for a physical device with no other instrumentation,
   and it is the direct observable behind the §3.4 probe.
3. **`tls_failed_client` from mitmproxy**, which is upstream of quern's
   bookkeeping entirely.

**Rule: no trust assertion resolves to a quern field alone.** A test that
checks `proxy_status` agrees with `proxy_status` is the mutation-survival
failure from §5.7 wearing hardware.

##### The setup manifest

Written down so it is reproducible, and asserted by name at session start:

| Requirement | Why | Destructive? |
|---|---|---|
| A **scratch simulator** the suite owns | erased repeatedly for D3/D20 | yes — must never be the active device |
| One **physical iOS device** on the same network, WiFi proxy configured, cert installed | rows 8–11, 22–25, 57–61; nothing else can produce them | no |
| An **Android device or emulator** | rows 12–14, 18, 26, 34; D9 and D10 live here | wipe only on an emulator |
| Proxy running; **system proxy not configured** | standing project constraint — never leave it configured when not actively testing | n/a |
| A scratch CA the suite may regenerate | row 21 / #151 | yes — must not be the working `~/.mitmproxy` |

**Preconditions fail, they do not skip.** A `skipif` on "no phone present"
recreates exactly the gap this suite exists to close: a green run that proved
nothing about the region where the bugs are. The suite reports which
requirement is missing and exits non-zero.

##### What still needs a human, and the tricks that remove most of it

| Scenario | Automatable? |
|---|---|
| Simulator loses trust (L2) | **yes** — `simctl erase`, verified live today |
| Every device loses trust at once (L2) | **yes** — regenerate the scratch CA. This is the automatable form of "a phone no longer trusts us", with no hands on the phone |
| Traffic stops reaching the proxy (L1) | **yes** — stop the proxy, or move its listen port. The recorded host stops answering without touching the device |
| Phone leaves the network | **no** — manual, or accept the proxy-stop equivalent above |
| Removing a CA profile from a phone | **no** — manual; the CA-regeneration trick covers the same assertion |
| App-level pinning (L3) | **not yet** — needs a pinning surface in `QuernProbe`, which is a natural addition to the web-view tab already proposed in `verification-apps.md` §4 |

So one human step remains in the common case: install the CA and set the WiFi
proxy on the phone **once**, at setup. Everything after that is driven.

##### Where it runs

A **self-hosted macOS runner**, which `verification-apps.md` already proposes
(§5 of its proposed work) and which matches this project's standing preference
for self-hosted CI. A hosted cloud runner cannot have a phone plugged into it,
so this is not a preference here but a requirement.

## 5. Test case matrix

68 rows, which between them would have caught all of §2. Grouped by what
varies. **Every row marked ⚠ is a case that passes today and should not, or
fails today and should not.** Rows marked ✓ verified live were run against real
booted simulators on 2026-09-13, not reasoned about.

**Execution policy (§4.5.1):** rows needing a real device are `integration`,
run against the setup manifest, and **fail rather than skip** when it is absent.
Nothing in this matrix is approximated by a mock of the thing it is testing —
that is how four of these defects survived a passing suite.

### 5.1 Ground truth vs. record (all kinds)

| # | Kind | Record | Age | Ground truth | Expected `trusted` | Expected `basis` | Today | Cites |
|---|---|---|---|---|---|---|---|---|
| 1 | simulator | absent | — | in TrustStore | `True` | verified | ok | |
| 2 | simulator | absent | — | not in TrustStore | `False` | verified | ok | |
| 3 | simulator | `true` | 3 min | **erased** | `False` | verified | ⚠ was `True` | D3 |
| 4 | simulator | `true` | 2 h | erased | `False` | verified | ok | D2 |
| 5 | simulator | `false` | 1 min | installed since | `True` | verified | ⚠ `False` on cached paths | D3 |
| 6 | simulator | `true`, fp=**old CA** | 1 min | old CA present, current CA absent | `False` | verified | ⚠ returns `True` | **D6** |
| 7 | simulator | `true`, fp=current | 1 min | current CA present | `True` | verified | ok | |
| 8 | iOS physical | `true` | any | traffic decrypting | `True` | verified(traffic) | ok | |
| 9 | iOS physical | `true` | any | **no traffic yet** | `None` | blocked: no_traffic | ⚠ returns record as fact | D12 |
| 10 | iOS physical | `true` | any | no traffic, host IP changed | `None` | blocked: config_unreachable | ⚠ indistinguishable from #9 | **D12** |
| 11 | iOS physical | `true` | any | queried via simulator path | `True` | — | ⚠ returns `False` **and persists it** | **D5/D8** |
| 12 | android emulator | absent | — | cert in system store | `True` | verified(adb) | ok | |
| 13 | android emulator | `true` | any | adb unavailable | `None` | blocked: adb_unavailable | ⚠ returns `False` | D10 |
| 14 | android physical | `true` | any | device unrooted | `None` | blocked: not_rootable | ⚠ returns `False` | D10 |

### 5.2 Device state

| # | Kind | State | Expected | Today | Cites |
|---|---|---|---|---|---|
| 15 | simulator | shutdown | `None`, blocked: not_booted | returns record as fact | D13 |
| 16 | simulator | booting | `None`, blocked: not_booted | undefined | |
| 17 | simulator | never booted (no trustd dir) | `False`, verified | `never_booted` via a different function only | D10 |
| 18 | android | unauthorized | `None`, blocked: adb_unavailable | `False` | D10 |
| 19 | any | device absent from `list_devices` | `None`, blocked: unknown_device | record returned | D13 |

### 5.3 Invalidating events

| # | Kind | Event | Expected after | Today | Cites |
|---|---|---|---|---|---|
| 20 | simulator | `simctl erase`, then re-check within 1 min | `False` | ⚠ `True` | **D3** |
| 21 | simulator | CA regenerated | `False` for every device | ⚠ `True` for 1 h | **D6** |
| 22 | iOS physical | joined a different WiFi network | `None`, blocked: config_unreachable | `True`, unchanged | D12 |
| 23 | iOS physical | host changed IP on same network | `None`, blocked: config_unreachable | `wifi_proxy_stale` only, boolean unchanged | D12 |
| 24 | simulator | host changed WiFi | unchanged — irrelevant for simulators | unchanged (correct, but by accident) | §2.3 |
| 25 | iOS physical | profile removed by the user | `None` until traffic proves otherwise | `True` indefinitely | D12 |
| 26 | android | factory reset | `False` | `True` for 1 h | D6-shaped |

### 5.4 The capture gate (Q1)

| # | Setup | Call | Expected | Today | Cites |
|---|---|---|---|---|---|
| 27 | untrusting booted sim, `auto_install_cert` off | `POST /proxy/configure-system` | **428** + resolutions | ok ✓ verified live | |
| 28 | untrusting booted sim, `auto_install_cert` off | `POST /proxy/local-capture` w/ processes | **428** | ✓ fixed `525d9e0`, verified live | D1 |
| 29 | untrusting booted sim | `POST /proxy/local-capture` `processes: []` | **200** — disabling is never refused | ✓ verified live | |
| 30 | untrusting booted sim | either, `skip_cert_check: true` | **200**, nothing installed | ✓ verified live | |
| 31 | untrusting booted sim, `auto_install_cert` on | either | **200** + CA genuinely in TrustStore | ✓ verified live (sha256 match) | |
| 32 | sim erased 3 min ago, record says installed | `POST /proxy/local-capture` | **428** | ⚠ was **200** — now fixed | **D3** |
| 33 | `auto_install_cert` on, install fails | either | **500**, not a silent proceed | ok | |
| 34 | untrusting booted **android emulator** | either | **428** | ⚠ **200** — not covered | **D9** |
| 35 | trusting sim + untrusting phone | either | phone reported as `cannot_determine`, not refused | phone not considered at all | D9/§2.3 |
| 36 | preflight itself raises | either | proceed, **and surface that it could not run** | proceeds silently | **D11** |
| 37 | no booted devices | either | **200** | ok | |
| 37a | sim with **no record at all**, untrusting | either | 428 whose text says *never trusted* | one generic message | **D15** |
| 37b | sim with `installed_at` set, since erased | either | 428 whose text says *trusted until erased at `<installed_at>`* | same generic message | **D15** |
| 37c | sim erased, `auto_install_cert` **off** | either | still **428** — a prior install is not standing consent | ✓ correct today | **I1** |
| 37d | proxy started but never routed | `start_proxy` alone | no gate, no refusal — binding a listener routes nothing | ✓ correct today | **I4** |
| 38 | CA file missing entirely | either | **200** with a distinct reason, not "trusted" | `False` for all → 428 | D6-adjacent |

### 5.5 Reporting (Q2) and the filter (Q4)

| # | Setup | Call | Expected | Today | Cites |
|---|---|---|---|---|---|
| 39 | record `true`, ground truth false, booted sim | `proxy_status` | `cert_trust_stale: true`, record preserved | ✓ fixed | D4 |
| 40 | record `false`, ground truth false | `proxy_status` | **not** flagged stale — nothing to be stale about | ✓ fixed | D4 |
| 41 | shutdown sim, record `true` | `proxy_status` | not flagged; absence ≠ verified | ✓ fixed | D4 |
| 42 | erased sim, record `true` | `list_devices(cert_installed=true)` | **excluded** | ✓ fixed | D5 |
| 43 | physical device, record `true` | `list_devices(cert_installed=true)` | **included** — record is the best answer available | ✓ fixed by scoping | **D5** |
| 44 | physical device, record `true` | `list_devices(cert_installed=true)` twice | record **unchanged** on disk after both calls | ⚠ the pre-fix path overwrote it | **D8** |
| 45 | android device, cert installed | `POST /cert/verify` | `status` consistent with `cert_installed` | ⚠ `true` + `never_booted` | **D10** |
| 46 | any | `GET /cert/status` | labelled as a record, with `as_of` | honest today, by prose only | D13 |
| 47 | entry with stale computed fields on disk | `proxy_status` | stripped and rebuilt, no 500 | ok | |

### 5.6 Persistence invariants

| # | Property | Today | Cites |
|---|---|---|---|
| 48 | No read path mutates `cert-state.json` | ⚠ violated by `is_cert_installed`, `get_device_cert_state` | **D8** |
| 49 | `as_of`/`verified_at` is only written by a path that actually checked | ⚠ violated | **D7** |
| 50 | Only canonical fields are persisted | ok | |
| 51 | Concurrent updates to two devices both survive | ok (`flock` + read-modify-write) | |
| 52 | `wifi_proxy_configs` survives a trust update for the same device | ⚠ `is_cert_installed`'s write drops it (not in the `DeviceCertState` it builds) | **D8** |

### 5.7 Test-construction rules (learned the hard way)

Four separate times this week a test re-implemented the decision it was
checking, or stubbed the seam under test, and mutations survived. The rules
that came out of it:

- **Patch below the decision, never at it.** For simulator trust the seam is
  `verify_cert_in_truststore`; for Android it is the `adb` call; for physical
  it is the flow store query. Patching `is_cert_installed` tests nothing.
- **Never pass the expected value into the fixture.** If the rig computes what
  the assertion checks, mutating the real code changes nothing.
- **Test doubles must carry every attribute the real path touches** — D11 means
  a missing one produces a *green* test for code that never ran.
- **Every gate test needs its converse**, or "report everything as missing"
  passes.
- **Row 52's shape is the general one:** assert what a write *preserves*, not
  only what it sets.

### 5.8 The probe and the layer model (§3.4)

New surface, so every row is "does not exist today".

| # | Setup | Probe result | Expected diagnosis | Covered by |
|---|---|---|---|---|
| 57 | phone, WiFi proxy host wrong | no connection reaches the proxy | **L1** — reconfigure host/port, re-probe | integration; stop the proxy to synthesize |
| 58 | phone, correct proxy, CA not installed | connection arrives, client aborts handshake | **L2** — install the CA; name the device and time from `tls_failed_client` | integration; regenerate the scratch CA, no hands on the phone |
| 59 | phone, correct proxy, CA installed | handshake completes, flow decrypts | trusted, `basis=verified`, `as_of`=now | integration, phone |
| 60 | phone left the recorded network | no connection | **L1**, *not* `unverified_no_traffic` | integration, **manual** — or accept row 57 as equivalent |
| 61 | phone idle but correctly set up | probe forces the answer | must **not** report `None` — the probe is the whole point | integration, phone |
| 62 | app pins, device trusts the CA | probe host succeeds, app's hosts fail | **L3** — pinning, not a cert problem | blocked on a pinning surface in `QuernProbe` |
| 62a | `www.apple.com` in Safari, CA trusted | client refuses the certificate | **must produce a trace** — host, client, time. Zero trace today | integration; free, no app needed (§1.1.1) |
| 62b | `support.apple.com` in the same session | decrypts normally | proves 62a is host-specific, not "Apple is blocked" | integration |
| 62c | `local_capture: ["MobileSafari"]` only, Safari driven | zero flows | **L1** — name the process traffic actually arrives from. Silent today. Not the default, but what the CLI help and both MCP examples describe | integration |
| 63 | probe target is `quern.dev` | succeeds regardless of CA trust | **must fail the test** — `ALWAYS_BYPASS` makes it prove only L1 | unit |
| 64 | physical iOS, probe requested | `--payload-url` used, not `open_url` | `open_url` raises for physical devices; the probe must not use it | unit |
| 65 | `record_device_proxy_config` called | probe fires, result returned with the call | the multi-step reconfiguration ends with an answer, not a recording | integration, phone |
| 66 | `network_monitor` reports `ssid_changed` | affected phones' claims re-probed or invalidated | simulators unaffected — the event is kind-specific | unit |
| 67 | iOS hits `captive.apple.com` unprompted | passive L1 confirmation, no probe issued | routing confirmed for free | integration, phone |
| 68 | `tls_failed_client` fires for any kind | claim → `trusted=False`, `basis=verified` | a rejection is proof, for every device kind | unit + integration |

## 6. Decisions taken, and what is still open

### 6.1 Settled — 2026-09-13

**Q: Should `trusted: None` block capture?**
**A: The question was wrong. Probe the device instead of accepting its
silence.** Both answers to the original question were bad — blocking taxes
every physical-device session with a refusal, not blocking leaves the kind with
the most failure modes unprotected. Asking the device to load a page resolves
the unknown in seconds and reports *which layer* failed, which is more useful
than either. §3.4. Consequences: `None` does not block; `record_device_proxy_config`
verifies rather than gates; #150 becomes moot rather than fixed.

**Q: Where should a detected CA rejection surface?**
**A: As an input to "what is the next configuration step", not as a status
line.** Consistent with the project's standing position that an error without
an instruction is not worth showing. §3.5.

**Q: Is certificate pinning in scope?**
**A: Yes — the model answers "will capture work", not "is the CA trusted".**
L3 is a real cause of the identical symptom, and the probe can separate it from
the other two (a pinned app fails *after* a successful handshake to other hosts
from the same device). §1.1.

### ADR 1 — Delete the trust cache rather than fix it

**Date:** 2026-09-14 · **Status:** accepted · **Supersedes:** the third open
question in §6.2 as originally written, and narrows #151.

#### Context

`is_cert_installed` kept an hour-long cache of each device's trust. **That hour
is a window in which an erased simulator reports as trusting the CA**, which is
the failure in §0: capture silently fails, and the symptom points at the app.
A field report lost an hour to it, and a live test here reproduced it three
minutes after `simctl erase`.

#151 records a second defect in the same branch: it computes
`expected_fingerprint` and never compares it to the recorded one, so
regenerating the CA leaves every device reporting trusted — for the old CA —
until the TTL expires.

The migration had phase 2 as "fingerprint-aware cache invalidation", which
would have fixed the second defect and left the first.

#### Why the cache is not worth its window

The case for keeping it was latency. It does not survive contact with the code,
and this is the supporting argument rather than the reason — a cache that made
erasure invisible would be worth deleting even if it were free.

1. **Android never reaches the cache.** `is_cert_installed` dispatches to
   `_is_cert_installed_android` and returns *before* the cache block. The one
   device kind whose verification is genuinely expensive cannot use it, so the
   stated justification was about a case that does not exist.
2. **The expensive call happens first regardless.** `get_cert_fingerprint`
   shells out to `openssl` and runs *above* the cache check, so a cache hit
   pays it anyway.
3. **The saving is about 1%**, and none of this is on a hot path: nothing polls
   it. The menu bar's three-second timer reads `state.json` off disk; no
   background loop touches cert state. These calls happen when an agent or the
   CLI asks — session start, enabling capture, `doctor`.

   | step | cost | |
   |---|---|---|
   | `get_cert_fingerprint` | 9.13 ms | runs before the cache check |
   | `read_cert_state_for_device` | 0.08 ms | the cache read itself |
   | `verify_cert_in_truststore` | 0.11 ms | what the cache skips |

4. **Nothing calls it.** After #152 and #153 every caller passes `verify=True`,
   including the single pass-through in `get_device_cert_state`. The branch is
   dead code in production, so #151 is currently unreachable — a latent
   landmine rather than a live bug.

#### Decision

**Delete the cache**, along with the `verify` parameter, `CACHE_TTL_SECONDS`,
and the cache-hit path. `is_cert_installed` always asks the TrustStore.

#### Why deletion beats the alternatives

Three other mitigations were considered and rank below it:

- *Fix the fingerprint comparison* (#151 as filed) — correct, but leaves a
  branch nothing uses, for a future caller to wander into.
- *Flip the default to `verify=True`* — makes the unsafe path opt-in, but
  leaves it reachable.
- *A test asserting every call site passes `verify=True`* — brittle, and guards
  the symptom rather than the cause.

Deletion is the only one a future caller cannot undo. It is also consistent
with §2.4: a correct function that is easy not to call is not a fix, and the
same is true of a correct branch that is easy to reach by accident.

#### Consequences

- **#151 is closed by removal**, not by a fingerprint check. The defect cannot
  recur because the code holding it is gone.
- **§3.1's `fingerprint` field is unaffected.** A claim still records which CA
  it is about; that is what makes CA regeneration expressible. This removes a
  *time-based* cache, not the identity of the thing being claimed.
- **The fingerprint comparison inside `verify_cert_in_truststore` is now the
  only CA-identity mechanism in this path**, and #151 is closed without one
  being added above it. It is pinned by a test asserting the *current* CA's
  fingerprint is what gets asked about; before that, passing a constant left
  the whole suite green.
- **If speed matters later, cache the fingerprint** — 9 ms, CA-scoped, with no
  per-device staleness to get wrong. That is a different and much smaller
  change, and none of this risk attaches to it.
- Phase 2 of §4.5 is now "delete the cache" rather than "#151".

### 6.2 Still open

1. **Does `TrustClaim` go on the wire, or stay internal behind a flattened
   view?** Phase 6 in §4.5. It is the only phase that touches the menu bar and
   the MCP layer, and acceptance criterion 12 (the 56 lines) cannot be met
   without it.
2. **Should the probe ever fire unprompted?** It is one HTTPS request to a
   reserved domain, but it is still traffic quern generates on someone's
   device. `record_device_proxy_config` is clearly consented. A probe on every
   `proxy_status` is clearly not. The boundary in between is unresolved.
3. **How much of L3 is worth modelling?** Detecting *that* an app pins is
   within reach via the probe. Doing anything about it is a different project.
