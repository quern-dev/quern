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

var server: HTTPStreamServer?
if let port = options.servePort {
    let s = HTTPStreamServer(port: port, bindAll: options.bindAll, codec: options.codec) {
        pipeline.requestKeyframe()
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

// MARK: - lifetime

let shutdownOnce = ShutdownGuard {
    source.stop()
    server?.stop()
    if let summary = recording?.finish() {
        MediaLog.log(String(
            format: "[record] %d frames over %.2fs, %d dropped -> %@",
            summary.framesWritten, summary.duration,
            summary.framesDropped, summary.url.path
        ))
    }
    pipeline.invalidate()
}

/// Without this, a recording ended with ^C or `kill` is a file with no moov
/// atom — unopenable, not merely truncated. The default signal action has to
/// be ignored first, or the process dies before the handler runs.
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        shutdownOnce.run()
        exit(0)
    }
    src.resume()
    signalSources.append(src)
}

/// Runs a closure at most once, however many ways the process can end.
final class ShutdownGuard {
    private let body: () -> Void
    private var done = false
    private let lock = NSLock()

    init(_ body: @escaping () -> Void) { self.body = body }

    func run() {
        lock.lock()
        let already = done
        done = true
        lock.unlock()
        guard !already else { return }
        body()
    }
}

atexit_b { shutdownOnce.run() }

// A CFRunLoop rather than dispatchMain: CoreMediaIO publishes device changes
// through run-loop sources, and AVFoundation expects one on the main thread.
RunLoop.main.run()
