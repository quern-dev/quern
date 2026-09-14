# Teaching a debug server to see: a screen-streaming spike

*A day spent on a problem that started as "can we even do this?" and ended
with hardware-accelerated video, sub-millisecond clock alignment, and a
twelvefold drop in bandwidth.*

---

## Where we started

Quern is a local debug server that gives AI coding agents access to iOS
simulators, Android emulators and physical devices. It could already take
screenshots. It could not show you a screen.

The live preview it did have was narrow, and every limitation came from one
architectural fact: **preview was built on CoreMediaIO capture devices.**

- **Physical iOS only, over USB.** A device on wifi does not appear as a
  capture device. Simulators never appear at all.
- **Simulators could not be previewed.** The most common target, invisible.
- **A local window and nothing else.** `AVCaptureVideoPreviewLayer` draws to
  the screen and hands back no pixels. Nothing could leave the machine.
- **No recording.** None.
- **Android was a separate world** — a `scrcpy` subprocess owning its own
  window, unmanageable and unreadable.
- **Everything on the CPU**, though nobody had measured it, because there
  was nothing to measure: no encode path existed.

The prompt that started it: *"They pipe simulator video feeds to their app,
leaving the simulators running headless. I'm curious if we can do that with
our screen preview app."*

## Where we landed

One capture-and-encode core, three sources, measured throughout.

| | before | after |
|---|---|---|
| simulator preview | impossible | headless, 60 fps |
| physical iOS | window only | window **and** stream |
| pixels off the machine | none | MJPEG or H.264 over HTTP |
| recording | none | .mp4, wall-clock accurate |
| JPEG encode CPU | 10.0% / core | **2.8%** |
| bandwidth (60 fps) | 15.8–20.6 Mbps | **0.57–2.2 Mbps** |
| delivered vs requested fps | 23 of 30 | **exactly 30.0** |
| 30 fps → 60 fps cost | — | **zero extra CPU** |
| device↔host clock alignment | unknown | **0.9 ms** |

Concretely, by the end:

- **Simulators stream headless** via CoreSimulator's framebuffer — no
  Simulator.app, event-driven, free when the screen is idle.
- **Physical iOS streams** through an `AVCaptureVideoDataOutput` added
  alongside the existing preview layer. One session, two consumers.
- **Both sources converge on `IOSurface`**, so one encoder serves both.
- **Hardware encode throughout**: JPEG 3.6× cheaper than ImageIO, H.264
  11.9× smaller than MJPEG at identical CPU and frame rate.
- **Recording to .mp4** with real timestamps, where a six-second idle stays
  six seconds instead of vanishing.
- **Keyframes on demand**, so a late viewer decodes immediately and a
  timeline can seek frame-accurately.
- **Android characterised** end to end, and correctly classified as a
  *transport* rather than a frame source.

That last row of the table — clock alignment — turned out to be the
difference between "a video" and "a video you can anchor a test timeline
to." It is also the thing I got most wrong.

---

## Step by step

### 1. Read the neighbour's homework

The reference was **baguette**, an open-source headless simulator manager.
Rather than guess, we read how it actually gets frames: CoreSimulator's
private API, `SimDevice.io` → `deviceIOPorts` → the
`com.apple.framebuffer.display` descriptor →
`registerScreenCallbacks…` → an `IOSurface` per composited frame.

Then the useful discovery: **quern already did most of this.** Its
`sim-bridge` already dlopened the same private frameworks and walked the
same path to grab one-shot screenshots. The delta was a continuous callback
instead of a poll.

The headless half was already done too — quern boots simulators with
`simctl boot`, never `open -a Simulator`. Confirmed by capturing a
1206×2622 frame from a simulator with no GUI attached.

**Assumption corrected:** this looked like new capability. It was mostly
subscribing to something we already knew how to read.

### 2. First frames, and a detail worth stealing

