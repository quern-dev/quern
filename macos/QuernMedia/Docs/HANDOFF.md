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

## Next, in order

1. **WDA supervision (#159).** Quern has none. The runner died three times
   in one session.
2. **Wifi devices (#163) — re-diagnosed, and harder than it looked.** The
   original theory (quern reads the tunnel address from the wrong place) is
   wrong, and the suggested devicectl fallback cannot work. See the issue
   comment for the measurements. Short version: tunneld *does* discover wifi
   devices and build tunnels for them, but those tunnels have a **median
   lifetime of 2.0 seconds** across 11,587 of them, against 32s for USB and
   13+ hours for the one live wired tunnel. `devicectl device info details`
   does report `connectionProperties.tunnelIPAddress`, but that tunnel only
   lives as long as the devicectl process — probing the address from another
   process immediately afterwards gives "No route to host". The real work is
   finding out why Network-transport tunnels churn, given one in the log
   managed 13.7 hours.
3. **Recorder and stream through one capture.** `StreamPipeline` already fans
   out to sinks; the preview path currently uses only the HTTP one.

`ios-preview --interactive`'s window layer is still AppKit and still separate
from `quern-media`, which stays headless. That split is deliberate.

Then: Android on-device encoder, and the video-anchored timeline.

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
