import Foundation
import QuernMedia

// A headless video producer. No AppKit, no bundle, no Dock presence, no
// window — showing frames to a person is the preview app's job, and this
// tool's job is to make frames available for it (or anything else) to
// consume over HTTP or on disk.
//
// Assembly only. Every decision worth testing lives in QuernMedia.

let usage = """
quern-media — headless screen capture for iOS simulators and devices.

Produces video. Does not display it: point a browser, ffplay, or the preview
app at the stream, or record to a file.

SOURCE (exactly one required)
  --sim-udid <UDID>      a booted simulator, no Simulator.app needed
  --device <name>        a USB-connected device, matched by name substring
  --list                 list connected capture devices and exit

OUTPUT (at least one required; they combine)
  --serve <port>         HTTP server; open http://127.0.0.1:<port>/
  --record <path>        write an .mp4. Implies --h264.

TUNING
  --fps <n>              max frames encoded per second (default 15)
  --max-dim <px>         downscale longest side (default 900, 0 = native)
  --h264                 H.264 instead of MJPEG. ~12x less data, but a
                         browser cannot play the raw stream — use ffplay.
  --bitrate <bps>        H.264 target bitrate (default 2000000)
  --quality <0..1>       JPEG quality (default 0.6). Not comparable to
                         ImageIO's scale — VideoToolbox runs larger.
  --bind-all             listen on all interfaces instead of loopback.
                         UNAUTHENTICATED: anyone on the network can watch.

EXAMPLES
  quern-media --sim-udid <UDID> --serve 8422
  quern-media --device "iPhone 11" --serve 8424 --fps 60
  quern-media --sim-udid <UDID> --record run.mp4
"""

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n\n".utf8))
    FileHandle.standardError.write(Data((usage + "\n").utf8))
    exit(2)
}

let options: Options
do {
    options = try OptionsParser.parse(Array(CommandLine.arguments.dropFirst()))
} catch OptionsError.help {
    print(usage)
    exit(0)
} catch OptionsError.list {
    let devices = CaptureDeviceDiscovery.waitForDevices()
    if devices.isEmpty {
        print("No iOS capture devices. Connect by USB, unlock, and trust the Mac.")
    } else {
        for (i, d) in devices.enumerated() { print("  [\(i)] \(d.localizedName)") }
    }
    exit(0)
} catch let error as OptionsError {
    fail(error.description)
} catch {
    fail("\(error)")
}

// MARK: - wiring

let pipeline = StreamPipeline(
    codec: options.codec, fps: options.fps, maxDimension: options.maxDimension,
    quality: options.quality, bitrate: options.bitrate
)

var recording: RecordingSink?
if let path = options.recordPath {
    do {
        let sink = RecordingSink(recorder: try Recorder(url: URL(fileURLWithPath: path)))
        pipeline.add(sink)
        recording = sink
        // The writer must open on a keyframe. The first frame of a fresh
        // session is one anyway, but ask rather than rely on it.
        pipeline.requestKeyframe()
        MediaLog.log("[record] \(path)")
    } catch {
        fail("cannot record to \(path): \(error)")
    }
}

/// Carries the source-priming call to the attach handler, which is built
/// before the source exists.
///
/// A box with a lock rather than a bare `var`: the server invokes the handler
/// on its own queue while this is assigned from the main thread, and an
/// unsynchronized capture is a data race that Swift 6 would reject outright.
final class AttachPrimer: @unchecked Sendable {
    private let lock = NSLock()
    private var prime: (() -> Void)?

    func install(_ body: @escaping () -> Void) {
        lock.lock(); prime = body; lock.unlock()
    }

    func fire() {
        lock.lock(); let body = prime; lock.unlock()
        body?()
    }
}

let attachPrimer = AttachPrimer()

var server: HTTPStreamServer?
if let port = options.servePort {
    let s = HTTPStreamServer(port: port, bindAll: options.bindAll, codec: options.codec) {
        // Unconditional, and deliberately not routed through the box. The
        // server starts accepting the moment it binds, while the source is
        // still being brought up -- dlopen, device resolution, callback
        // registration, and for a USB device a blocking wait on enumeration.
        // preview.py dials the port as soon as it is listening, so the first
        // viewer lands squarely in that window. Making this wait on the source
        // silently dropped the keyframe request that used to be live from the
        // moment the port bound.
        pipeline.requestKeyframe()
        attachPrimer.fire()
    }
    do { try s.start() } catch { fail("cannot bind port \(port): \(error)") }
    pipeline.add(s)
    server = s
}