The spike registered the continuous callback and set `layer.contents` to the
IOSurface directly — zero copy, composited by WindowServer on the GPU. No
codec at all, because the frames never left the machine.

It worked first try: **63 fps under animation, ~0.2 fps idle.** Event-driven
capture means a static screen costs nothing.

One detail from baguette earned its place immediately: it registers on
*every* framebuffer descriptor and picks the largest live surface each tick.
Our screenshot path took the first match. The spike logged
`framebuffer descriptors: 2` on a booted iPhone — the concern was real, not
defensive.

### 3. Getting pixels off the machine

MJPEG first, deliberately. Every browser renders
`multipart/x-mixed-replace` from a bare `<img>` with no client code, which
makes it the cheapest possible proof the pipeline works.

Two simulators streamed concurrently — iPhone and iPad, both headless,
separate ports. Concurrent framebuffer subscription was the risky unknown
for a device farm; CoreSimulator handled it.

Then the first honest correction. A mid-stream iPad frame came through with
a correct status bar and a **black content area**. A ground-truth `simctl`
screenshot showed Settings rendering fine, so this was a real discrepancy —
possibly the largest-surface heuristic picking wrong. It wasn't: 102 settled
frames all came in ≥28 KB where an all-black JPEG would be ~3 KB, and the
aspect ratio matched throughout. A transient mid-crossfade, chased rather
than waved off.

### 4. Physical devices, and the format that would have ruined it

Adding physical iOS meant getting pixels from a path that had never
produced any. The existing code attaches only an
`AVCaptureVideoPreviewLayer`; adding an `AVCaptureVideoDataOutput` to the
*same session* yields sample buffers without disturbing the layer.

The part that made one encoder possible for both sources:
`CVPixelBufferGetIOSurface` on a capture buffer returns the same type the
simulator framebuffer hands over. **But only because the output pins
`videoSettings` to 32BGRA.** Left alone a DAL device negotiates YUV, which
the encoder would have read as BGRA and rendered as garbage. The device
reported `828x1792 px, format BGRA` — the pin held.

This also surfaced a bug the simulator had been hiding. The server encoded
asynchronously, which is fine for a framebuffer surface that persists, and
wrong for a capture buffer whose `IOSurface` belongs to a recycling pool and
can be reused the moment the delegate returns. Encoding moved onto the
caller's queue, synchronously. The simulator would never have revealed it.

### 5. "Is any of this using the GPU?"

This question changed the trajectory. The honest answer was almost none of
it, and finding out required measuring *CPU time against wall time* rather
than wall time alone.

On an M4, 1206×2622 source:

| path | wall | CPU | CPU/wall | size |
|---|---|---|---|---|
| ImageIO JPEG → 900px | 4.96 ms | 4.91 ms | **99%** | 44 KB |
| VideoToolbox H.264 | 7.02 ms | **0.48 ms** | 7% | 7 KB |

The naive reading — H.264 is *slower* — is exactly backwards. It uses a
tenth of the CPU; the extra wall time is media-engine pipeline latency
during which the cores are free.

`ioreg` confirmed the target isn't the GPU at all: `AppleAVE2Driver`,
`AppleAVD` and `AppleJPEGDriver` are fixed-function blocks, separate silicon
from the GPU cores. Metal or MLX would have been *worse* than the dedicated
encoder — a genuinely counterintuitive result, and the reason the follow-up
question about MLX and v4l deserved a direct "neither applies."

**The cheapest win was a drop-in.** VideoToolbox JPEG keeps the wire format
and the bare `<img>` client, and cut CPU 3.6× (10.0% → 2.8%) with half the
memory. One trap: it reports
`UsingHardwareAcceleratedVideoEncoder = false` for the JPEG codec while
running at 23% CPU-to-wall. Anything gating on that property would silently
fall back forever.

### 6. The frame rate we were asking for and not getting

