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

### Static screens were flattering the numbers

Every physical-device figure above was taken on a near-static screen —
one capture produced 133 frames of which only 9 were distinct. Re-measured
under sustained driven motion (Settings scrolled continuously via WDA, 30
fps cap, 900px, q0.6, mean of 10 CPU samples):

| encoder | CPU | RSS | delivered | bitrate |
|---|---|---|---|---|
| VideoToolbox | 11.7% | 71 MB | 21.8-23.5 fps | **8.7-10.8 Mbps** |
| ImageIO | 28.0% | 116 MB | 22.6-22.7 fps | 6.2 Mbps |

Bitrate roughly doubles against the static-screen measurement (4.2-4.4
Mbps). For farm sizing that is the number to use: 18 devices under real
interaction is ~160 Mbps of MJPEG, not ~80.

Two things to read carefully here. VideoToolbox shows a *higher* bitrate
than ImageIO at the same nominal q0.6 — that is the quality-scale
mismatch noted above, not worse compression, and it inflates VT's Mbps
against a CPU number that is still 2.4x better. And delivered rate tops
out at ~23 fps against a 30 fps cap on both encoders, so something other
than the encoder is the limit — either the device's change rate or the
synchronous encode. Not chased.

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

## Where the work actually happens

Measured on an M4 with `tools/encode-bench.swift`, 60 frames per path,
CPU time via `getrusage` against wall time. The ratio is the whole point:
a path that is 100% CPU is doing the work on the cores, one at 20% is
handing it to fixed-function silicon.

Full-resolution 1206x2622 simulator frame:

| path | wall | CPU | CPU/wall | size |
|---|---|---|---|---|
| ImageIO JPEG → 900px (**what the spike does now**) | 4.96 ms | 4.91 ms | **99%** | 44 KB |
| VideoToolbox H.264, full res | 7.02 ms | **0.48 ms** | 7% | 7 KB |

Like-for-like at 414x900, so both paths touch the same pixels:

| path | wall | CPU | CPU/wall | size |
|---|---|---|---|---|
| ImageIO JPEG | 0.99 ms | 0.99 ms | **100%** | 44 KB |
| VideoToolbox JPEG | 0.66 ms | **0.15 ms** | 23% | 51 KB |
| VideoToolbox H.264 | 1.58 ms | **0.26 ms** | 16% | 1 KB |

So: **nothing in the current encode path is offloaded.** ImageIO JPEG is
100% CPU, and that is the 9-20% per stream measured earlier.

The relevant silicon is not the GPU. `ioreg` on this machine shows
`AppleAVE2Driver` (H.264/HEVC encoder), `AppleAVD` (decoder) and
`AppleJPEGDriver` — fixed-function media blocks, separate from the GPU
cores. VideoToolbox drives them. Metal or any GPU-compute path would be
slower and less power-efficient than dedicated encoder silicon.

The GPU *is* already doing one job: the local simulator window sets
`layer.contents` to the IOSurface, so WindowServer composites it with no
copy and no encode. That path is free and does not need changing.

### Two offload options, in order of cost

1. **VideoToolbox JPEG, as a drop-in.** Same MJPEG bytes on the wire, same
   `<img>` on the client, nothing else changes — and CPU drops ~6.6x
   (0.99 → 0.15 ms/frame). Note `UsingHardwareAcceleratedVideoEncoder`
   reports **false** for the JPEG codec, yet CPU is 23% of wall, so the
   work is clearly leaving the cores regardless of what the flag claims.
   Do not trust that property for JPEG; measure instead.

2. **VideoToolbox H.264 for the farm.** 0.48 ms CPU per full-res frame is
   ~1.4% of one core at 30 fps, so 18 streams is roughly a quarter of a
   core against ~2.6 cores for the current path. It also solves the
   keyframe problem for the two iOS sources, which Android cannot:
   `kVTEncodeFrameOptionKey_ForceKeyFrame` mints an IDR on demand, so a
   late-joining viewer needs no process restart.