var source: FrameSource
switch options.source {
case .simulator(let udid):
    source = SimulatorFramebuffer(udid: udid) { pipeline.consume($0) }
    do { try source.start() } catch { fail("\(error)") }
    MediaLog.log("[capture] streaming simulator \(udid)")

case .device(let match):
    MediaLog.log("[capture] waiting for capture devices...")
    let devices = CaptureDeviceDiscovery.waitForDevices()
    let needle = match.lowercased()
    guard let device = devices.first(where: {
        $0.localizedName.lowercased().contains(needle)
    }) else {
        if devices.isEmpty {
            fail("no capture devices — is the device connected by USB, unlocked and trusted?")
        }
        fail("no device matching \"\(match)\". Available: "
            + devices.map(\.localizedName).joined(separator: ", "))
    }
    source = CaptureDeviceSource(device: device) { pipeline.consume($0) }
    do { try source.start() } catch { fail("\(error)") }
    MediaLog.log("[capture] streaming \(device.localizedName)")
}

// A viewer that has just attached needs a picture, and on an event-driven
// source there may not be another one for a long time: an idle simulator
// composites nothing, so the window stayed black until the screen happened to
// change. Asking the source to re-deliver what is already on screen costs one
// encode and removes the wait.
// A viewer attaching before this point still gets a picture: `start()` primes
// the framebuffer itself, so the frame it delivers reaches the sink that just
// attached. This only covers viewers arriving after the source settles.
attachPrimer.install { source.requestCurrentFrame() }

// MARK: - lifetime

let shutdownOnce = ShutdownGuard { () -> Int32 in
    source.stop()
    server?.stop()

    var status: Int32 = 0
    if let recording {
        if let summary = recording.finish() {
            MediaLog.log(String(
                format: "[record] %d frames over %.2fs from host %.6f, "
                    + "%d dropped -> %@",
                summary.framesWritten, summary.duration, summary.startHostTime,
                summary.framesDropped, summary.url.path
            ))
        } else if case .alreadyFinished? = recording.failure {
            // Nothing to say: a previous finish already reported the outcome.
        } else if case .neverStarted? = recording.failure {
            // Not a failure. No keyframe ever reached the recorder, so there
            // is no file -- reporting that as a broken recording, and exiting
            // 1 for it, told the caller their capture was corrupt when there
            // simply was not one.
            //
            // An earlier version of this comment claimed ^C on an idle
            // simulator reaches it. Measured: it does not. `start()` primes
            // the framebuffer, so even a run interrupted after three seconds
            // on a still screen recorded 2 frames. This is the defensive case
            // -- a source that produces nothing at all before shutdown, which
            // a capture device with no signal can do.
            MediaLog.log("[record] nothing was recorded")
        } else {
            // A recording that could not be finalised is an unopenable file,
            // not a shorter one. Saying nothing and exiting 0 told the caller
            // it had a recording.
            let reason = recording.failure.map(String.init(describing:)) ?? "unknown"
            MediaLog.log("[record] could not finish the recording: \(reason)")
            status = 1
        }
    }
    pipeline.invalidate()
    return status
}

/// Without this, a recording ended with ^C or `kill` is a file with no moov
/// atom — unopenable, not merely truncated. The default signal action has to
/// be ignored first, or the process dies before the handler runs.
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        // The recording's fate decides the exit code. ^C on a recording that
        // could not be finalised is a failure, and it used to exit 0.
        exit(shutdownOnce.run())
    }
    src.resume()
    signalSources.append(src)
}


// Cleanup only. A callback here cannot change a status already being
// returned, so the signal handlers above are what carry a failure out.
atexit_b { shutdownOnce.run() }

// A CFRunLoop rather than dispatchMain: CoreMediaIO publishes device changes
// through run-loop sources, and AVFoundation expects one on the main thread.
RunLoop.main.run()
