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

- [x] **A CodeRabbit full pass.** Done at 15:46Z on `d3bb987`, after three
  earlier attempts — one refused on the review limit, two accepted and
  silently never ran. Three actionable findings, all fixed and
  mutation-tested: `Recorder.finish()` overwriting a start failure with
  `.alreadyFinished`, the binary install rewriting a running executable
  in place, and the dropped-frame gate corrupting H.264 viewers. Two
  review agents had covered `a00eeb2` earlier and found nine more.

  Confirm a review actually *started* before waiting on one — the
  acknowledgement body says `Reviews are available now` when it did and
  carries an `Action not completed` block when it did not, and the
  walkthrough's "Review limit reached" banner is a stale edit that lies.

- [ ] **One re-read of the four commits since that review.** The fixes above
  plus the ambiguous-device-name refusal. CodeRabbit auto-resolved all
  three threads on the push, which is not the same as having read the
  fixes — it resolves what it can see a diff for. `merge-pr.sh` refuses
  while the head is newer than the newest review and `--ask` requests
  one; the coordination note below still applies.

  **Coordinate before asking.** The window is repo-wide — shared across
  PRs and across agents, six reviews over four PRs in one measured
  evening. A request on this branch can take the slot from someone
  actively trying to land something, so check `gh pr list` and ask the
  sessions working those PRs first. This branch is parked; almost
  anything else in flight has a better claim.

  **"One per hour" is stale — do not plan against a number.** The
  subscription was upgraded on 2026-09-23 and the window is shorter but
  genuinely unknown. Measured that day: a refusal quoting 25 minutes, a
  banner on #275 quoting 39, two grants on #277 about eleven minutes
  apart, and #164's own request granted immediately after a review four
  hours earlier. A refusal costs nothing, so retry rather than compute
  when a window *should* have reopened — and read the acknowledgement
  body, where `Action not completed` is the refusal.

  That also explains a stall recorded here as unexplained: an
  acknowledgement can say `Reviews are available now` and still produce
  nothing if another PR takes the window before it runs. Plausible rather
  than proven — the grant times for those two were never captured.

### Product work

- [ ] **`feat/media-keyframe-control` is pushed and parked, awaiting #164.**
  One commit (`be73b74`): `POST /keyframe` on the stream server, so a
  caller can ask for an IDR without reconnecting. No PR yet, on purpose —
  it stacks, and a stacked PR is never auto-reviewed.

  **Collapse the two callbacks when rebasing it, do not keep both.** That
  branch adds `onKeyframeRequested` beside `onClientAttached` and wires
  both to the same closure in `main.swift`. #164 has since renamed
  `onClientAttached` to `onKeyframeNeeded`, because the H.264 desync gate
  made it fire for a second reason; a control request is a third, and all
  three mean one thing to the pipeline. So the rebase should call the
  existing `onKeyframeNeeded?()` from the `POST /keyframe` route and
  delete the added parameter. A mechanical conflict resolution keeps both,
  which restores the trailing-closure hazard that branch's own comment
  spends six lines warning about — a lone trailing closure binds to the
  *last* closure parameter, so adding one at the end silently rebound
  every existing call site with no diagnostic.

- **Done: a viewer attaching to an idle simulator now gets a picture in
  ~0.1s.** Measured on a completely idle simulator: 0.108s to the first
  complete JPEG, against **no frame at all within 25 seconds** before —
  the previously recorded "~14s" was just something eventually
  compositing, so the real behaviour was unbounded.

  The planned fix recorded here — cache the last payload in
  `HTTPStreamServer` and replay it to a new client — **would not have
  worked**, and checking beat implementing. `StreamPipeline.consume`
  returns before encoding when no sink wants frames, so with nobody
  watching there is no last payload to cache; the cache would have been
  empty for the first viewer, which is the case that actually hurts.

  What works is priming on attach, which `start()` already did once:
  `FrameSource.requestCurrentFrame()` (default no-op, since a continuous
  CoreMediaIO source delivers at 60fps regardless), implemented on
  `SimulatorFramebuffer` as the same `captureLatest()` hop, and wired to
  the server's attach handler in `main.swift` alongside the keyframe
  request.

  Only verified live: the wiring is in `main.swift`, which no test target
  covers. The unit test asserts only that the call is safe before start
  and after stop.