H.264 has *higher wall time* than JPEG while using a tenth of the CPU.
That is pipeline latency in the media engine, not slowness, and it does
not limit throughput — the cores are free during it, and concurrent
streams overlap.

### Result of doing (1)

Swapped in and A/B'd on the same booted simulator under identical driven
load, 15 fps cap, 900px, q0.6:

| encoder | CPU | RSS | delivered |
|---|---|---|---|
| ImageIO (`--imageio`) | 10.0% | 129 MB | 13.5 fps |
| VideoToolbox | **2.8%** | **66 MB** | 13.8 fps |

3.6x less CPU and half the memory at the same frame rate, with the wire
format unchanged — the client is still a bare `<img>`. Output verified by
decoding frames back: same 414x900, correct content, no artifacts.

Two things to know before tuning it:

- **The quality scales are not equivalent.** Both were asked for 0.6;
  ImageIO produced a 32 KB median frame and VideoToolbox 38 KB. Matching
  byte size means re-tuning the number, not reusing it.
- **Keep the CPU path.** A VTCompressionSession can fail to create, and
  the implementation falls back per-frame rather than dropping the frame.
  A preview that silently goes black is worse than one that quietly costs
  more CPU.

### Still CPU, still worth moving

- **Downscaling** is `CGContext.draw`, on the cores. Either hand it to
  `VTPixelTransferSession` or let the encoder output the target size and
  skip the separate scale entirely.
- **Downscaling is solved** by the VideoToolbox swap: the session resizes
  a mismatched input buffer, so the CGContext resize is gone.

### Physical iOS passthrough: investigated, not worth it

The device encodes H.264 in hardware, macOS decodes it in hardware, and we
encode again. Skipping both conversions looked like the biggest remaining
win. It is not reachable at acceptable cost.

The DAL device exposes exactly one format:

    mediaType=muxx  subType='isr '  0x0

Muxed, not video, with no dimensions — an opaque "iOS screen recording"
container. There is no `avc1` format to select, so AVFoundation demuxes
and decodes it internally and there is nothing to ask for instead.

`AVCaptureMovieFileOutput` was the obvious candidate for a passthrough
recorder. It is not one: recording cost **more** CPU than decoding to
BGRA (11.1% of a core against 8.3%) and produced H.264 Main profile at
1.8 Mbps, i.e. a re-encode.

Where the physical path's CPU actually goes, measured per device:

| stage | cost |
|---|---|
| AVFoundation demux + hardware decode to BGRA | 8.3% of a core |
| VideoToolbox JPEG encode | ~3.6% of a core |
| **full pipeline (VideoToolbox)** | **11.9%, 69 MB** |
| full pipeline (ImageIO, before the swap) | 24.4%, 115 MB |

True passthrough means going under AVFoundation to CoreMediaIO, taking
the stream's buffer queue directly, and demuxing `'isr '` ourselves — an
undocumented Apple container. The prize is ~12% of one core per device,
on a path where USB limits how many devices attach to one Mac anyway.
The VideoToolbox swap already took half of it for a fraction of the risk.
Not recommended unless the physical-device count ever gets large.

### MLX and v4l — neither applies

MLX is an array/ML framework over Metal. It has no capture or codec
functionality, and routing video encode through GPU compute would be
worse than the fixed-function blocks that already exist, on both speed
and power.

v4l2 is a *Linux kernel* capture API with no macOS equivalent. It shows
up in this project only because scrcpy's `--v4l2-sink` is Linux-only,
which is part of why Android previews cannot be reparented on macOS
(#127). There is nothing to port: AVFoundation and CoreMediaIO are the
macOS equivalents, and we are already on them.

## Not investigated

- Whether a hand-written remuxer hits acceptable latency (expected, not measured).
- WebCodecs as an MSE alternative.
- Multi-device fan-out: one server, N sources, `/stream/<id>` routing.
- Input injection over the same channel. sim-bridge already has iOS tap and
  swipe, so an interactive remote view is closer than it looks.
- Authentication. The spike binds loopback unless `--bind-all`, and
  `--bind-all` is unauthenticated. Anything remote needs a real answer here.