Chasing a 23 fps ceiling against a 30 fps cap found a bug in our own code.
The throttle tested `now - lastEncode >= 1/fps` and reset `lastEncode` to
each frame's *arrival*, discarding the remainder. A source faster than the
target therefore quantises to an integer division of it: the iPhone was
delivering a full **60 fps** and we were aliasing away half of it.

Deadline scheduling fixed it — 30.0 fps exactly, and 60 at the higher cap.

Then the result that mattered: **60 fps costs the same CPU as 30.** Both
paths are decode-bound, so the encoder is nearly free and frame rate is not
a budget item. That only became visible because CPU-time measurement had
replaced `ps %cpu`, which had been reading roughly half the real value.

### 7. What motion actually costs

I reported that motion increases MJPEG bitrate by ~12%. A clean A/B at fixed
frame rate, with distinct-frame counts proving each run was what it claimed,
showed the opposite:

| run | distinct frames | median frame |
|---|---|---|
| static Settings | 6 of 768 | 43 KB |
| scrolled Settings | 414 of 672 | **32 KB** |

Motion frames are *smaller*. JPEG is intra-only, so size follows how complex
the picture is, not whether it changed — the static screen sat on a dense
list, scrolling passed through plainer rows. Bandwidth is set by frame rate,
resolution and quality, all of which we choose. H.264 behaves the opposite
way, which is why a farm sized for one codec cannot be rescaled to the other
by multiplying a number.

The first version of this claim came from comparing two runs that differed
in more than motion. The corrected one came from an experiment designed to
isolate it — and an earlier attempt at that same experiment had to be thrown
out entirely when both arms turned out to be static.

### 8. H.264, and the trap in the format

Same `VTCompressionSession` API, same hardware, but three real differences:
output is AVCC rather than Annex-B, frames depend on each other, and bitrate
becomes a target we set rather than an outcome.

Head to head on the USB iPhone at 60 fps under identical load:

| codec | CPU | delivered | bitrate | bytes/window |
|---|---|---|---|---|
| MJPEG | 10.0% | 43.9–49.4 fps | 15.8–20.6 Mbps | 29.6 MB |
| H.264 | 10.0% | 44.9–48.0 fps | **0.57–2.2 Mbps** | **2.5 MB** |

**11.9× less data at identical CPU, memory and frame rate.** An 18-device
farm goes from ~320 Mbps (LAN-only) to ~36 Mbps (an ordinary WAN link).

The first run crashed. Converting AVCC to Annex-B by loading each 4-byte
length as a `UInt32` traps on alignment, because every NAL advances the
cursor by an arbitrary payload size. Knowing the formats got the structure
right; only running it found that.

And the capability that came free: **we own the keyframes.**
`ForceKeyFrame` on client attach means a late viewer decodes immediately.
A capture shows IDRs at frames 0, 120, 240 … with parameter sets re-emitted
before each, so a viewer can join at any of them. Android's `screenrecord`
gives one IDR per session and no way to ask for another — the sharp contrast
between owning an encoder and borrowing one.

### 9. Recording, and why it came before the refactor

At this point the obvious move was to stop and extract a clean architecture.
We deliberately did recording first, for one specific reason: **the frame
callback carried no timestamp.**

Fine for streaming — frames go out as they arrive and nobody asks when.
Fatal for recording, and the two sources disagree about what they can even
report. Physical iOS has a real presentation timestamp from AVFoundation
that the code was discarding. The simulator framebuffer has none, so arrival
time is the best available: later and noisier than the true composite.

That asymmetry is the single most important signature in any frame-source
protocol, and *only recording forces the question*. Extracting first would
have designed the protocol around the one consumer that doesn't care about
time, then changed it immediately.

`AVAssetWriter` passthrough, validated against a driven run with a
deliberate six-second idle:

```
138 frames, first 0.00s last 13.73s
  5.57s gap starting at t=6.15s     <- the deliberate idle
median inter-frame gap while active: 33 ms (~30 fps)
```

