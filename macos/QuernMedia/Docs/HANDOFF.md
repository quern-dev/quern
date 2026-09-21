# Handoff — media engine

Written 2026-09-13 at the end of the session that built this, updated
2026-09-14. Everything below is either verified or explicitly flagged as
unverified.

## Where things are

| branch | state |
|---|---|
| `feat/media-engine` | this work, and the preview app that consumes it. PR #164. 79 Swift tests, 37 Python tests across media_engine and preview. |
| `spike/sim-framebuffer-preview` | the spike it was extracted from. Pushed, tagged `spike/media-2026-09-13`. Reference implementation + `WRITEUP.md` + `docs/proposals/unified-screen-streaming.md` (all the measurements). |

Issues from this work: **#159** WDA startup reinstalls, **#160** leaked usbmux
forward, **#162** tunnel churn (resolved — USB topology), **#163** wifi
devices unreachable. PR **#161** is WDA docs.

## What exists and works

`macos/QuernMedia` is a SwiftPM package producing `quern-media`, a
**headless** video producer. No AppKit, no bundle, no window — `otool`
confirms nothing that draws is linked. Showing frames is the preview app's
job.

```
Frame.swift          CapturedFrame (+TimeAccuracy), EncodedFrame, 2 protocols
FrameThrottle.swift  deadline-based
StreamPipeline.swift owns encoder+throttle, fans out to FrameSinks
Capture/             SimulatorFramebuffer, CaptureDevice, PrivateFrameworks
Encode/              AnnexB, JPEGEncoder, H264Encoder
Sinks/               Recorder, HTTPWire, HTTPStreamServer
```

Verified end to end: MJPEG and H.264 streaming from a booted simulator and a
USB iPhone; recording to .mp4 with wall-clock timestamps where a deliberate
idle gap survives as elapsed time.

`server/device/media_engine.py` builds it, and **the preview app consumes
it**: `PreviewManager.add_simulator` starts a `quern-media` per simulator
serving MJPEG on loopback, and `ios-preview` opens a window on the stream via
a new `add_stream` command. Verified end to end against a headless iPhone 16
Pro — one `added`, first frame 414x900 — and a stream that dies reports
`window_closed` so the server tears its `quern-media` down.

Sessions are keyed by identity, not by name: `AVCaptureDevice.uniqueID` for a
capture device, a udid for a simulator. `PreviewManager.add()` accepts a
device name, a device ID or a simulator udid and works out which it is.

## Work items

The list. Anything deferred lands here rather than in a commit message
nobody re-reads. Ordered within each group; groups are not ordered against
each other.

### Blocking the merge of #164

- [ ] **`075602f` has never had a CodeRabbit full pass.** Three attempts:
      one refused on the OSS review limit, two accepted and silently never
      ran. Two review agents covered `a00eeb2` instead and found nine real
      problems, so the gap is partly filled, but `075602f` itself is
      unreviewed. Confirm a review actually *started* before waiting on one —
      the acknowledgement body says `Reviews are available now` when it did
      and carries an `Action not completed` block when it did not, and the
      walkthrough's "Review limit reached" banner is a stale edit that lies.

### Product work

- [ ] **A viewer attaching to an idle simulator sees ~14s of black.**
      `multipart/x-mixed-replace` makes URLSession deliver a "response" per
      part, so the add acknowledgement is in practice gated on the first
      frame rather than the HTTP header — and an idle simulator composites
      nothing. Fix: cache the last payload in `HTTPStreamServer` and write it
      to a newly streaming client. Valid for MJPEG, where every frame stands
      alone; H.264 needs the existing `onClientAttached` keyframe request
      instead. This also removes the acknowledgement latency.
- [ ] **No client-side liveness bound on a stream.** Both URLSession
      timeouts are unbounded, which is correct — a 15s inactivity timeout
      killed idle previews — but it means only a peer that *closes* the
      connection is detected. A dropped network or a wedged `quern-media`
      leaves the window on its last frame with the server still believing the
      preview is live. Fix: an idle watchdog in `MJPEGClient` plus a periodic
      keepalive part from `HTTPStreamServer`, since a transport timeout
      cannot tell "idle" from "gone".
