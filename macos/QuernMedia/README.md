# QuernMedia

A SwiftPM package producing `quern-media`, a **headless** video producer for
iOS simulators and USB-connected devices. No AppKit, no bundle, no window —
`otool` confirms nothing that draws is linked. Showing frames to a person is
the preview app's job; this makes frames available for it, for the page it
serves to a browser, or for a file.

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

`server/device/media/media_engine.py` builds and installs the binary.
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
GET  /frames    the video, one length-prefixed part per frame
GET  /stream    the video itself: multipart under MJPEG, raw Annex B under H.264
POST /keyframe  force an IDR now, answers 204
```

The page plays either codec from `/frames`. When the stream ends it greys the
last frame under an OFF AIR label and reconnects. H.264 goes through WebCodecs, and
`?stats` overlays frames per second, the longest gap between frames, and the
decoder's queue. Measured in Chrome on a Mac at native resolution: MJPEG held
60fps with gaps of 23-25 ms, while H.264 through the hardware decoder stalled
for 250-280 ms now and then with its queue empty. The frames had arrived on
time; the stall was in the decoder. Software decoding had no stalls but managed
only about 20fps. So MJPEG is the codec for a local preview, and H.264 is for
when bandwidth matters more than smoothness.

A streaming viewer must keep its sending side open: the server reads from each
stream to notice a viewer leaving, so one that half-closes after its request is
taken to have gone. While a simulator is shut down, streams are refused with 503
and the reason, and they work again once it has booted.

`?source=<UDID>` on `/stream` or `/frames` asks for a particular simulator and
gets 409 from a server streaming another. Viewers reconnect by port, ports are
reused, and this is what stops a reconnecting window attaching to someone
else's simulator. `--exit-with-parent` ends quern-media when the process that
started it exits, for owners whose crash would otherwise leave it running.

`--bind-all` serves these on every interface and is **unauthenticated** by
design; it is opt-in and the usage text says so.

## Before changing anything here

Read [`Docs/platform-traps.md`](Docs/platform-traps.md). Every entry is an
Apple API that lies or a device behaviour that is not discoverable from the
API surface, and each cost at least a session to find.
[`Docs/source-options.md`](Docs/source-options.md) covers why the source
protocols are shaped the way they are.

The suite runs with `--no-parallel`; see `CONTRIBUTING.md` for why.