The idle survives as real elapsed time. With the old synthetic PTS it would
have vanished — which is precisely the property a test timeline needs.

Three traps, each found by running it: a passthrough input cannot be added
without a `sourceFormatHint`, and that hint is the encoder's format
description, which doesn't exist until the first frame. Recording must start
on a keyframe or the file is undecodable. And SIGINT/SIGTERM must be
trapped, because an mp4 with no moov atom is *unopenable*, not short.

### 10. The clock, and the question that saved it

The last risk was whether device logs could be placed against video frames.
I measured it, got a **478 ms** uncertainty window, and wrote it up as
unsolved for physical devices — wider than a UI transition, so a timeline
could not order a log line against a visual change.

Then: *"Can the device stream its clock?"*

That reframed it from a clock problem to a transport problem. I had been
spawning a `pymobiledevice3` process per sample — measuring my own
subprocess overhead, not the device.

| method | round trip | offset bound | uncertainty |
|---|---|---|---|
| CLI, one process per read | ~490 ms | [−88.9, +389.6] ms | 478 ms |
| persistent connection | **2.2 ms** | **[−7.0, −6.1] ms** | **0.9 ms** |

**Sub-millisecond**, a ~530× improvement, drifting about 1 ms/min. Sample
every 30 s, interpolate, and alignment stays well inside a single frame at
60 fps. The suspiciously stable −90 ms lower bound the CLI kept producing
was pure artifact.

A wrong conclusion from a real measurement, corrected by one question that
challenged the method rather than the number.

---

## What actually made this work

**Measure the thing, not a proxy for it.** `ps %cpu` reported about half the
real CPU. Wall time made hardware H.264 look slower than software JPEG.
CPU-time-against-wall-time was the measurement that reorganised the whole
project — and the question that prompted it was "is any of this using the
GPU?"

**Prior knowledge picks the API; running it finds the truth.** Knowing the
frameworks got us to `VTCompressionSession`, `IOSurface` convergence and
AVCC-versus-Annex-B quickly. It did not predict that the hardware flag lies
for JPEG, that a passthrough writer input needs a format hint, or that
unaligned length loads trap. Every one of those cost a crash or a silent
wrong answer.

**Direction came from one side, momentum from the other.** Every turn that
moved this forward was a suggestion: offload to the media engine, use the
Apple TV app for motion, can the device stream its clock. What made them
pay off was not that the agent worked fast — it was that **the cost of
changing direction was close to zero.**

When the GPU question landed, there was no context-switch penalty: no
re-reading VideoToolbox documentation, no standing up new tooling, no
reluctance to abandon an MJPEG thread three commits deep. `ioreg` for the
media-engine hardware, a fresh benchmark harness, and a reorganised
understanding of the whole project followed within minutes. Same again on
the clock: from "this is unsolved" to a persistent-connection experiment in
a single step, no re-approach.

A human expert redirected mid-investigation pays real cost to switch. That
asymmetry is what the pairing exploits — high-information redirections
meeting near-zero pivot cost.

**And that is the same property that produced the wrong answers.** Cheap to
commit to a direction, cheap to abandon it. Roughly a third of the confident
statements in this project were wrong: motion increases bitrate (backwards),
USB contention kills the tunnel (disproved by a controlled test), clock skew
is unsolvable (method artifact), CPU is 5% (measurement artifact). Clock
skew was declared unsolved with exactly the same low friction that allowed
it to be overturned twenty minutes later.

That is not a flaw sitting beside the strength; it is one mechanism seen
from two sides. Which is precisely why the measurement discipline and the
course corrections were load-bearing rather than nice to have. None of those
claims survived an experiment designed to isolate them. The ones that
survived are in the table at the top.

**Know when to stop.** The spike ends here, at 2,600 lines in one file with
zero tests and four near-duplicate delegates. Everything uncertain has been
measured; what remains is known work. That is the moment to extract rather
than to add one more feature.