- **Done: a dead stream is now noticed.** Both URLSession timeouts stay
  unbounded — a 15s inactivity timeout killed previews of idle
  simulators — so the detection is a pair, not a timeout:

  - `HTTPStreamServer` repeats its last frame when nothing has been sent
    for `keepalive` (5s default), MJPEG only. Every JPEG stands alone, so
    a repeat is valid and a browser simply redraws; an H.264 stream
    cannot have frames replayed into it, and its consumers are ffplay and
    the recorder rather than the preview window. An active stream pays
    nothing — both cases are tested, and `keepalivesSent` is counted so a
    test asserts the repeat happened rather than that nothing broke.
  - `MJPEGClient` treats 20s of silence as death, which is four missed
    keepalives. It reports through the existing `onError` path, so the
    server hears `window_closed` and tears the producer down.

  Verified against the case a closed connection cannot cover: `SIGSTOP`
  on `quern-media`, so the socket stays open and no FIN is ever sent.
  Wedged at 9.0s, reported at 29.0s, `window_closed` at 29.1s. A peer
  that *dies* still arrives faster, as `didCompleteWithError`.

  The one invariant to keep: the client timeout must stay well above the
  server keepalive. Setting it below inverts the pair and every idle
  stream is declared dead — observed while testing with a deliberately
  short timeout.

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

  - **Clocks already agree, and the join needs no conversion at all.**
    Our frames are stamped `CMClockGetHostTimeClock()`, which is mach
    absolute time — measured identical to Python's `time.monotonic()` on
    this host (612668.5505 vs 612668.5969, the delta being two process
    starts). Every trace action now carries `started_monotonic` on that
    same clock, so a frame PTS compares against it directly. The action's
    end is `started_monotonic + duration_ms / 1000`.
  - **The `clock_anchor` is for labels, not for the join.** A
    `(wall, monotonic)` pair is written per export, so wall-clock times
    can be rendered; nothing in the video join depends on it.
  - **`wall - monotonic` looks constant, and per-export is justified
    by a different argument.** Two wrong claims were made about this in
    one day, so the readings are recorded with how they were parsed:

        sysctl -n kern.boottime -> { sec = 1789402153, usec = 218257 }

        parsed sec+usec : mono - (wall - boot) = +4.2956  then  +4.2980
        parsed sec only : mono - (wall - boot) = +4.0773  then  +4.0800

    The 0.218257 between the two columns is exactly the dropped `usec`.
    An apparent 0.2s of "drift" was one reading from each column being
    differenced against the other; three readings twenty seconds apart
    agree to the millisecond, and half an hour apart to ~2ms. Nothing
    here has observed drift.

    So the honest reason for reading the anchor per export is **not**
    measured drift. It is that a recorded anchor is wrong the moment the
    wall clock is *stepped* — NTP correction, a manual change — which a
    long-running server cannot detect, and the failure is silent and
    unbounded. Two syscalls to avoid it. That argument needs no
    measurement, which is the point.

    The monotonic base also does not tick while the machine is asleep.
    True, and not what these numbers show.

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
    re-encode. "Nearest keyframe to offset" stays on our side, where the
    encoder parameters are.
  - **Done: `Recorder.Summary.startHostTime`** carries the first
    written frame's PTS in seconds on the host clock, and `quern-media`
    logs it (`[record] N frames over Xs from host 612668.550550, ...`).
    That is our half of the interface: subtract it from an absolute host
    time to get a movie-relative offset.

  - **The .mp4 timeline is not zero-based** — `startSession` uses the
    first frame's real PTS, so it runs in host-monotonic seconds since
    boot. Anything assuming 0.0 is wrong.
  - **Their device-log attribution compares a device clock against a host
    interval with no offset.** Harmless for simulators, wrong for
    physical devices. Our sub-millisecond lockdown alignment is better
    than anything in the trace today but is *not* exposed as an API, and
    only holds while the connection is held — a live measurement, not a
    cacheable constant. Surfacing it is a possible task, not a promise.
  - **An action's visible effect can land after its interval ends.**
    Most actions hand work to the device and return before anything
    happens — measured, `open_url` ran 143ms and the flow it caused
    arrived 174ms *after* it closed. The trace attributes flows landing
    within a 3s grace window to the preceding action and marks them
    "attributed by timing rather than observed causation".

    For keyframes this is a rendering constraint, not a placement one. A
    keyframe still belongs at the action's start: it is a seek point, and
    the consequence is seen by playing forward from it, which needs no
    second anchor. What it forbids is drawing a video region for
    `[started_at, finished_at]` and implying the visible result is inside
    it — the causal span is wider than the interval by up to the grace.
    Same discipline as `.arrival` vs `.reported` on our side: inferred
    and observed must not render identically.
  - **Flow attribution is verified** (2026-09-22), including HTTPS under
    local capture, with `simulator_udid` resolved from the client pid.
    The earlier "zero flows in every live run" caveat is withdrawn: it
    was a misconfiguration — `processes: ["MobileSafari"]` *replaces* the
    default list and so dropped `com.apple.WebKit.Networking`, which is
    the process that actually makes the requests. No error, no flows, and
    a correct config indistinguishable from a broken one. Third time this
    shape has cost time on this work: an empty result and a broken one
    look the same.
  - **Still unconfirmed:** device-log attribution reads the shared ring
    buffer without a verified source filter, so a trace's `logs` could
    carry a line that is not an app log. Does not touch
    `started_monotonic` or the interval fields.
  - **Re-read `clock_anchor` per recording, never cache one.** Their
    side reads it per export precisely because a stepped wall clock
    invalidates it silently. An anchor kept alongside an earlier
    recording is wrong with nothing to say so.
  - **The trace branch is unmerged**, now PR #259 and awaiting a
    review window. The fields above are real and testable now, but the
    shape can still move if review pushes back.
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
- [ ] **`MJPEGClient.framing` and `announced` are unguarded** on the grounds
  that only URLSession's serial delegate queue touches them. Verified
  true and enforced by nothing — a future `reconnect()` breaks it
  silently. (`buffer` is gone; the parser holds that state now.)
