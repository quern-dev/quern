# Choosing a simulator input transport

**Status:** proposal, 2026-09-20. Prompted by #249, which asked whether DTUHID
is worth adopting and whether Indigo can ever be retired.

Quern drives simulator input through Indigo's legacy HID services. Apple has
moved: modern hosts ship `dtuhidd`, a guest daemon that receives HID events
over the wire, and Xcode's Device Hub attaches it to every booted simulator —
at which point the guest disconnects the very services quern uses. That is the
bug behind #234, #233 and #231, and the repair quern ships today (clear a
notify state, restart backboardd) is a stopgap that costs a SpringBoard
restart.

This describes how quern decides which transport to use, so that both can
exist and neither is guessed at.

## What is actually true

Measured on 2026-09-20, macOS 26, Xcode 27.0, CoreSimulator 1171.7. These are
the facts the design rests on; if one is wrong the design is wrong.

**`dtuhidd` ships with the host, not the runtime.** Its binary lives in
CoreSimulator and its launchd plist is written into each device's data
directory at boot:

```
$ xcrun simctl spawn <udid> launchctl print system/com.apple.coredevice.dtuhidd
  path    = .../Devices/<udid>/data/Library/LaunchDaemons/com.apple.coredevice.dtuhidd.plist
  program = /Library/Developer/PrivateFrameworks/CoreSimulator.framework/
            Resources/Platforms/iphoneos/usr/libexec/dtuhidd
```

**So availability follows the host, not the guest.** `dtuhidd` runs inside an
iOS **18.6** simulator on this machine, and that simulator reports
`com.apple.coredevice.dtuhidd.active = 1` — the same suppression as iOS 27.
A runtime version tells you nothing about which transport is available.

**Indigo cannot be retired.** facebook/idb puts the handover at CoreSimulator
**1155.4**. A host below that has no `dtuhidd` at all, and those users are
precisely the ones who never see the suppression — a DTUHID-only quern would
break working installs to fix a bug they do not have.