- [ ] **WDA supervision (#159).** Quern has none. The runner died three
      times in one session.
- [ ] **Wifi devices (#163) — re-diagnosed, and harder than it looked.** The
      original theory (quern reads the tunnel address from the wrong place)
      is wrong, and the suggested devicectl fallback cannot work. tunneld
      *does* discover wifi devices and build tunnels for them, but those
      tunnels have a **median lifetime of 2.0 seconds** across 11,587 of
      them, against 32s for USB and 13+ hours for the one live wired tunnel.
      `devicectl device info details` does report
      `connectionProperties.tunnelIPAddress`, but that tunnel only lives as
      long as the devicectl process. The real work is finding out why
      Network-transport tunnels churn, given one in the log managed 13.7
      hours. Measurements are on the issue.
- [ ] **Recorder and stream through one capture.** `StreamPipeline` already
      fans out to sinks; the preview path uses only the HTTP one.
- [ ] **The video-anchored timeline.** The consumer now exists: the
      tracefile on `feat/logging-trace-export` (stacked on #253), joining
      quern actions, proxy flows, device logs and screenshots.
      `server/trace.py` is the join, `server/api/trace.py` the endpoint.
      Established with its author 2026-09-21:

      - **Clocks already agree.** Our frames are stamped
        `CMClockGetHostTimeClock()`, which is mach absolute time — measured
        identical to Python's `time.monotonic()` on this host (612668.5505 vs
        612668.5969, the delta being two process starts). Trace entries are
        wall clock, so joining needs one `(time.time(), time.monotonic())`
        anchor and a subtraction. They are adding the anchor.
      - **Re-record that anchor per session, not once at server start.** The
        monotonic base does not advance while the machine sleeps; `wall -
        monotonic` on this host already sits ~4s from `kern.boottime`. Drift
        with no upper bound and nothing to detect it.
      - **`finished_at` is when an action ENDED.** Markers placed there are
        late by the action's own duration — measured 2369ms cold and 129ms
        warm for a tap on one simulator, so not a constant to subtract. The
        interval is `[finished_at - duration_ms, finished_at]`. They are
        adding `started_at` per action rather than making us re-derive it.
      - **A discrete begin event already exists** (`outcome="started"`, no
        duration, emitted before the work runs) but is DEBUG-only and
        currently filtered out of the trace endpoint. That is the anchor for
        action-aligned keyframes, which is what fixes seek granularity
        drifting with activity.
      - **Reference video as path + offset, never a stored keyframe index.**
        An index is a property of one encode and goes silently wrong on
        re-encode. We publish the first frame's PTS in the recording summary
        so an offset can be computed; "nearest keyframe to offset" stays on
        our side, where the encoder parameters are.
      - **The .mp4 timeline is not zero-based** — `startSession` uses the
        first frame's real PTS, so it runs in host-monotonic seconds since
        boot. Anything assuming 0.0 is wrong.
      - **Their device-log attribution compares a device clock against a host
        interval with no offset.** Harmless for simulators, wrong for
        physical devices. Our sub-millisecond lockdown alignment is better
        than anything in the trace today but is *not* exposed as an API, and
        only holds while the connection is held — a live measurement, not a
        cacheable constant. Surfacing it is a possible task, not a promise.
      - **Flow attribution is untested** — zero flows in every live run they
        have done. Do not lean on it without exercising it.
- [ ] Android on-device encoder.

### Known defects, deferred with reasons

- [ ] **A failed `SimulatorFramebuffer.start()` leaves the successful half
      registered.** If `register(on:)` throws for the second descriptor the
      first stays registered and the state stays populated, with no `stop()`
      to undo it. Harmless in `quern-media`, which exits; a library caller
      that retries double-registers. Pre-existing.
- [ ] **A concurrent second `stop()` returns before the first has finished
      cleaning up.** Deliberately not fixed with a lock: a main-thread
      `stop()` holding it inside `queue.sync`, while an `onFrame`-thread
      `stop()` waits for it, deadlocks. The `stopped` flag is already set
      before the early return, so frames are suppressed either way.
- [ ] **`Recorder.finish()` discards the frame counts when it returns nil.**
      A caller reporting a failed recording cannot say how much was in it.
- [ ] **`MJPEGClient.buffer` and `announced` are unguarded** on the grounds
      that only URLSession's serial delegate queue touches them. Verified
      true today and enforced by nothing — a future `reconnect()` breaks it
      silently.
- [ ] **`Recorder.finishWritingOverride` is `internal`, not test-scoped**, so
      anything in the module can swap the writer out.

### Test coverage gaps

- [ ] **`tools/ios-preview.swift` has no test target at all.** It is a
      single-file `swiftc` script, which is why the 15s idle-preview defect
      shipped. The MJPEG frame parser and the session-key resolution are both
      pure functions that would test cheaply if the file were split.
- [ ] **`ShutdownGuard` and the exit-status propagation are untested** — the
      type lives in the executable target and nothing imports it.
- [ ] **`HTTPStreamServer.StartFailure` is untested.** Neither `.notReady`
      nor `.listenerFailed` appears in any test, though "a bind failure was
      only logged, so a server that never came up still looked started" was
      half the reason for the `.ready` wait. A test needs a reliably
      unbindable port, which `allowLocalEndpointReuse` makes awkward.
- [ ] **`RecordingSink.failure`** is public API with no test.

### Decided against

- **Porting `ios-preview --interactive`'s window layer into `quern-media`.**
  The engine stays headless; the AppKit half stays where it is. Consolidating
  would mean re-deriving the acknowledgement timing and inventing a simulator
  enumeration source, to rewrite a feature nobody has complained about.
- **Decoding WDA's MJPEG back to surfaces** to make it fit `FrameSource`.
  That discards the only advantage an already-encoded source has. See
  `Docs/source-options.md`.

## Traps that cost time — do not rediscover these

**Build and repo**

- `git add -A` from the repo root will sweep `macos/QuernMedia/.build`
  (~116 MB) into every commit. `.gitignore` covers it now; it did not, and
  cleaning it needed a `filter-branch` and a `gc`.
- There is **no venv in a worktree**. Use the main checkout's interpreter,
  `<main-checkout>/.venv/bin/python`, with `PYTHONPATH=$PWD`.
- The main checkout is an **editable install**, so `import server` resolves
  there, not to your worktree. Pure-Swift work is unaffected; Python work is
  not. `QUERN_STATE_DIR` redirects `CONFIG_DIR` if you need to isolate
  build artifacts from `~/.quern`.

**APIs that lie or bite**

- `CMTime(seconds:preferredTimescale:)` **truncates**. `11/60` at timescale
  600 gives 0.18166 rather than 0.18333 — a frame stamped a tick early.
  Build test timestamps from integer value/timescale. This looked exactly
  like a throttle bug.
- `UsingHardwareAcceleratedVideoEncoder` reports **false** for the JPEG
  codec while the work plainly leaves the cores. Do not gate on it.
- `AVAssetWriterInput` with nil `outputSettings` cannot be added without a
  `sourceFormatHint`, and that hint is the encoder's format description —
  which does not exist until the first frame. Create the input lazily.
- Converting AVCC to Annex-B by loading each 4-byte length as a `UInt32`
  **traps on alignment**. Assemble bytewise.
- `URLSession` parses `multipart/x-mixed-replace` and hands back only part
  bodies, so it cannot see MJPEG framing. Test the wire with a raw socket.
  It bites the *client* too, twice: frame boundaries have to be found by
  scanning for JPEG start/end markers rather than the multipart boundary,
  and `didReceive response:` fires **once per part**, so anything hung off
  it needs a fire-once gate — the add acknowledgement went out per frame
  until one went in.
- An **idle simulator sends no frames at all**: the framebuffer is
  event-driven and free while nothing composites. `curl` against one returns
  200 and zero bytes. Anything that waits for a first frame to decide a
  preview started will hang on a working preview; acknowledge on the
  response instead.
- One `NWConnection.receive` is not one HTTP request. TCP will split
  `GET /stream`, and routing on the first segment served the index page.
  Accumulate to `\r\n\r\n`.
- `ps %cpu` is a since-start average and read about **half** the real value.
  Measure CPU-time deltas over a window.
- **Xcode 27 moved SimulatorKit and deleted the directory it lived in.**
  `Contents/Developer/Library/PrivateFrameworks/` no longer exists; the
  framework is now at `Contents/SharedFrameworks/SimulatorKit.framework`, a
  *sibling* of `Developer` rather than a relocation inside it. CoreSimulator
  is unaffected — it lives at `/Library/Developer/PrivateFrameworks/`, outside
  Xcode — which is why capture kept working and the only symptom was a
  `dlopen` line in the log. `tools/sim-bridge.swift` was already fixed on
  main; `PrivateFrameworks.swift` had the same hardcoded path and was not.
  Check both when a probe and a `dlopen` can disagree.
- **Xcode 27's SwiftPM changed the build layout.** Products are under
  `<scratch>/out/Products/Debug/` rather than
  `<scratch>/arm64-apple-macosx/debug/`. Nothing in the repo hardcodes it —
  `media_engine.py` asks `swift build --show-bin-path`, which returns the new
  location — but anything that guesses the path will break.
- CoreMediaIO publishes devices only while a run loop turns. `Thread.sleep`
  finds nothing, which is indistinguishable from "no device connected".

**Device reality**

- Quitting Simulator.app shuts down simulators it booted. `simctl boot`
  ones survive.
- iOS **fakes the status bar** (9:41, full bars) whenever a device is
  captured over CoreMediaIO. WDA's screenshots and MJPEG show the real one.
- Physical iOS devices hate USB hub chains. An iPhone three hubs deep
  sharing with an external drive produced 30 RemoteXPC disconnects in 45
  minutes; moving it to its own port took that to ~zero. See #162 — the
  diagnostic recipe is counting `Created tunnel` in
  `/Library/Logs/com.quern.tunneld.log`.
- `~/.quern/tunneld.log` is a **stale orphan** (deleted now). The live log
  is `/Library/Logs/com.quern.tunneld.log`, per the LaunchDaemon.

## Open questions, honestly unresolved

- **WDA MJPEG (port 9100) fits neither source protocol.** It is a stream of
  JPEGs — already compressed, but not H.264 sample buffers. Either a third
  protocol, or generalise `EncodedFrameSource`. Do not decode JPEGs back to
  surfaces to make it fit; that discards the only advantage it has. See
  `Docs/source-options.md`.
- ~~Can tunneld and devicectl hold tunnels to the same device at once?~~
  **Answered: yes.** Both were observed holding tunnels to the same wifi
  iPhone simultaneously, each with its own address. They do not contend.
- `MaxKeyFrameInterval` counts **frames, not seconds**, so on an
  event-driven source seek granularity drifts with activity. Anchoring
  keyframes to test actions is the fix for the timeline.

## Environment as left

Five worktrees: `quern` (main), `quern-media-engine`, `quern-wifi-devices`
(branch `fix/wifi-device-tunnel`, no commits — #163 was parked once the
diagnosis changed), `quern-sim-preview-spike`, `quern-wda-docs`. One
simulator booted headless (iPhone 16 Pro). Two physical iPhones attached, no
WDA session running.

CI runs the media suite with `--no-parallel`. Runners are VMs where
VideoToolbox falls back to software encoding, and 72 tests at once on three
cores made every real-time deadline in the suite miss.
