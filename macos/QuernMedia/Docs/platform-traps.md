# Platform traps in the media path

Apple APIs that lie, bite, or mean something other than they appear to, plus
the device realities behind them. Every one cost at least a session to find,
and none is discoverable from the API surface.

Scoped to `macos/QuernMedia` and the preview client in `tools/ios-preview`.
General quern conventions are in [`CONTRIBUTING.md`](../../../CONTRIBUTING.md);
the source-protocol design discussion is in
[`source-options.md`](source-options.md).

## Timing and encoding

- **`CMTime(seconds:preferredTimescale:)` truncates.** `11/60` at timescale
  600 gives 0.18166 rather than 0.18333 — a frame stamped a tick early. Build
  test timestamps from integer value/timescale. This presented exactly as a
  throttle bug.
- **`UsingHardwareAcceleratedVideoEncoder` reports `false` for JPEG** while
  the work plainly leaves the cores. Do not gate on it.
- **`AVAssetWriterInput` with nil `outputSettings` needs a
  `sourceFormatHint`**, and that hint is the encoder's format description —
  which does not exist until the first frame. Create the input lazily.
- **Converting AVCC to Annex-B by loading each 4-byte length as a `UInt32`
  traps on alignment.** Assemble bytewise.
- **`MaxKeyFrameInterval` counts frames, not seconds.** On an event-driven
  source the wall-clock interval stretches whenever the screen is idle, so
  seek granularity drifts with activity. Anchoring keyframes to actions is
  the fix; a fixed interval is not.
- **The `.mp4` timeline is not zero-based.** `startSession` uses the first
  frame's real PTS, so it runs in host-monotonic seconds since boot. Anything
  assuming 0.0 is wrong. `Recorder.Summary.startHostTime` is what turns an
  absolute host time into a movie-relative offset.
- **`CMClockGetHostTimeClock()` is mach absolute time, which is Python's
  `time.monotonic()`.** Measured identical on this host — 612668.5505 against
  612668.5969, the delta being two process starts. A frame PTS therefore
  compares directly against a trace action's `started_monotonic`, with no
  conversion.

## HTTP, MJPEG and the wire

- **`URLSession` parses `multipart/x-mixed-replace`** and hands back only part
  bodies, so it cannot see MJPEG framing. Test the wire with a raw socket. It
  bites the client twice over: frame boundaries must be found by scanning for
  JPEG SOI/EOI markers rather than the multipart boundary, and
  `didReceive response:` fires **once per part** — so anything hung off it
  needs a fire-once gate. The add acknowledgement went out per frame until one
  went in.
- **One `NWConnection.receive` is not one HTTP request.** TCP will split
  `GET /stream`, and routing on the first segment served the index page to a
  viewer that asked for video. Accumulate to `\r\n\r\n` before routing.
- **An idle simulator sends no frames at all.** The framebuffer is
  event-driven and free while nothing composites; `curl` against one returns
  200 and zero bytes. Anything that waits for a first frame to decide a
  preview started will hang on a *working* preview. Acknowledge on the
  response instead.
- **CoreMediaIO publishes devices only while a run loop turns.**
  `Thread.sleep` finds nothing, which is indistinguishable from "no device
  connected".

## Xcode and the private frameworks

- **Xcode 27 moved SimulatorKit and deleted the directory it lived in.**
  `Contents/Developer/Library/PrivateFrameworks/` no longer exists; the
  framework is at `Contents/SharedFrameworks/SimulatorKit.framework`, a
  *sibling* of `Developer` rather than a relocation inside it. CoreSimulator
  is unaffected — it lives at `/Library/Developer/PrivateFrameworks/`, outside
  Xcode — which is why capture kept working and the only symptom was a
  `dlopen` line in the log. `tools/sim-bridge.swift` and
  `PrivateFrameworks.swift` both hardcoded the old path and were fixed at
  different times. Check both when a probe and a `dlopen` disagree.
- **Xcode 27's SwiftPM changed the build layout.** Products are under
  `<scratch>/out/Products/Debug/` rather than
  `<scratch>/arm64-apple-macosx/debug/`. Nothing here hardcodes it —
  `media_engine.py` asks `swift build --show-bin-path` — but anything that
  guesses will break.

## Device reality

- **Quitting Simulator.app shuts down simulators it booted.** Ones booted by
  `simctl boot` survive.
- **iOS fakes the status bar** — 9:41, full bars — whenever a device is
  captured over CoreMediaIO. WDA screenshots and MJPEG show the real one.
- **Physical iOS devices hate USB hub chains.** An iPhone three hubs deep
  sharing with an external drive produced 30 RemoteXPC disconnects in 45
  minutes; its own port took that to roughly zero. See #162. The diagnostic
  is counting `Created tunnel` in `/Library/Logs/com.quern.tunneld.log` —
  note that path, not `~/.quern/tunneld.log`, which was a stale orphan.
- **tunneld and devicectl can hold tunnels to the same device at once.**
  Observed on one wifi iPhone, each with its own address. They do not
  contend.
