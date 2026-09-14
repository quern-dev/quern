import AppKit
import Foundation
import QuernMedia

// Assembly only. Every decision worth testing lives in QuernMedia; this
// resolves a source, hangs sinks off a pipeline, and manages lifetime.

let usage = """
quern-media — live preview, streaming and recording for iOS screens.

Two sources, one encoder. Simulators come from CoreSimulator's framebuffer,
physical devices from a CoreMediaIO capture device; both arrive as IOSurface
and share the same encode path.

SOURCE (exactly one required)
  --sim-udid <UDID>      a booted simulator, no Simulator.app needed
  --device <name>        a USB-connected device, matched by name substring
  --list                 list connected capture devices and exit

OUTPUTS (at least one; they combine)
  (default)              a local window
  --no-window            no window
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
  quern-media --sim-udid <UDID> --serve 8422 --no-window
  quern-media --device "iPhone 11" --serve 8424 --no-window --fps 60
  quern-media --sim-udid <UDID> --record run.mp4 --no-window
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

let app = NSApplication.shared
// A headless run has nothing to put in the Dock, and a bouncing tile for a
// background streamer is noise.
app.setActivationPolicy(options.window ? .regular : .accessory)

final class Runner: NSObject, NSApplicationDelegate {
    let options: Options
    var source: FrameSource?
    var pipeline: StreamPipeline?
    var server: HTTPStreamServer?
    var recording: RecordingSink?
    var surfaceWindow: SurfacePreviewWindow?
    var sessionWindow: CaptureSessionWindow?
    var signals: [DispatchSourceSignal] = []

    init(options: Options) { self.options = options }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let pipeline = StreamPipeline(
            codec: options.codec, fps: options.fps,
            maxDimension: options.maxDimension,
            quality: options.quality, bitrate: options.bitrate
        )
        self.pipeline = pipeline

        if let path = options.recordPath {
            do {
                let sink = RecordingSink(recorder: try Recorder(url: URL(fileURLWithPath: path)))
                pipeline.add(sink)
                recording = sink
                // The writer must open on a keyframe. The first frame of a
                // fresh session is one anyway, but ask rather than rely on it.
                pipeline.requestKeyframe()
                MediaLog.log("[record] \(path)")
            } catch {
                fail("cannot record to \(path): \(error)")
            }
        }

        if let port = options.servePort {
            let server = HTTPStreamServer(
                port: port, bindAll: options.bindAll, codec: options.codec
            ) { [weak pipeline] in pipeline?.requestKeyframe() }
            do { try server.start() } catch { fail("cannot bind port \(port): \(error)") }
            pipeline.add(server)
            self.server = server
        }

        switch options.source {
        case .simulator(let udid):
            startSimulator(udid: udid, pipeline: pipeline)
        case .device(let match):
            startDevice(match: match, pipeline: pipeline)
        }

        installSignalHandlers()
    }

    private func startSimulator(udid: String, pipeline: StreamPipeline) {
        if options.window {
            let window = SurfacePreviewWindow(title: "Simulator \(udid.prefix(8))")
            window.onClose = { NSApplication.shared.terminate(nil) }
            surfaceWindow = window
        }
        let source = SimulatorFramebuffer(udid: udid) { [weak self] frame in
            self?.surfaceWindow?.present(frame.surface)
            pipeline.consume(frame)
        }
        do {
            try source.start()
            self.source = source
            MediaLog.log("[capture] streaming simulator \(udid)")
        } catch {
            fail("\(error)")
        }
    }

    private func startDevice(match: String, pipeline: StreamPipeline) {
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

        let source = CaptureDeviceSource(device: device) { frame in
            pipeline.consume(frame)
        }
        do { try source.start() } catch { fail("\(error)") }
        self.source = source

        if options.window {
            let window = CaptureSessionWindow(
                title: device.localizedName, session: source.session
            )
            window.onClose = { NSApplication.shared.terminate(nil) }
            sessionWindow = window
        }
        MediaLog.log("[capture] streaming \(device.localizedName)")
    }

    /// Without this, a recording ended with ^C or `kill` is a file with no
    /// moov atom — unopenable, not merely truncated. The default action has to
    /// be ignored first or the process dies before the handler runs.
    private func installSignalHandlers() {
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                self?.shutdown()
                exit(0)
            }
            source.resume()
            signals.append(source)
        }
    }

    func applicationWillTerminate(_ notification: Notification) { shutdown() }

    private var didShutDown = false
    private func shutdown() {
        guard !didShutDown else { return }
        didShutDown = true
        source?.stop()
        server?.stop()
        if let summary = recording?.finish() {
            MediaLog.log(String(
                format: "[record] %d frames over %.2fs, %d dropped -> %@",
                summary.framesWritten, summary.duration,
                summary.framesDropped, summary.url.path
            ))
        }
        pipeline?.invalidate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool {
        options.window
    }
}

let runner = Runner(options: options)
app.delegate = runner
if options.window { app.activate(ignoringOtherApps: true) }
app.run()