- [ ] **`Recorder.finishWritingOverride` is `internal`, not test-scoped**, so
  anything in the module can swap the writer out.

### Test coverage gaps

- [ ] **`tools/ios-preview/main.swift` still has no test target** — but the
  riskiest piece is out of it. The frame parser now lives in
  `QuernMedia/Encode/JPEGFraming.swift` with seven tests, and the script
  compiles that same file rather than carrying a copy.

  The mechanism, since it is not obvious: Swift allows top-level code only
  in a file named `main.swift`, so renaming the script is what lets a
  second file join the same single `swiftc` invocation. No module, no
  link step, one compile as before. `build_preview_bundle` compiles both
  and takes freshness from the newest of them — comparing against the
  script alone would leave an edit to the parser silently not taking.

  What is still untested in there: the session lifecycle, the JSON-lines
  command handling, the window sizing, and the URLSession delegate
  behaviour that produced both shipped defects. Those are not pure, and
  testing them means either a real test target for the app or moving more
  logic into the package.
- **Done: `ShutdownGuard` moved into the package and tested.** It was
  untestable only because it sat in the executable target. Three tests,
  including the one that matters: a caller arriving mid-run must *wait*
  for the real status rather than be handed a zero. Mutation-tested
  against the "publish done on entry" version.
- **Done: `StartFailure` is tested.** Two `HTTPStreamServer`s on one port
  do reliably conflict despite `allowLocalEndpointReuse` — the second
  `start()` throws and `isListening` stays false. Mutation-tested by
  restoring the log-and-continue behaviour.
- **Done: `RecordingSink.failure`** is covered — the sink owns its recorder
  privately, so a nil `finish()` is only actionable if the reason comes
  out with it, and `main.swift` dispatches on exactly that.

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
