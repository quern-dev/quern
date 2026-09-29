# QuernMedia

A SwiftPM package producing `quern-media`, a **headless** video producer for
iOS simulators and USB-connected devices. No AppKit, no bundle, no window —
`otool` confirms nothing that draws is linked. Showing frames to a person is
the preview app's job; this makes frames available for it, a browser, `ffplay`
or a file to consume.

```text
Frame.swift          CapturedFrame (+TimeAccuracy), EncodedFrame, 2 protocols
FrameThrottle.swift  deadline-based
StreamPipeline.swift owns encoder + throttle, fans out to FrameSinks
Capture/             SimulatorFramebuffer, CaptureDevice, PrivateFrameworks
Encode/              AnnexB, JPEGEncoder, H264Encoder, JPEGFraming
Sinks/               Recorder, HTTPWire, HTTPStreamServer
```

MJPEG and H.264 both stream from a booted simulator and a USB iPhone, and
recording writes an `.mp4` whose timestamps preserve a deliberate idle gap as
elapsed time.

## How quern uses it

`server/device/media_engine.py` builds and installs the binary.
`PreviewManager.add_simulator` starts one `quern-media` per simulator serving
MJPEG on loopback, and `ios-preview` opens a window on that stream via its
`add_stream` command. A stream that dies reports `window_closed`, so the
server tears its producer down.

A simulator is not a CoreMediaIO capture source, which is the whole reason
this package exists: physical devices are captured directly by the preview
app, and simulators can only be reached through their framebuffer.

Sessions are keyed by identity, never by name — `AVCaptureDevice.uniqueID`
for a capture device, a udid for a simulator. Two phones of one model report
the same name, so a name is input to be resolved, not a key to store.

## HTTP surface

```text
GET  /          a page that plays the stream
GET  /stream    the video itself
POST /keyframe  force an IDR now, answers 204
```

`--bind-all` serves these on every interface and is **unauthenticated** by
design; it is opt-in and the usage text says so.

## Before changing anything here

Read [`Docs/platform-traps.md`](Docs/platform-traps.md). Every entry is an
Apple API that lies or a device behaviour that is not discoverable from the
API surface, and each cost at least a session to find.
[`Docs/source-options.md`](Docs/source-options.md) covers why the source
protocols are shaped the way they are.

The suite runs with `--no-parallel`; see `CONTRIBUTING.md` for why.
