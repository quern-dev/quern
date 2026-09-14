# Handoff — media engine

Written 2026-09-13 at the end of the session that built this. Everything
below is either verified or explicitly flagged as unverified.

## Where things are

| branch | state |
|---|---|
| `feat/media-engine` | this work. Pushed. 70 Swift tests, 14 Python tests. |
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

`server/device/media_engine.py` builds it (`swift build --scratch-path`) but
**nothing consumes it yet**. That is the next job.

## Next, in order

1. **Preview app consumes `quern-media`.** The agreed direction. One capture
   feeds window + recorder + stream through `StreamPipeline`'s sinks, instead
   of each opening its own. Makes simulators previewable in the existing UI,
   which they are not today. `preview.py` and `ios-preview` are deliberately
   untouched so far — this is additive, and `ios-preview --interactive`
   (the JSON-lines protocol driving the menu-bar windows) is **not ported**.
2. **Wifi devices (#163).** Small fix, real capability. Proven that session,
   input and video all work over wifi; quern just looks for the tunnel in
   the wrong place.
3. **WDA supervision (#159).** Quern has none. The runner died three times
   in one session.

Then: Android on-device encoder, and the video-anchored timeline.

## Traps that cost time — do not rediscover these

**Build and repo**

- `git add -A` from the repo root will sweep `macos/QuernMedia/.build`
  (~116 MB) into every commit. `.gitignore` covers it now; it did not, and
  cleaning it needed a `filter-branch` and a `gc`.
- There is **no venv in a worktree**. Use
  `/Volumes/Home/jham/Dev/quern/.venv/bin/python` with `PYTHONPATH=$PWD`.
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
- `ps %cpu` is a since-start average and read about **half** the real value.
  Measure CPU-time deltas over a window.
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
- Can tunneld and devicectl hold tunnels to the same device at once, or do
  they contend? Unknown, and relevant to #163.
- `MaxKeyFrameInterval` counts **frames, not seconds**, so on an
  event-driven source seek granularity drifts with activity. Anchoring
  keyframes to test actions is the fix for the timeline.

## Environment as left

Four worktrees: `quern` (main), `quern-media-engine`, `quern-sim-preview-spike`,
`quern-wda-docs`. One simulator booted headless (iPhone 16 Pro). Two physical
iPhones attached, no WDA session running. Nothing else of this session is
still live.
