# Unified screen streaming

Status: spike findings, no shipped code. Branch `spike/sim-framebuffer-preview`.

Goal: one preview/streaming path across iOS simulators, physical iOS
devices and Android, with remote viewing as the stretch target (issue #127
asks for the window-management half of the same problem).

## Where frames come from

| Source | Mechanism | Frames today? |
|---|---|---|
| iOS simulator | CoreSimulator framebuffer, `registerScreenCallbacks` → `IOSurface` | yes, added in this spike |
| Physical iOS | CoreMediaIO DAL device + `AVCaptureVideoDataOutput` → `CVPixelBuffer` → `IOSurface` | yes, added in this spike |
| Android | `adb exec-out screenrecord --output-format=h264 -` → Annex-B H.264 | yes, verified; no code yet |

The two iOS sources converge on `IOSurface` and share one encoder.
`CVPixelBufferGetIOSurface` on a capture buffer returns the same type the
simulator framebuffer hands over, provided the capture output pins
`videoSettings` to 32BGRA — a DAL device otherwise negotiates YUV.

Android does not converge. It arrives already encoded, from a subprocess,
and never becomes an `IOSurface` without a decode. That asymmetry is the
central design fact: **Android is not a third frame source, it is a second
transport.**

## Measured

Local machine, headless throughout.

| Source | Resolution | Rate | Bitrate | Cost |
|---|---|---|---|---|
| iPhone 16 Pro sim | 1206x2622 → 900px | 13.3 fps | 3.8–5.3 Mbps | 9–11% CPU, 25 MB |
| iPad Pro 11" sim | → 900px | 13.1 fps | 5.2 Mbps | ~11% CPU, 25 MB |
| iPhone 11 (USB) | 828x1792 → 900px | 12.8–13.4 fps | 4.2–4.4 Mbps | 20% CPU, 113 MB |
| Android emulator | 720x1600 native | 21.9 fps | 1.75 Mbps | negligible host CPU |
| Pixel 3 XL (USB) | 1440x2960 → 720x1480 | 29.5 fps | **requested** | negligible host CPU |

MJPEG is the reason the iOS numbers are what they are. Android is ~2.5x
cheaper at higher resolution and higher frame rate because the encode
happens on the device, in hardware, and the host only moves bytes.

Two devices on a LAN is comfortable. An 18-device farm at MJPEG rates is
~90 Mbps and a lot of host CPU, which is what makes H.264 a requirement
for the stretch goal rather than a refinement.

On real Android hardware the bitrate is not merely lower, it is *chosen*.
Measured on the Pixel over USB, 4s each at a constant ~29.5 fps:

| `--bit-rate` | actual |
|---|---|
| 1M | 945 kbps |
| 2M | 2256 kbps |
| 6M | 7087 kbps |

Frame rate held at ~29.5 fps across all three, so rate and quality are
independent knobs. That reframes the farm arithmetic: 18 devices at 1 Mbps
is ~18 Mbps, which is a WAN-viable number, against ~81 Mbps for the same
count over MJPEG. The emulator's 1.75 Mbps was not efficiency, it was the
encoder under-running a 4M request because the emulated display produced
fewer frames.

## screenrecord specifics

- `--output-format=h264` is **undocumented** — absent from `screenrecord
  --help` — but present and working on both v1.3 (emulator, Android 13)
  and v1.2 (Pixel 3 XL, Android 10). Two versions three releases apart
  both support it, so it is reasonably portable, but it is still an
  unsupported flag: pin a fallback.
- Output is Annex-B with a single SPS/PPS/IDR at the head.
- **180 second hard cap.** Not configurable past it. Any long-lived stream
  must restart on a timer regardless of anything else.
- **One IDR per session, confirmed on real hardware.** The emulator gave
  262 non-IDR slices to 1 IDR over 12s; the Pixel gave 119 to 1 over 4s at
  30 fps. There is no `force_idr`: we do not own the encoder, unlike a
  VideoToolbox path. (The Pixel emits SPS/PPS twice at the head rather
  than once — a duplicate parameter set, not a periodic one.)
- Restart costs ~110 ms on the emulator (138/100/112 ms) and **~290 ms
  over real USB** (291/298/284 ms). Every restart begins with a fresh
  SPS/PPS/IDR. Budget the higher number: it is the join latency floor and
  the glitch every existing viewer sees when someone new attaches.

The IDR scarcity is the real constraint. A viewer joining mid-stream
cannot decode until a keyframe arrives, and none will. The only lever is
restarting the subprocess, which mints one. So keyframe cadence and
process lifetime are the same knob: restart every N seconds to bound
join latency, and N must be under 180 anyway.

## Browser delivery

MJPEG needs no client code — `<img src="/stream">` renders a
`multipart/x-mixed-replace` response in every browser. That is why the
iOS spike used it: cheapest possible proof the pipeline works.

H.264 cannot go in an `<img>`. It needs fMP4 via Media Source Extensions,
or WebCodecs `VideoDecoder` to a canvas. Remuxing Annex-B → fMP4 was
verified with ffmpeg and the result decodes back to correct frames.

Two traps found while verifying:

- **`+frag_keyframe` is wrong here.** It fragments at keyframes, and with
  one IDR per session that is one unbounded fragment which never flushes
  until the stream ends. MSE receives nothing. Use `-frag_duration` /
  `+frag_custom` for time-based fragmentation instead.
- **Aggressive low-delay flags backfire.** `-probesize 32
  -analyzeduration 0` starves the H.264 demuxer of the bytes it needs to
  parse SPS, and first output slipped from 1.8 s to 8.3 s — the whole
  capture. A moderate probesize is required.

ffmpeg would be a new runtime dependency (Android already needs scrcpy).
A hand-written Annex-B → AVCC remuxer avoids it and gives direct control
of flush timing, which is where the latency actually lives. baguette does
its own AVCC conversion for the same reason.

## Consequences for the design

1. The frame-source protocol covers the two iOS sources cleanly. Android
   should implement a *stream* interface, not a frame interface, or it
   forces a pointless decode/re-encode.
2. Codec choice belongs to the transport, not the source. Local window =
   no codec, hand the IOSurface to a CALayer. LAN = MJPEG is adequate and
   trivial. Remote/farm = H.264.
3. Android inverts the usual cost model: it is the cheapest source to
   stream remotely and the most expensive to show in a local window,
   because scrcpy owns its window and macOS will not reparent it (#127).
4. Capture changes what iOS reports. A device captured over CoreMediaIO
   shows a synthetic status bar — 9:41, full signal, full battery. Fine
   for demos, misleading for diagnosis. Android shows real state.

## Verified on real hardware

Everything in the screenrecord section was re-run against a Pixel 3 XL
(Android 10, screenrecord v1.2, 1440x2960) over USB, not just the
emulator. The flag, the single-IDR behaviour and the restart-mints-a-
keyframe property all held. What changed was quantitative: real hardware
sustains 30 fps where the emulator managed 22, restart costs ~2.6x more
over USB, and bitrate turns out to be a dial rather than an outcome.

A Pixel is close to AOSP. A Samsung or Xiaomi would be the real test of
vendor divergence, and has not been done.

## Not investigated

- Whether a hand-written remuxer hits acceptable latency (expected, not measured).
- WebCodecs as an MSE alternative.
- Multi-device fan-out: one server, N sources, `/stream/<id>` routing.
- Input injection over the same channel. sim-bridge already has iOS tap and
  swipe, so an interactive remote view is closer than it looks.
- Authentication. The spike binds loopback unless `--bind-all`, and
  `--bind-all` is unauthenticated. Anything remote needs a real answer here.