**The transport carries the primitives we are missing.** `dtuhidd` imports
`IOHIDEventCreateDigitizerEvent`, `IOHIDEventCreateKeyboardEvent`,
`IOHIDEventCreateFluidTouchGestureEvent` and
`IOHIDEventCreateRelativePointerEvent` — touch, keyboard, gestures and a
relative pointer, which is the scroll-wheel primitive we lack (#243). Its man
page describes it as a "DT Remote service for receiving and posting
UniversalHID events", so the host side is CoreDevice/RemoteXPC rather than
anything in SimulatorKit — `nm` finds no DTUHID symbols in CoreSimulator or
SimulatorKit, which is consistent.

## What baguette does, and why it is the better reference

**Correction, same day.** An earlier revision of this section claimed baguette
"does not use DTUHID at all" and "has no Device Hub reclaim". Both were read
off a local checkout pinned at v0.1.70 (2026-05-11), 462 commits behind. The
current tree (v0.1.99, 2026-09-19) has `DeviceHubAttachment.swift`,
`SimctlInputSurface.swift` and a `docs/features/device-hub.md` devoted to
exactly this problem. The conclusion below survives; the evidence for it is
different, and better.

### Where it lands on DTUHID

Baguette's **input transport is still Indigo**, and it says so deliberately:

> **Not a change to how input is sent.** baguette still speaks the legacy
> Indigo port. Device Hub itself uses CoreDevice + `UniversalHID.framework`,
> which reaches dtuhidd's `IndigoHIDServer` over guest XPC
> (`com.apple.coredevice.feature.remote.hid.digitizer` and siblings).
> Adopting that transport is the durable fix — Apple is clearly retiring the
> legacy services — but it is a separate reverse-engineering effort; Xcode
> 27's SimulatorKit still ships only `SimDeviceLegacyHIDClient`.

That is the most informed answer available to #249's question, and it matches
the one this document reaches: DTUHID is where Apple is going, it is not a
weekend, and Indigo carries the work until someone does it. It also hands us
the concrete lead open question 2 was missing — the transport is guest XPC to
`com.apple.coredevice.feature.remote.hid.*`, not anything in SimulatorKit.

### Where the two projects already agree

Quern's `server/device/sim_input.py` and baguette's `SimctlInputSurface`
converged, independently, on the same repair:

| | quern | baguette |
|---|---|---|
| detection key `com.apple.coredevice.dtuhidd.active` | yes | yes |
| clear the notify state, **then** restart `backboardd` | yes | yes |
| documents that the reverse order re-kills the services | yes | yes |
| waits for SpringBoard to return | yes | yes |
| is Device Hub running on the host | yes | yes |
| explicit heal for a device we did not boot | `restore_simulator_input` | `baguette heal --udid` |
| **the advisory reaches the caller** | **added here** | server log only |

Two independent implementations agreeing on an undocumented ordering
constraint is the strongest confirmation this repair is right that we are
going to get.

An earlier revision of this table claimed the on-demand heal was #249 Tier 1
and that we lacked it. Wrong twice over: `restore_simulator_input` and
`POST /api/v1/device/ui/restore-input` already exist, and the tool description
already names the case — "a simulator that was already booted, typically one
booted while Xcode or its Device Hub was open". Having just been caught
reading a four-month-old copy of baguette, the lesson repeated itself
immediately in the other direction: read your own tree before recording a gap
in it.

The real gap was the last row. `_warn_if_input_is_suppressed` wrote the
advisory to `logger.warning`, which the caller never sees. An agent driving
quern over MCP got `{"status": "ok"}` back from a tap that was accepted and
discarded — the exact bug the warning exists to catch, delivered to the one
audience that could not act on it. The write endpoints now carry it on the
response.

Two of their measurements are worth having:

- **`bootstatus` returns too early to trust.** On a warm boot it returns
  ~1.3 s after `boot`, while dtuhidd publishes its state ~2 s in. Their boot
  path blocks on `simctl bootstatus -b`, then — only if Device Hub is running
  on the host — waits up to ten seconds for the state to appear before
  concluding there is nothing to heal.
- **Prevention does not work.** SimulatorHID reacts to state *changes*, so
  forcing the key to `0` during boot still yields the fatal
  connect→disconnect once dtuhidd flips it. Heal after the fact.

### Where the input layers differ

Quern already implements baguette's digitizer recipe — the `IOHIDEvent`
parent + finger child through
`IndigoHIDMessageForTrackpadEventFromHIDEventRef`, then the same two byte
slots: target `0x32` at `0x6c`/`0x10c`, edge bitmask at `0x3a`/`0x3b` and
`0xda`/`0xdb`. Same offsets, reached independently.

Resolved symbols, checked against v0.1.99:

| | quern | baguette |
|---|---|---|
| `…ForTrackpadEventFromHIDEventRef` | yes | yes |
| `…ForButton`, `…ForHIDArbitrary` | yes | yes |
| `…ForKeyboardArbitrary`, `…ForModifierKeyBit` | yes | — |
| `…ForScrollEvent` | — | yes |
| `…ForMouseNSEvent`, CarPlay service, remove-pointer | — | yes |
| edge exposed on the wire | **—** | `edge:` on tap/touch1 |

That last row is the actionable one. `wrapAndPatch(event:edgeBit:)` and
`sendDigitizerEvent(… edgeBit:)` already carry an edge, default `0`, and
`doSwipe` does not accept one — so the byte-patching that turns a swipe into
a system gesture is written and unreachable. Baguette exposes `edge` as a
first-class wire field and has since before our pinned commit, which is good
reason to think the offsets are right.

The same holds one step on: `doTap(… hold:)` exists *and* the bridge already
parses `hold` off the wire (`dict["hold"] as? Double ?? 0.05`). The Python
client never sends it. Long press is implemented and unexposed.

Neither is verified to work — the offsets are written and no test drives
them. What changed is the estimate, not the evidence.

### What this does to the plan

It removes DTUHID from the critical path for **primitives**. Scroll needs one
more `dlsym`; edge gestures and long press need plumbing connected, not
written; pinch, pan and rotate compose from events quern can already build.
Baguette reaches all of that over Indigo.

So the order is: **expand Indigo, using baguette as the reference**, keep the
reclaim, add the on-demand heal, and treat DTUHID as the durable fix that
neither project has yet paid for.

## The proposal

Read the host's CoreSimulator version once at server start, record it in
`~/.quern/state.json`, and let every path that writes input consult it.

That is the right spine, for three reasons. It is one detection point rather
than a probe per call; it is **observable**, so a bug report carries the number
that decides the behaviour instead of leaving us to ask; and it puts the choice
in one place, which is what lets a second transport exist at all.

Three refinements, each from something that went wrong before.

### 1. The version selects a *preference*; the device confirms it

The version answers "could this host have `dtuhidd`". It does not answer "does
this device have it". A simulator booted before an Xcode switch keeps the
daemon set it booted with, and `xcode-select` can change under a running
server.

Worth being clear about what is and is not already solved here: quern
**already has** exact per-device detection. `legacy_input_is_suppressed`
reads `com.apple.coredevice.dtuhidd.active` off the guest in one round trip
(~300 ms), and baguette independently settled on the same key as its whole
signal. So the "is input suppressed on this device" question needs no version
number at all. What the CoreSimulator version would add is *transport
selection* — a different question, and one with no second transport to select
between yet. That is an argument for recording the version now and deferring
the selection logic until there is something to select.

So: host version chooses the preferred transport, and the first input call to a
device confirms it against that device, caching the answer per udid. This is
the shape `_sim_bridge_ok` already has — and #236 is the warning attached to
it: that flag initialises `False`, is never refreshed, and nothing tests the
selection, so a mutation that routes every simulator to idb passes the suite.
Whatever this design records, the selection needs tests that fail when it is
wrong.

### 2. A recorded version is a record, not a fact

Twice today a stored value that nobody rechecked produced confident wrong
behaviour: a per-model screen-size table that was wrong for every model in it
(#210), and hardcoded tap coordinates for an app that had moved (#239). Both
were read at a point where asking the device was cheap.

A version read at start is stale the moment someone runs `xcode-select`. The
mitigation is not to re-read constantly — it is to **record what the value was
read from** and re-read when that changes:

```json
"simulator_input": {
  "core_simulator_version": "1171.7",
  "developer_dir": "/Applications/Xcode.app/Contents/Developer",
  "read_at": "2026-09-20T15:04:11Z",
  "preferred_transport": "dtuhid"
}
```

`developer_dir` is the thing that decides which CoreSimulator is in play, so a
cheap comparison of it (one `xcode-select -p`) tells us whether the recorded
version still describes reality. Re-read on device boot is enough; nobody
switches Xcode mid-tap.

### 3. Failure has to be loud, and must not silently fall back

If the preferred transport fails, falling back quietly means a machine that
appears to work while doing something other than what was chosen — and the
whole class of bug this project keeps finding is an operation reporting success
for something that did not happen. So: one re-probe on a transport-unavailable
error, then switch with a warning that names both transports and the reason.
Never a per-call silent retry, which would hide a transport that is broken for
everyone.

**The current write is never replayed.** Input is not idempotent. A transport
error can arrive after `tap`, `type`, `swipe` or `button` was already handed
to the guest and may have been delivered, so retrying it on the other
transport risks applying it twice — a double tap where one was asked for,
which is worse than the failure it was trying to paper over.

So the rule has three parts, and the middle one is the one that is easy to
leave out:

1. The failing call **fails**, explicitly, saying the write may or may not
   have landed. Not "failed", which claims it did not.
2. The transport selection switches for **subsequent** calls.
3. The caller decides whether to repeat it, because only the caller knows
   whether the operation is safe to repeat.

This matches what `restore_legacy_input` already does with a `_spawn` timeout:
it treats the write as possibly-applied and puts the state back rather than
assuming it did nothing. `_TIMED_OUT` exists as a value distinct from failure
for exactly this reason — "the command may have taken effect before it was
killed" — and the same distinction belongs here.

## What this unlocks

- **#249 Tier 1**, repairing a simulator quern did not boot, becomes
  transport-aware: on a DTUHID host the repair is not needed at all, because
  the suppression only matters to Indigo.
- **#243's primitives** are no longer blocked on this at all — see the
  baguette section: they are reachable on Indigo today. What the selection
  buys them is that if DTUHID is ever added, a primitive has one place to be
  implemented twice rather than two call sites to discover. A primitive that
  exists only on new hosts would be a worse outcome than not having it.
- **Diagnostics** gain the number that explains behaviour. `quern doctor` can
  say which transport a device is using and why, which is the question nobody
  can currently answer without reading source.

## Open questions

1. **Which Xcode first shipped CoreSimulator 1155.4?** idb's threshold is
   inherited, not verified here, and it sizes how long Indigo must be kept.
   Only one Xcode is installed on this machine, so it cannot be measured here.
2. **What does the host side of DTUHID look like in practice?** Partly
   answered: baguette's reverse engineering puts it at guest XPC to
   `com.apple.coredevice.feature.remote.hid.digitizer` and siblings, reached
   by Device Hub through CoreDevice + `UniversalHID.framework`, with
   SimulatorKit still shipping only `SimDeviceLegacyHIDClient`. What remains
   unknown is the host-side client — idb is the only implementation to read,
   and baguette, having gone this far, still calls adopting it "a separate
   reverse-engineering effort".
3. **Does DTUHID cover buttons and hardware keyboard**, or only the event types
   the daemon imports? Quern's `press_button` and `type_text` both go through
   Indigo services today, and a transport that covers touch but not buttons
   would leave a split.
4. **What happens on a host that has `dtuhidd` but where Device Hub has never
   run?** Measured once (2026-09-18): a simulator booted without Device Hub
   kept working after Device Hub launched. So the daemon existing is not the
   same as it holding the services, and the preference may need to account for
   that rather than assuming a modern host means DTUHID.

## What this does not propose

Implementing DTUHID. That is the larger piece of work behind this, and it
should not start until (1) and (2) above are answered — the point of this
document is that the *decision* has a home, so the second transport can be
added without rewriting how the first one is chosen.
