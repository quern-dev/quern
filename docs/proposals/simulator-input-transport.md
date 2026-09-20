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

`tddworks/baguette` solves the same problem for the same simulators, and
**does not use DTUHID at all**. Its entire input layer is two files,
`IndigoHIDInput.swift` and `IOHIDDigitizerDispatch.swift`, and nothing in the
tree mentions `dtuhid`, `UniversalHID` or CoreDevice.

What it does instead is go deeper into Indigo. From its own notes:

> `IndigoHIDMessageForMouseNSEvent`'s 7-arg signature is the path every other
> open-source bridge (`serve-sim`, `kittyfarm`, `opensafari`) targets, but on
> Xcode 26 it produces messages iOS either ignores or interprets as the Home
> gesture. The fix is to build a real `IOHIDEvent` digitizer parent + finger
> child, run it through `IndigoHIDMessageForTrackpadEventFromHIDEventRef` …
> then *patch two byte slots* the wrapper leaves uninitialised

**Quern is already on that path.** `tools/sim-bridge.swift` resolves the same
wrapper over `IOHIDEventCreateDigitizerEvent` + `…FingerEvent`, and
`wrapAndPatch` writes the same two slots — target `0x32` at `0x6c`/`0x10c`,
edge bitmask at `0x3a`/`0x3b` and `0xda`/`0xdb`. Two projects reached the same
recipe independently, which is the strongest evidence available that it is the
right one.

Where the two differ is instructive, because each has what the other lacks:

| | quern | baguette |
|---|---|---|
| digitizer via `…FromHIDEventRef` + byte patch | yes | yes |
| buttons (`IndigoHIDMessageForButton`) | yes | yes |
| keyboard (`…ForKeyboardArbitrary`) | yes | — |
| modifier keys (`…ForModifierKeyBit`) | yes | — |
| scroll wheel (`…ForScrollEvent`) | — | yes |
| edge gestures | **plumbing present, unreachable** | exposed |

That last row is the surprise. `wrapAndPatch(event:edgeBit:)` and
`sendDigitizerEvent(… edgeBit:)` already take an edge, default `0`, and
**nothing on the wire ever passes one** — `doSwipe` does not accept an edge,
so the byte-patching that would make a swipe a system gesture is written and
unreachable. #243 rates edge swipes as a protocol change; the expensive half
of it is already done.

The same is true one step further along. `doTap(… hold:)` exists *and* the
bridge already parses `hold` off the wire (`dict["hold"] as? Double ?? 0.05`).
The Python client never sends it. So long press — #243's item 1 — is
implemented in the bridge and simply not exposed.

Neither is verified to *work*, which is the caveat that matters: the offsets
are written but no test drives them. What changed is the estimate, not the
evidence.

One thing baguette does that quern does not: it guards **every** input call
with an `ensureWarm()` that re-establishes the Indigo connection if it has
gone cold. It has no Device Hub reclaim at all — no notify-state clear, no
`backboardd` restart. Whether that is sufficient on a host where Device Hub
has actually claimed the services is untested here, and it is the one place
worth measuring before treating baguette as complete.

### What this does to the plan

It removes DTUHID from the critical path for **primitives**. Everything #243
asks for, except a second transport, appears reachable on Indigo as it stands:
scroll needs one more `dlsym` (baguette shows the symbol), edge gestures need a
parameter threaded through, and long press, pinch and pan are compositions of
events quern can already build.

DTUHID's remaining argument is narrower and still real: it is the transport
Apple is moving to, and on a host where Device Hub holds the legacy services it
would sidestep the suppression rather than repairing it. But baguette running
on Xcode 26 and 27 without it, and quern running on Xcode 27 with the reclaim,
are two working existence proofs that Indigo is viable now.

So the honest order is: **expand Indigo using baguette as the reference**, keep
the reclaim, and treat DTUHID as a question to answer rather than a migration
to schedule.

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
2. **What does the host side of DTUHID look like in practice?** The man page
   says UniversalHID over a DT remote service. idb's implementation is the
   only reference, since baguette never went there — and that asymmetry is
   itself worth weighing: the project closest to our approach looked at this
   problem and chose to stay on Indigo.
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
