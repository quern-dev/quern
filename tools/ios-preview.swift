#!/usr/bin/env swift
// ios-preview: live preview and streaming for iOS screens.
//
// Three sources, one encoder. Simulators come from CoreSimulator's
// framebuffer, physical devices from a CoreMediaIO capture device; both
// arrive as IOSurface and share the same encode path.
//
// Run with --help for the full usage. The text lives in `usageText` below
// rather than in this comment, so there is exactly one copy of it.
//
// Build:
//   swiftc -O -o tools/ios-preview tools/ios-preview.swift \
//     -framework AVFoundation -framework CoreMediaIO \
//     -framework AppKit -framework VideoToolbox

import AVFoundation
import AppKit
import CoreMediaIO
import Foundation
import IOSurface
import Network
import ObjectiveC
import VideoToolbox

// MARK: - Enable iOS screen capture device discovery

func enableScreenCaptureDevices() {
    var prop = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain)
    )
    var allow: UInt32 = 1
    CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &prop, 0, nil, UInt32(MemoryLayout<UInt32>.size), &allow)
}

// MARK: - Discover iOS devices

/// A connected iPhone publishes more than its screen. On a device that
/// supports Continuity Camera, its rear camera arrives as a second
/// `.external` device, and previewing it opened a window showing a black
/// rectangle -- the camera is not streaming and nobody asked to see it.
///
/// The two are cleanly distinguishable, but not by the obvious route: a
/// `.continuityCamera` discovery session returns nothing, because the camera
/// is published as a plain external device. What separates them is what they
/// carry. A screen-capture device is muxed (audio and video together) and
/// reports the generic `iOS Device` model; a Continuity Camera is video-only
/// and reports the actual hardware model, e.g. `iPhone16,1`.
///
/// Measured with an iPhone 15 Pro and an iPhone 11 attached:
///
///     external/muxed:  J iPhone 15 Pro         modelID=iOS Device   muxed
///                      iPhone 11               modelID=iOS Device   muxed
///     external/video:  J iPhone 15 Pro Camera  modelID=iPhone16,1   video
private let screenCaptureModelID = "iOS Device"

func isScreenCaptureDevice(_ d: AVCaptureDevice) -> Bool {
    // The model ID is the whole assertion, on either media type. Accepting
    // any muxed external device was looser than the claim above it: muxed
    // means "audio and video together", which an unrelated capture device can
    // also be, and one of those would have opened a preview window. Media
    // type is not checked at all now -- screen-capture devices are muxed on
    // every macOS this has run on, but a host that typed them as video would
    // still be recognised by model.
    return d.modelID == screenCaptureModelID
}

func discoverDevices() -> [AVCaptureDevice] {
    let muxed = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.external],
        mediaType: .muxed,
        position: .unspecified
    ).devices

    let videoOnly = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.external],
        mediaType: .video,
        position: .unspecified
    ).devices

    var seen = Set<String>()
    var result: [AVCaptureDevice] = []
    for d in muxed + videoOnly {
        guard seen.insert(d.uniqueID).inserted else { continue }
        if isScreenCaptureDevice(d) {
            result.append(d)
        } else {
            // Say what was turned away and why. The filter runs before any
            // other logging, so without this a device rejected for an
            // unexpected model ID -- a future macOS reporting something other
            // than "iOS Device" -- would look exactly like no device at all.
            fputs("  ignoring \(d.localizedName) (model \(d.modelID))\n", stderr)
        }
    }
    return result
}

// MARK: - Device attach / detach

/// AVFoundation posts both notifications for CoreMediaIO screen-capture
/// devices, verified by observing an unplug/replug of an iPhone 11:
///
///     DISCONNECTED name=iPhone 11 model=iOS Device muxed=true
///     CONNECTED    name=iPhone 11 model=iOS Device muxed=true
///
/// The notification carries the device itself, so `isScreenCaptureDevice`
/// applies directly and a Continuity Camera appearing alongside its phone is
/// filtered out here too, rather than being noticed later.
func observeDeviceChanges(
    onConnect: @escaping (AVCaptureDevice) -> Void,
    onDisconnect: @escaping (AVCaptureDevice) -> Void
) -> [NSObjectProtocol] {
    let connected = NotificationCenter.default.addObserver(
        forName: AVCaptureDevice.wasConnectedNotification, object: nil, queue: .main
    ) { note in
        guard let d = note.object as? AVCaptureDevice, isScreenCaptureDevice(d) else { return }
        traceDeviceEvent("attach", d)
        onConnect(d)
    }
    let disconnected = NotificationCenter.default.addObserver(
        forName: AVCaptureDevice.wasDisconnectedNotification, object: nil, queue: .main
    ) { note in
        guard let d = note.object as? AVCaptureDevice, isScreenCaptureDevice(d) else { return }
        traceDeviceEvent("detach", d)
        onDisconnect(d)
    }
    return [connected, disconnected]
}

/// Every attach and detach as AVFoundation reports it, before any policy is
/// applied. One line per event, on stderr, because what the window ends up
/// doing is a decision layered on top -- and when the two disagree, this is
/// the record that says which half was surprising.
func traceDeviceEvent(_ kind: String, _ device: AVCaptureDevice) {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss.SSS"
    fputs("  [\(f.string(from: Date()))] \(kind): \(device.localizedName)\n", stderr)
}

// MARK: - Filter devices by args

/// Everything the simulator preview path needs. A struct rather than more
/// enum payload because the flag list is already past the point where
/// positional associated values stay readable.
struct StreamTuning {
    let window: Bool
    let serve: Bool
    let port: UInt16
    let bindAll: Bool
    let fps: Double
    let maxDimension: Int
    let quality: Double
    /// Force the CPU encoder. Diagnostic escape hatch and A/B lever.
    let useImageIO: Bool
    /// H.264 instead of MJPEG. Cuts bitrate by roughly an order of magnitude
    /// but costs the bare-<img> client -- the stream needs a decoder.
    let useH264: Bool
    let bitrate: Int
    /// Record to this path. Implies H.264 — an mp4 wants a video codec.
    let recordPath: String?
}

struct SimOptions {
    let udid: String
    /// Use the CGImage copy-through render path instead of handing the
    /// IOSurface to CALayer directly.
    let viaCGImage: Bool
    let tuning: StreamTuning
}

struct DeviceStreamOptions {
    /// Case-insensitive substring of the capture device's localized name.
    let match: String
    let tuning: StreamTuning
}

enum FilterMode {
    case all
    case listOnly
    case interactive
    case simUDID(SimOptions)
    case deviceStream(DeviceStreamOptions)
    case byArgs([String])
}

let usageText = """
ios-preview: live preview and streaming for iOS screens.

Three sources, one encoder. Simulators come from CoreSimulator's
framebuffer, physical devices from a CoreMediaIO capture device; both
arrive as IOSurface and share the same encode path.

LOCAL WINDOW
  ios-preview                        preview every connected device
  ios-preview --list                 list capture devices and exit
  ios-preview "iPhone 11"            preview devices matching a substring
  ios-preview 0 2                    preview devices by index
  ios-preview --interactive          JSON Lines protocol on stdin/stdout
  ios-preview --sim-udid <UDID>      preview a booted simulator, headless

STREAMING  (add --serve to either source; --no-window for headless)
  ios-preview --sim-udid <UDID> --serve 8422 --no-window
  ios-preview --device "iPhone 11" --serve 8424 --no-window

  MJPEG by default: open http://127.0.0.1:<port>/ in any browser, or
  curl http://127.0.0.1:<port>/stream. No client-side code needed.

  With --h264 the /stream endpoint serves a raw Annex-B elementary
  stream instead. Roughly 12x less data, but a browser cannot play it
  directly -- use ffplay/ffprobe, or pipe it to ffmpeg.

FLAGS
  --serve <port>     start the HTTP server (default port 8422)
  --bind-all         listen on all interfaces instead of loopback.
                     UNAUTHENTICATED -- anyone on the network can watch.
  --no-window        stream only, open no local window
  --fps <n>          max frames encoded per second (default 15)
  --max-dim <px>     downscale longest side (default 900, 0 = native)
  --quality <0..1>   JPEG quality (default 0.6). Not comparable to
                     ImageIO's scale -- VideoToolbox runs larger.
  --h264             H.264 elementary stream instead of MJPEG
  --bitrate <bps>    H.264 target bitrate (default 2000000)
  --imageio          encode JPEG on the CPU via ImageIO instead of
                     VideoToolbox. ~6x more CPU; diagnostic A/B lever.
  --cgimage          simulator window only: render frames through a
                     CGImage copy instead of handing the IOSurface to
                     CALayer. Fallback if the direct path shows nothing.

EXAMPLES
  # headless simulator, H.264, watch with ffplay
  ios-preview --sim-udid <UDID> --serve 8422 --no-window --h264 \
    & ffplay -fflags nobuffer http://127.0.0.1:8422/stream

  # physical device, MJPEG at 60fps, viewable in a browser
  ios-preview --device "iPhone 11" --serve 8424 --no-window --fps 60

Build:
  swiftc -O -o tools/ios-preview tools/ios-preview.swift \
    -framework AVFoundation -framework CoreMediaIO \
    -framework AppKit -framework VideoToolbox
"""

func printUsageAndExit() -> Never {
    print(usageText)
    exit(0)
}

func parseArgs() -> FilterMode {
    let args = Array(CommandLine.arguments.dropFirst())
    if args.contains("--help") || args.contains("-h") { printUsageAndExit() }
    if args.isEmpty { return .all }
    if args.contains("--list") || args.contains("-l") { return .listOnly }
    if args.contains("--interactive") { return .interactive }
    if args.contains("--sim-udid") || args.contains("--device") {
        func value(_ flag: String) -> String? {
            guard let j = args.firstIndex(of: flag), j + 1 < args.count else { return nil }
            let next = args[j + 1]
            return next.hasPrefix("--") ? nil : next
        }
        let tuning = StreamTuning(
            window: !args.contains("--no-window"),
            serve: args.contains("--serve"),
            port: value("--serve").flatMap(UInt16.init) ?? 8422,
            bindAll: args.contains("--bind-all"),
            fps: value("--fps").flatMap(Double.init) ?? 15,
            maxDimension: value("--max-dim").flatMap(Int.init) ?? 900,
            quality: value("--quality").flatMap(Double.init) ?? 0.6,
            useImageIO: args.contains("--imageio"),
            useH264: args.contains("--h264") || value("--record") != nil,
            bitrate: value("--bitrate").flatMap(Int.init) ?? 2_000_000,
            recordPath: value("--record")
        )
        if let udid = value("--sim-udid") {
            return .simUDID(SimOptions(
                udid: udid, viaCGImage: args.contains("--cgimage"), tuning: tuning
            ))
        }
        if let match = value("--device") {
            return .deviceStream(DeviceStreamOptions(match: match, tuning: tuning))
        }
    }
    return .byArgs(args)
}

func filterDevices(_ devices: [AVCaptureDevice], args: [String]) -> [AVCaptureDevice] {
    var result: [AVCaptureDevice] = []
    for arg in args {
        // Try as index first
        if let idx = Int(arg), idx >= 0, idx < devices.count {
            if !result.contains(where: { $0.uniqueID == devices[idx].uniqueID }) {
                result.append(devices[idx])
            }
        } else {
            // Match as name substring (case-insensitive)
            let lower = arg.lowercased()
            for d in devices {
                if d.localizedName.lowercased().contains(lower) {
                    if !result.contains(where: { $0.uniqueID == d.uniqueID }) {
                        result.append(d)
                    }
                }
            }
        }
    }
    return result
}

// MARK: - Menu bar setup

func setupMenuBar(devicesMenuDelegate: DevicesMenuDelegate, quitTarget: AnyObject? = nil, quitAction: Selector = #selector(NSApplication.terminate(_:))) {
    let mainMenu = NSMenu()

    // App menu
    let appMenuItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "About Quern Preview", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    let quitItem = NSMenuItem(title: "Quit Quern Preview", action: quitAction, keyEquivalent: "q")
    quitItem.target = quitTarget
    appMenu.addItem(quitItem)
    appMenuItem.submenu = appMenu
    mainMenu.addItem(appMenuItem)

    // Devices menu
    let devicesMenuItem = NSMenuItem()
    let devicesMenu = NSMenu(title: "Devices")
    devicesMenu.delegate = devicesMenuDelegate
    devicesMenuItem.submenu = devicesMenu
    mainMenu.addItem(devicesMenuItem)

    // Window menu
    let windowMenuItem = NSMenuItem()
    let windowMenu = NSMenu(title: "Window")
    windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
    windowMenu.addItem(withTitle: "Zoom", action: #selector(NSWindow.zoom(_:)), keyEquivalent: "")
    windowMenu.addItem(.separator())
    windowMenu.addItem(withTitle: "Bring All to Front", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: "")
    windowMenuItem.submenu = windowMenu
    mainMenu.addItem(windowMenuItem)

    NSApplication.shared.mainMenu = mainMenu
    NSApplication.shared.windowsMenu = windowMenu
}

func makeRoundedIcon(_ image: NSImage) -> NSImage {
    let canvas = NSSize(width: 512, height: 512)
    let inset: CGFloat = canvas.width * 0.1  // ~10% padding on each side
    let iconRect = NSRect(x: inset, y: inset, width: canvas.width - inset * 2, height: canvas.height - inset * 2)
    let radius = iconRect.width * 0.2237  // macOS icon corner radius ratio
    let result = NSImage(size: canvas)
    result.lockFocus()
    NSBezierPath(roundedRect: iconRect, xRadius: radius, yRadius: radius).addClip()
    image.draw(in: iconRect, from: .zero, operation: .sourceOver, fraction: 1.0)
    result.unlockFocus()
    return result
}

func loadAppIcon() {
    // Try bundle Resources first (when running inside .app bundle),
    // then fall back to icon next to the binary
    let candidates = [
        Bundle.main.resourcePath.map { $0 + "/AppIcon.png" },
        Bundle.main.executablePath.map { (($0 as NSString).deletingLastPathComponent as NSString).appendingPathComponent("../Resources/AppIcon.png") },
    ].compactMap { $0 }

    for path in candidates {
        if let icon = NSImage(contentsOfFile: path) {
            NSApplication.shared.applicationIconImage = makeRoundedIcon(icon)
            return
        }
    }
}

// MARK: - PreviewController protocol

protocol PreviewController: AnyObject {
    var allDevices: [AVCaptureDevice] { get set }
    var activeDeviceNames: Set<String> { get }
    func togglePreview(name: String, position: Int)
    func nextPosition() -> Int
}

// MARK: - Devices menu delegate

class DevicesMenuDelegate: NSObject, NSMenuDelegate {
    weak var controller: PreviewController?

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        guard let controller = controller else { return }

        // Re-discover devices every time the menu opens — AVCaptureDevice
        // references go stale after capture sessions are torn down.
        enableScreenCaptureDevices()
        controller.allDevices = discoverDevices()
        let devices = controller.allDevices
        if devices.isEmpty {
            let noDevices = NSMenuItem(title: "No Devices Found", action: nil, keyEquivalent: "")
            noDevices.isEnabled = false
            menu.addItem(noDevices)
        } else {
            for device in devices {
                let item = NSMenuItem(title: device.localizedName, action: #selector(DevicesMenuDelegate.toggleDevice(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = device.localizedName
                if controller.activeDeviceNames.contains(device.localizedName) {
                    item.state = .on
                }
                menu.addItem(item)
            }
        }

        menu.addItem(.separator())
        let refreshItem = NSMenuItem(title: "Refresh Devices", action: #selector(DevicesMenuDelegate.refreshDevices(_:)), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)
    }

    @objc func toggleDevice(_ sender: NSMenuItem) {
        guard let controller = controller,
              let name = sender.representedObject as? String else { return }
        controller.togglePreview(name: name, position: controller.nextPosition())
    }

    @objc func refreshDevices(_ sender: NSMenuItem) {
        guard let controller = controller else { return }
        enableScreenCaptureDevices()
        controller.allDevices = discoverDevices()
    }
}

// MARK: - Stream-aspect window sizing

/// Resizes a window to its stream's own proportions once they are known.
///
/// Shared because there are two preview classes -- `PreviewWindow` for the
/// standalone app and `PreviewSession` for server-driven interactive mode --
/// and they are near-duplicates. The first version of this fix went into one
/// of them, so previews opened through the MCP tool kept their bars while the
/// menu-bar ones did not. Owning the behaviour in one place is what stops
/// that recurring.
///
/// A window opens at a fixed size because nothing better is available yet: a
/// screen-capture device advertises a single format of 0x0 until it is
/// actually streaming, so its real size cannot be read at construction time.
/// The default 400x710 is 0.563 wide-to-tall while a modern iPhone is nearer
/// 0.462, and `videoGravity = .resizeAspect` letterboxes the difference into
/// black bars down each side.
final class StreamAspectSizer {
    private weak var window: NSWindow?
    private let input: AVCaptureDeviceInput?
    private var timer: Timer?

    init(window: NSWindow, input: AVCaptureDeviceInput?) {
        self.window = window
        self.input = input
    }

    /// Poll the input port until it reports real dimensions, then match them.
    /// Polling rather than KVO because the wait is short, bounded, and this is
    /// a single-file script; an observer would be more ceremony than the
    /// problem deserves.
    func begin() {
        cancel()
        var attempts = 0
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] t in
            guard let self else { t.invalidate(); return }
            attempts += 1
            if let dims = self.streamDimensions() {
                t.invalidate()
                self.timer = nil
                self.apply(dims)
            } else if attempts >= 40 {
                // ~10s. Keep the default window rather than retrying forever;
                // a device that never reports dimensions still mirrors fine,
                // it just keeps the bars.
                t.invalidate()
                self.timer = nil
            }
        }
    }

    func cancel() {
        timer?.invalidate()
        timer = nil
    }

    private func streamDimensions() -> CMVideoDimensions? {
        // A muxed device exposes separate audio and video ports; only the
        // video one carries the picture size.
        guard let port = input?.ports.first(where: { $0.mediaType == .video }),
              let desc = port.formatDescription else { return nil }
        let dims = CMVideoFormatDescriptionGetDimensions(desc)
        return (dims.width > 0 && dims.height > 0) ? dims : nil
    }

    private func apply(_ dims: CMVideoDimensions) {
        guard let window else { return }
        let aspect = CGFloat(dims.width) / CGFloat(dims.height)
        guard aspect.isFinite, aspect > 0 else { return }
        let contentHeight = window.contentView?.frame.height ?? 710
        let newWidth = (contentHeight * aspect).rounded()
        window.setContentSize(NSSize(width: newWidth, height: contentHeight))
        // Hold the ratio through any later user resize, so the bars cannot
        // come back by dragging a corner.
        window.contentAspectRatio = NSSize(
            width: CGFloat(dims.width), height: CGFloat(dims.height)
        )
    }
}

// MARK: - Preview window

class PreviewWindow: NSObject, NSWindowDelegate {
    let window: NSWindow
    let session: AVCaptureSession
    let device: AVCaptureDevice
    var onWindowClosed: ((String) -> Void)?
    private var input: AVCaptureDeviceInput?
    private var sizer: StreamAspectSizer?

    init(device: AVCaptureDevice, index: Int) {
        self.device = device
        self.session = AVCaptureSession()

        session.beginConfiguration()
        do {
            let input = try AVCaptureDeviceInput(device: device)
            if session.canAddInput(input) {
                session.addInput(input)
                self.input = input
            } else {
                fputs("  Warning: canAddInput returned false for \(device.localizedName)\n", stderr)
            }
        } catch {
            fputs("  Error adding input for \(device.localizedName): \(error)\n", stderr)
        }
        session.commitConfiguration()

        let screenFrame = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let windowWidth: CGFloat = 400
        let windowHeight: CGFloat = 710
        let xOffset = CGFloat(index) * (windowWidth + 20) + 50
        let yOffset = screenFrame.height - windowHeight - 80

        let frame = NSRect(x: xOffset, y: yOffset, width: windowWidth, height: windowHeight)

        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = device.localizedName
        window.isReleasedWhenClosed = false

        let previewLayer = AVCaptureVideoPreviewLayer(session: session)
        previewLayer.videoGravity = .resizeAspect
        previewLayer.frame = NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight)
        previewLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]

        let view = NSView(frame: NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight))
        view.wantsLayer = true
        view.layer?.addSublayer(previewLayer)
        window.contentView = view

        super.init()
        sizer = StreamAspectSizer(window: window, input: input)
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    func start() {
        session.startRunning()
        sizer?.begin()
    }

    func stop() {
        sizer?.cancel()
        session.stopRunning()
        window.delegate = nil
        window.close()
    }

    func windowWillClose(_ notification: Notification) {
        sizer?.cancel()
        session.stopRunning()
        onWindowClosed?(device.localizedName)
    }
}

// MARK: - App delegate (standalone mode)

class AppDelegate: NSObject, NSApplicationDelegate, PreviewController {
    var allDevices: [AVCaptureDevice] = []
    var activePreviews: [String: PreviewWindow] = [:]
    let devicesMenuDelegate = DevicesMenuDelegate()
    let mode: FilterMode
    private var deviceObservers: [NSObjectProtocol] = []

    var activeDeviceNames: Set<String> {
        return Set(activePreviews.keys)
    }

    init(mode: FilterMode) {
        self.mode = mode
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        ProcessInfo.processInfo.processName = "Quern Preview"
        loadAppIcon()
        devicesMenuDelegate.controller = self
        setupMenuBar(devicesMenuDelegate: devicesMenuDelegate)
        enableScreenCaptureDevices()
        watchForDeviceChanges()
        fputs("Waiting for devices...\n", stderr)

        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
            self.onDevicesReady()
        }
    }

    private func watchForDeviceChanges() {
        deviceObservers = observeDeviceChanges(
            onConnect: { [weak self] device in self?.deviceAppeared(device) },
            onDisconnect: { [weak self] device in self?.deviceVanished(device) }
        )
    }

    /// Unplugging a phone left its window on screen forever, showing a frozen
    /// last frame and holding a session bound to a device that no longer
    /// exists -- which also meant replugging could not attach to it, because
    /// the name was still taken.
    private func deviceVanished(_ device: AVCaptureDevice) {
        let name = device.localizedName
        allDevices.removeAll { $0.uniqueID == device.uniqueID }
        guard let preview = activePreviews[name] else { return }
        fputs("  \(name) disconnected — closing its window\n", stderr)
        preview.onWindowClosed = nil  // we are already removing it
        preview.stop()
        activePreviews.removeValue(forKey: name)
    }

    /// Plugging a phone in opens its window, so the app keeps showing what is
    /// attached rather than a snapshot of whatever was attached at launch.
    ///
    /// Honours the launch filter: started with no arguments means "everything",
    /// so anything new qualifies, but `ios-preview "iPhone 11"` asked for one
    /// device and must not sprout windows for the rest. List mode never gets
    /// here -- it prints and exits.
    private func deviceAppeared(_ device: AVCaptureDevice) {
        let name = device.localizedName
        if !allDevices.contains(where: { $0.uniqueID == device.uniqueID }) {
            allDevices.append(device)
        }

        guard activePreviews[name] == nil else { return }

        switch mode {
        case .all:
            break
        case .byArgs(let args):
            guard !filterDevices([device], args: args).isEmpty else { return }
        case .listOnly, .interactive, .simUDID, .deviceStream:
            // Simulator mode never runs through AppDelegate -- it has no
            // capture devices to hot-plug -- but the switch must cover it.
            return
        }

        fputs("  \(name) connected — opening its window\n", stderr)
        togglePreview(name: name, position: nextPosition())
    }

    func onDevicesReady() {
        allDevices = discoverDevices()

        if allDevices.isEmpty {
            fputs("No iOS devices found.\n", stderr)
            fputs("Make sure your iPhone is connected via USB, unlocked, and trusted.\n", stderr)
            NSApplication.shared.terminate(nil)
            return
        }

        // List mode: print and exit
        if case .listOnly = mode {
            print("Connected iOS screen capture devices:")
            for (i, d) in allDevices.enumerated() {
                print("  [\(i)] \(d.localizedName)  (id: \(d.uniqueID))")
            }
            NSApplication.shared.terminate(nil)
            return
        }

        // Filter devices
        let devices: [AVCaptureDevice]
        if case .byArgs(let args) = mode {
            devices = filterDevices(allDevices, args: args)
            if devices.isEmpty {
                fputs("No devices matched your filter. Available devices:\n", stderr)
                for (i, d) in allDevices.enumerated() {
                    fputs("  [\(i)] \(d.localizedName)\n", stderr)
                }
                NSApplication.shared.terminate(nil)
                return
            }
        } else {
            devices = allDevices
        }

        print("Opening preview for \(devices.count) device(s):")
        for (i, device) in devices.enumerated() {
            print("  \(device.localizedName)")
            fputs("  Creating preview window for \(device.localizedName) (index \(i))...\n", stderr)
            let preview = PreviewWindow(device: device, index: i)
            preview.onWindowClosed = { [weak self] name in
                self?.activePreviews.removeValue(forKey: name)
            }
            activePreviews[device.localizedName] = preview
            fputs("  Preview window created for \(device.localizedName)\n", stderr)
        }
        // Stagger session starts to avoid CoreMediaIO race conditions
        let names = devices.map { $0.localizedName }
        startNextSession(names: names, index: 0)
        print("Close all windows or Ctrl+C to quit.")
    }

    func startNextSession(names: [String], index: Int) {
        guard index < names.count, let preview = activePreviews[names[index]] else { return }
        preview.start()
        if index + 1 < names.count {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                self.startNextSession(names: names, index: index + 1)
            }
        }
    }

    func togglePreview(name: String, position: Int) {
        if let preview = activePreviews[name] {
            preview.onWindowClosed = nil
            preview.stop()
            activePreviews.removeValue(forKey: name)
        } else {
            guard let device = allDevices.first(where: { $0.localizedName == name }) else { return }
            let preview = PreviewWindow(device: device, index: position)
            preview.onWindowClosed = { [weak self] name in
                self?.activePreviews.removeValue(forKey: name)
            }
            activePreviews[name] = preview
            preview.start()
        }
    }

    func nextPosition() -> Int {
        var pos = 0
        let used = Set(activePreviews.values.map { Int(($0.window.frame.origin.x - 50) / 420) })
        while used.contains(pos) { pos += 1 }
        return pos
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false  // User quits via ⌘Q or menu
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        return true
    }
}

// MARK: - Interactive mode: PreviewSession

class PreviewSession: NSObject, NSWindowDelegate {
    let deviceName: String
    let window: NSWindow
    let session: AVCaptureSession
    var onWindowClosed: ((String) -> Void)?
    private var input: AVCaptureDeviceInput?
    private var sizer: StreamAspectSizer?

    init(device: AVCaptureDevice, position: Int) {
        self.deviceName = device.localizedName
        self.session = AVCaptureSession()

        session.beginConfiguration()
        do {
            let input = try AVCaptureDeviceInput(device: device)
            if session.canAddInput(input) {
                session.addInput(input)
                self.input = input
            }
        } catch {
            // Error handled by caller checking session inputs
        }
        session.commitConfiguration()

        let screenFrame = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let windowWidth: CGFloat = 400
        let windowHeight: CGFloat = 710
        let xOffset = CGFloat(position) * (windowWidth + 20) + 50
        let yOffset = screenFrame.height - windowHeight - 80

        let frame = NSRect(x: xOffset, y: yOffset, width: windowWidth, height: windowHeight)

        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = device.localizedName
        window.isReleasedWhenClosed = false

        let previewLayer = AVCaptureVideoPreviewLayer(session: session)
        previewLayer.videoGravity = .resizeAspect
        previewLayer.frame = NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight)
        previewLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]

        let view = NSView(frame: NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight))
        view.wantsLayer = true
        view.layer?.addSublayer(previewLayer)
        window.contentView = view

        super.init()
        sizer = StreamAspectSizer(window: window, input: input)
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    func start() {
        session.startRunning()
        sizer?.begin()
    }

    func stop() {
        sizer?.cancel()
        session.stopRunning()
        window.delegate = nil
        window.close()
    }

    func windowWillClose(_ notification: Notification) {
        sizer?.cancel()
        session.stopRunning()
        onWindowClosed?(deviceName)
    }
}

// MARK: - Interactive delegate

class InteractiveDelegate: NSObject, NSApplicationDelegate, PreviewController {
    var sessions: [String: PreviewSession] = [:]
    var allDevices: [AVCaptureDevice] = []
    var positions: Set<Int> = []
    var stdinConnected = true
    let devicesMenuDelegate = DevicesMenuDelegate()
    private var deviceObservers: [NSObjectProtocol] = []

    var activeDeviceNames: Set<String> {
        return Set(sessions.keys)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        ProcessInfo.processInfo.processName = "Quern Preview"
        loadAppIcon()
        devicesMenuDelegate.controller = self
        setupMenuBar(devicesMenuDelegate: devicesMenuDelegate, quitTarget: self, quitAction: #selector(menuQuit(_:)))
        enableScreenCaptureDevices()
        deviceObservers = observeDeviceChanges(
            onConnect: { [weak self] device in self?.deviceAppeared(device) },
            onDisconnect: { [weak self] device in self?.deviceVanished(device) }
        )
        fputs("Interactive mode: waiting for device discovery...\n", stderr)

        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
            self.onReady()
        }
    }

    func onReady() {
        allDevices = discoverDevices()

        let deviceList = allDevices.map { d -> [String: String] in
            return ["name": d.localizedName, "id": d.uniqueID]
        }
        emit(["event": "ready", "devices": deviceList] as [String: Any])

        startStdinReader()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false  // Stay alive for new commands
    }

    // MARK: Stdin reader

    func startStdinReader() {
        let queue = DispatchQueue(label: "stdin-reader", qos: .userInitiated)
        queue.async {
            while let line = readLine(strippingNewline: true) {
                if line.isEmpty { continue }
                guard let data = line.data(using: .utf8),
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let cmd = json["cmd"] as? String else {
                    DispatchQueue.main.async {
                        self.emit(["event": "error", "message": "Invalid JSON command"])
                    }
                    continue
                }

                DispatchQueue.main.async {
                    self.handleCommand(cmd: cmd, json: json)
                }
            }
            // EOF — stdin closed (server restarted or stopped).
            // Stay alive so the user can still use the Devices menu
            // to toggle previews via CoreMediaIO directly.
            DispatchQueue.main.async {
                self.stdinConnected = false
                fputs("Server disconnected (stdin EOF). Devices menu still available.\n", stderr)
            }
        }
    }

    func handleCommand(cmd: String, json: [String: Any]) {
        // The command id, echoed back on whatever event completes the command.
        // The device name alone cannot correlate a reply with its request: the
        // server times a write out after a few seconds, but the command was
        // already delivered and may still run, so a late reply would otherwise
        // be matched against whatever request holds that name next.
        let id = json["id"] as? String

        switch cmd {
        case "add":
            guard let name = json["name"] as? String else {
                emit(["event": "error", "message": "add requires 'name'"])
                return
            }
            let position = json["position"] as? Int ?? nextPosition()
            handleAdd(name: name, position: position, id: id)

        case "remove":
            guard let name = json["name"] as? String else {
                emit(["event": "error", "message": "remove requires 'name'"])
                return
            }
            handleRemove(name: name, id: id)

        case "list":
            handleList()

        case "quit":
            handleQuit()

        default:
            emit(["event": "error", "message": "Unknown command: \(cmd)"])
        }
    }

    // MARK: Command handlers

    func handleAdd(name: String, position: Int, id: String? = nil) {
        // Already previewing?
        if sessions[name] != nil {
            emit(["event": "add_failed", "name": name, "error": "Already previewing", "id": id as Any])
            return
        }

        // Find device by exact name
        guard let device = allDevices.first(where: { $0.localizedName == name }) else {
            emit(["event": "add_failed", "name": name, "error": "Device not found", "id": id as Any])
            return
        }

        // Create session
        let session = PreviewSession(device: device, position: position)

        if session.session.inputs.isEmpty {
            session.stop()
            emit(["event": "add_failed", "name": name, "error": "Cannot create input", "id": id as Any])
            return
        }

        session.onWindowClosed = { [weak self] closedName in
            self?.onWindowClosed(name: closedName)
        }

        sessions[name] = session
        positions.insert(position)

        // Start capture, then acknowledge after a brief delay for CoreMediaIO.
        //
        // The window can be closed inside that second. Acknowledging anyway
        // told the server an add had succeeded, and it would record an active
        // preview with no window behind it -- and go on refusing a fresh add
        // for that device, because it believed one was already running. So the
        // session has to still be the one this call created; a `window_closed`
        // has already gone out for it otherwise.
        session.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self, weak session] in
            guard let self else { return }
            guard let session, self.sessions[name] === session else {
                self.emit([
                    "event": "add_failed",
                    "name": name,
                    "error": "Window closed before the preview was acknowledged",
                    "id": id as Any,
                ])
                return
            }
            self.emit(["event": "added", "name": name, "id": id as Any])
        }
    }

    /// A window whose device is gone must close here too, but the server is
    /// the one tracking what is previewing, so it is told rather than left to
    /// discover the mismatch on its next command. Reported as `disconnected`
    /// and not `removed`: the server asked for neither, and a caller that
    /// requested this preview should be able to tell "the phone was unplugged"
    /// from "someone called remove".
    private func deviceVanished(_ device: AVCaptureDevice) {
        let name = device.localizedName
        allDevices.removeAll { $0.uniqueID == device.uniqueID }
        if let session = sessions[name] {
            fputs("  \(name) disconnected — closing its window\n", stderr)
            session.onWindowClosed = nil
            session.stop()
            sessions.removeValue(forKey: name)
            rebuildPositions()
        }
        // Emitted whether or not a window was open. The server prunes its
        // available-devices list on this event, so returning early for a
        // device nobody was previewing left the server advertising an
        // unplugged phone until something forced a refresh.
        emit(["event": "disconnected", "name": name])
    }

    /// No window is opened here on purpose. In interactive mode the server
    /// decides what is on screen, and a window appearing by itself would
    /// contradict the caller that asked for a specific set. Announce it
    /// instead, so the server can offer it or open it deliberately.
    private func deviceAppeared(_ device: AVCaptureDevice) {
        if !allDevices.contains(where: { $0.uniqueID == device.uniqueID }) {
            allDevices.append(device)
        }
        emit(["event": "connected", "name": device.localizedName, "id": device.uniqueID])
    }

    func handleRemove(name: String, id: String? = nil) {
        guard let session = sessions[name] else {
            emit(["event": "error", "message": "Not previewing: \(name)"])
            return
        }

        session.onWindowClosed = nil  // Prevent double event
        session.stop()
        sessions.removeValue(forKey: name)
        // Release position (we don't track which position maps to which session, so just rebuild)
        rebuildPositions()
        emit(["event": "removed", "name": name, "id": id as Any])
    }

    func handleList() {
        let deviceList = allDevices.map { d -> [String: String] in
            return ["name": d.localizedName, "id": d.uniqueID]
        }
        let previewing = Array(sessions.keys)
        emit(["event": "devices", "devices": deviceList, "previewing": previewing] as [String: Any])
    }

    func handleQuit() {
        for (_, session) in sessions {
            session.onWindowClosed = nil
            session.stop()
        }
        sessions.removeAll()
        NSApplication.shared.terminate(nil)
    }

    @objc func menuQuit(_ sender: Any?) {
        handleQuit()
    }

    func togglePreview(name: String, position: Int) {
        if sessions[name] != nil {
            handleRemove(name: name)
        } else {
            handleAdd(name: name, position: position)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        return true
    }

    // MARK: Helpers

    func onWindowClosed(name: String) {
        sessions.removeValue(forKey: name)
        rebuildPositions()
        emit(["event": "window_closed", "name": name])
    }

    func nextPosition() -> Int {
        var pos = 0
        while positions.contains(pos) { pos += 1 }
        return pos
    }

    func rebuildPositions() {
        // We don't track position per session in a recoverable way,
        // so just clear — new adds will get fresh positions
        positions.removeAll()
    }

    func emit(_ dict: [String: Any]) {
        guard stdinConnected else { return }
        // Drop keys holding a nil Optional before serialising. Callers pass
        // optionals through `as Any` -- `"id": id as Any` with no id is the
        // reason this exists -- and JSONSerialization rejects that value for
        // the whole dictionary, not just the key. Paired with `try?` below,
        // that turned one absent id into an event the server never receives.
        var clean: [String: Any] = [:]
        for (key, value) in dict {
            if case Optional<Any>.none = value { continue }
            clean[key] = value
        }
        guard let data = try? JSONSerialization.data(withJSONObject: clean),
              let str = String(data: data, encoding: .utf8) else {
            // Never silently: the server is waiting on this event, and a
            // dropped one reads to it as a hang rather than a failure.
            fputs("  emit failed for event \(clean["event"] ?? "?")\n", stderr)
            return
        }
        print(str)
    }
}

// MARK: - Simulator framebuffer preview
//
// Physical devices show up as CoreMediaIO capture devices; simulators never
// do, which is why the AVCaptureSession path above cannot see them. This
// path reads CoreSimulator's framebuffer directly -- the same IOSurface
// that tools/sim-bridge.swift grabs one-shot for screenshots, but
// subscribed continuously via screen callbacks instead of polled.
//
// No Simulator.app and no codec: the surface goes straight to a CALayer.
// Encoding would only make sense if these frames left the machine.

nonisolated(unsafe) private var simFrameworksLoaded = false

func simLogErr(_ msg: String) {
    fputs("\(msg)\n", stderr)
}

private func simDlerror() -> String {
    guard let e = dlerror() else { return "unknown" }
    return String(cString: e)
}

private func simHasSimulatorKit(at dev: String) -> Bool {
    let path = (dev as NSString)
        .appendingPathComponent("Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit")
    return FileManager.default.fileExists(atPath: path)
}

private func simXcodeSelectDir() -> String? {
    let pipe = Pipe()
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
    task.arguments = ["-p"]
    task.standardOutput = pipe
    do { try task.run() } catch { return nil }
    task.waitUntilExit()
    let out = String(
        data: pipe.fileHandleForReading.readDataToEndOfFile(),
        encoding: .utf8
    )?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return out.isEmpty ? nil : out
}

func simDeveloperDir() -> String {
    if let dev = simXcodeSelectDir(), simHasSimulatorKit(at: dev) { return dev }
    let canonical = "/Applications/Xcode.app/Contents/Developer"
    if simHasSimulatorKit(at: canonical) { return canonical }
    return simXcodeSelectDir() ?? canonical
}

func simLoadFrameworks() {
    guard !simFrameworksLoaded else { return }
    simFrameworksLoaded = true

    let coreSim = "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator"
    if dlopen(coreSim, RTLD_NOW | RTLD_GLOBAL) == nil {
        simLogErr("CoreSimulator load failed: \(simDlerror())")
    }
    // SimulatorKit is not needed for the framebuffer itself -- CoreSimulator
    // owns the IOSurface. It is loaded anyway so this path stays a drop-in
    // neighbour of sim-bridge, which needs it for HID input.
    let simKit = (simDeveloperDir() as NSString)
        .appendingPathComponent("Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit")
    if dlopen(simKit, RTLD_NOW | RTLD_GLOBAL) == nil {
        simLogErr("SimulatorKit load failed: \(simDlerror())")
    }
}

private func simInvokeClassObjWithObjAndError(
    _ cls: AnyClass, _ sel: Selector, _ arg: AnyObject, _ err: inout NSError?
) -> NSObject? {
    guard let metaCls = object_getClass(cls),
          let imp = class_getMethodImplementation(metaCls, sel) else { return nil }
    typealias Fn = @convention(c) (
        AnyClass, Selector, AnyObject, AutoreleasingUnsafeMutablePointer<NSError?>
    ) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(cls, sel, arg, &err) as? NSObject
}

private func simInvokeObjWithError(
    _ target: NSObject, _ sel: Selector, _ err: inout NSError?
) -> NSObject? {
    guard let imp = class_getMethodImplementation(type(of: target), sel) else { return nil }
    typealias Fn = @convention(c) (
        AnyObject, Selector, AutoreleasingUnsafeMutablePointer<NSError?>
    ) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(target, sel, &err) as? NSObject
}

func simAvailableDevices() -> [NSObject] {
    guard let cls = NSClassFromString("SimServiceContext") else {
        simLogErr("SimServiceContext unavailable -- private frameworks did not load")
        return []
    }
    var err: NSError?
    guard let ctx = simInvokeClassObjWithObjAndError(
        cls,
        NSSelectorFromString("sharedServiceContextForDeveloperDir:error:"),
        simDeveloperDir() as NSString,
        &err
    ) else {
        simLogErr("sharedServiceContext failed: \(err?.description ?? "nil")")
        return []
    }
    let setSel = NSSelectorFromString("defaultDeviceSetWithError:")
    guard ctx.responds(to: setSel),
          let set = simInvokeObjWithError(ctx, setSel, &err) else {
        simLogErr("defaultDeviceSet failed: \(err?.description ?? "nil")")
        return []
    }
    return (set.value(forKey: "availableDevices") as? [NSObject]) ?? []
}

func simResolveDevice(udid: String) -> NSObject? {
    for device in simAvailableDevices()
    where (device.value(forKey: "UDID") as? NSUUID)?.uuidString.lowercased() == udid.lowercased() {
        return device
    }
    return nil
}

/// One frame, and when it happened.
///
/// The timestamp is the reason this is a struct rather than a bare
/// IOSurface. Streaming never needed it — frames go out as fast as they
/// arrive and nobody asks when. Recording does, and the two sources differ
/// in what they can honestly report:
///
/// - A capture sample buffer carries a real presentation timestamp from
///   AVFoundation, already on the host clock.
/// - The simulator framebuffer callback says only "a frame happened", so
///   the best available answer is the host clock read on arrival. That is
///   a slightly later and slightly noisier time than the real composite,
///   and anything correlating video against logs should know it.
struct CapturedFrame {
    let surface: IOSurface
    /// Host clock (`CMClockGetHostTimeClock`), so every source and the
    /// recorder share one timebase.
    let time: CMTime
}

enum SimFramebufferError: Error, CustomStringConvertible {
    case deviceNotFound(String)
    case notBooted(String)
    case ioUnavailable
    case noFramebuffer
    case callbackUnavailable

    var description: String {
        switch self {
        case .deviceNotFound(let u): return "simulator not found: \(u)"
        case .notBooted(let s): return "simulator is not booted (state: \(s))"
        case .ioUnavailable: return "device.io unavailable"
        case .noFramebuffer: return "no com.apple.framebuffer.display descriptor"
        case .callbackUnavailable: return "registerScreenCallbacks selector unavailable"
        }
    }
}

/// Subscribes to a booted simulator's framebuffer and emits an `IOSurface`
/// per composited frame.
///
/// Registers on *every* framebuffer descriptor rather than the first. A
/// simulator exposes secondary planes and overlays -- `simctl io screenshot`
/// says as much when it reports defaulting to a display -- and the main
/// screen is simply whichever live surface is largest at that moment.
final class SimFramebuffer {
    private let udid: String
    private let queue = DispatchQueue(label: "quern.sim-preview.frames", qos: .userInteractive)
    private let onFrame: (CapturedFrame) -> Void

    private var ioClient: NSObject?
    private var descriptors: [NSObject] = []
    private var callbackUUIDs: [ObjectIdentifier: NSUUID] = [:]

    init(udid: String, onFrame: @escaping (CapturedFrame) -> Void) {
        self.udid = udid
        self.onFrame = onFrame
    }

    func start() throws {
        simLoadFrameworks()

        guard let device = simResolveDevice(udid: udid) else {
            throw SimFramebufferError.deviceNotFound(udid)
        }
        // A shut-down device still resolves and still hands back an `io`
        // client; it just never composites. Failing here beats a window that
        // stays black with no explanation.
        let state = (device.value(forKey: "state") as? NSNumber)?.intValue ?? -1
        guard state == 3 else {
            throw SimFramebufferError.notBooted(simStateName(state))
        }

        guard let io = device.perform(NSSelectorFromString("io"))?
            .takeUnretainedValue() as? NSObject else {
            throw SimFramebufferError.ioUnavailable
        }
        ioClient = io

        io.perform(NSSelectorFromString("updateIOPorts"))
        guard let ports = io.value(forKey: "deviceIOPorts") as? [NSObject] else {
            throw SimFramebufferError.noFramebuffer
        }

        let pidSel = NSSelectorFromString("portIdentifier")
        let descSel = NSSelectorFromString("descriptor")
        let surfSel = NSSelectorFromString("framebufferSurface")

        for port in ports where port.responds(to: pidSel) {
            guard let pid = port.perform(pidSel)?.takeUnretainedValue(),
                  "\(pid)" == "com.apple.framebuffer.display",
                  port.responds(to: descSel),
                  let desc = port.perform(descSel)?.takeUnretainedValue() as? NSObject,
                  desc.responds(to: surfSel) else { continue }
            descriptors.append(desc)
        }
        guard !descriptors.isEmpty else { throw SimFramebufferError.noFramebuffer }
        simLogErr("[sim-preview] framebuffer descriptors: \(descriptors.count)")

        for desc in descriptors { try register(on: desc) }

        // Nothing composites on an idle screen, so the callback alone can
        // leave the window empty until the user touches something. Prime it
        // with whatever is on screen right now.
        queue.async { [weak self] in self?.captureLatest() }
    }

    func stop() {
        let unregSel = NSSelectorFromString("unregisterScreenCallbacksWithUUID:")
        for desc in descriptors {
            if let uuid = callbackUUIDs[ObjectIdentifier(desc)], desc.responds(to: unregSel) {
                desc.perform(unregSel, with: uuid)
            }
        }
        descriptors.removeAll()
        callbackUUIDs.removeAll()
        ioClient = nil
    }

    private func register(on desc: NSObject) throws {
        let regSel = NSSelectorFromString(
            "registerScreenCallbacksWithUUID:callbackQueue:frameCallback:" +
                "surfacesChangedCallback:propertiesChangedCallback:"
        )
        guard desc.responds(to: regSel),
              let imp = class_getMethodImplementation(type(of: desc), regSel) else {
            throw SimFramebufferError.callbackUnavailable
        }

        let uuid = NSUUID()
        callbackUUIDs[ObjectIdentifier(desc)] = uuid

        let frame: @convention(block) () -> Void = { [weak self] in
            self?.queue.async { self?.captureLatest() }
        }
        let surfaces: @convention(block) () -> Void = { [weak self] in
            self?.queue.async { self?.captureLatest() }
        }
        let props: @convention(block) () -> Void = {}

        typealias Fn = @convention(c) (
            AnyObject, Selector, AnyObject, AnyObject, AnyObject, AnyObject, AnyObject
        ) -> Void
        unsafeBitCast(imp, to: Fn.self)(
            desc, regSel,
            uuid, queue as AnyObject,
            frame as AnyObject, surfaces as AnyObject, props as AnyObject
        )
    }

    private func captureLatest() {
        let surfSel = NSSelectorFromString("framebufferSurface")
        var best: IOSurface?
        var bestArea = 0
        for desc in descriptors {
            guard let surfObj = desc.perform(surfSel)?.takeUnretainedValue() else { continue }
            let surf = unsafeBitCast(surfObj, to: IOSurface.self)
            let area = IOSurfaceGetWidth(surf) * IOSurfaceGetHeight(surf)
            if area > bestArea {
                best = surf
                bestArea = area
            }
        }
        // No timestamp is available from the framebuffer callback, so this
        // is arrival time, not composite time.
        if let best {
            onFrame(CapturedFrame(surface: best, time: CMClockGetTime(CMClockGetHostTimeClock())))
        }
    }
}

func simStateName(_ state: Int) -> String {
    switch state {
    case 0: return "creating"
    case 1: return "shutdown"
    case 2: return "booting"
    case 3: return "booted"
    case 4: return "shutting down"
    default: return "unknown(\(state))"
    }
}

/// Window that renders simulator frames. Mirrors `PreviewSession`'s shape so
/// the two can collapse behind one frame-source protocol later.
final class SimPreviewWindow: NSObject, NSWindowDelegate {
    let window: NSWindow
    var onWindowClosed: ((String) -> Void)?

    private let udid: String
    private let viaCGImage: Bool
    private let contentLayer = CALayer()
    // Frames arrive faster than AppKit needs to draw them. Keep only the
    // newest and coalesce: an older frame is worthless the moment a newer
    // one exists, and queueing every callback onto main is how you build a
    // preview that runs seconds behind the device.
    private let lock = NSLock()
    private var pending: IOSurface?
    private var scheduled = false

    private var sizedToContent = false
    private var frameCount = 0
    private var lastReport = Date()

    init(udid: String, viaCGImage: Bool) {
        self.udid = udid
        self.viaCGImage = viaCGImage

        let screenFrame = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let w: CGFloat = 400
        let h: CGFloat = 710
        window = NSWindow(
            contentRect: NSRect(x: 50, y: screenFrame.height - h - 80, width: w, height: h),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Simulator \(udid.prefix(8))"
        window.isReleasedWhenClosed = false

        contentLayer.frame = NSRect(x: 0, y: 0, width: w, height: h)
        contentLayer.contentsGravity = .resizeAspect
        contentLayer.backgroundColor = NSColor.black.cgColor
        contentLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]

        let view = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        view.layer = contentLayer
        view.wantsLayer = true
        window.contentView = view

        super.init()
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    func stop() {
        window.delegate = nil
        window.close()
    }

    func windowWillClose(_ notification: Notification) {
        onWindowClosed?(udid)
    }

    /// Hand one frame to the window. The framebuffer is owned by the app
    /// delegate now, because the server is a second consumer of the same
    /// frames and neither consumer should own the source.
    func present(_ surface: IOSurface) {
        lock.lock()
        pending = surface
        let alreadyScheduled = scheduled
        scheduled = true
        lock.unlock()

        guard !alreadyScheduled else { return }
        DispatchQueue.main.async { [weak self] in self?.drain() }
    }

    private func drain() {
        lock.lock()
        let surface = pending
        pending = nil
        scheduled = false
        lock.unlock()

        guard let surface else { return }
        render(surface)
        reportRate()
    }

    private func render(_ surface: IOSurface) {
        if !sizedToContent {
            sizeToSurface(surface)
            sizedToContent = true
        }
        // Implicit animation would cross-fade every single frame.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        contentLayer.contents = viaCGImage ? (cgImage(from: surface) as Any?) : (surface as Any?)
        CATransaction.commit()
    }

    private func sizeToSurface(_ surface: IOSurface) {
        let pw = CGFloat(IOSurfaceGetWidth(surface))
        let ph = CGFloat(IOSurfaceGetHeight(surface))
        guard pw > 0, ph > 0 else { return }
        simLogErr("[sim-preview] surface \(Int(pw))x\(Int(ph)) px")

        let targetWidth: CGFloat = 400
        let size = NSSize(width: targetWidth, height: (targetWidth * ph / pw).rounded())
        window.setContentSize(size)
        contentLayer.frame = NSRect(origin: .zero, size: size)
    }

    /// Fallback render path. `CALayer.contents` takes an IOSurface directly,
    /// which keeps the frame on the GPU; this copies it through CGContext
    /// instead. Kept behind a flag so a format mismatch on some runtime is a
    /// one-word change rather than a rewrite.
    private func cgImage(from surface: IOSurface) -> CGImage? {
        IOSurfaceLock(surface, .readOnly, nil)
        defer { IOSurfaceUnlock(surface, .readOnly, nil) }
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: IOSurfaceGetBaseAddress(surface),
                  width: IOSurfaceGetWidth(surface),
                  height: IOSurfaceGetHeight(surface),
                  bitsPerComponent: 8,
                  bytesPerRow: IOSurfaceGetBytesPerRow(surface),
                  space: colorSpace,
                  bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                      | CGImageAlphaInfo.premultipliedFirst.rawValue
              ) else { return nil }
        return ctx.makeImage()
    }

    private func reportRate() {
        frameCount += 1
        let now = Date()
        let elapsed = now.timeIntervalSince(lastReport)
        guard elapsed >= 1.0 else { return }
        let fps = Double(frameCount) / elapsed
        simLogErr(String(format: "[sim-preview] %.1f fps", fps))
        frameCount = 0
        lastReport = now
    }
}

// MARK: - Remote streaming (spike)
//
// The local window hands the IOSurface straight to a CALayer, which is free
// but only works on this machine. Getting a screen to a browser means
// paying for a codec. MJPEG first: every browser renders it from an <img>
// with no client-side JavaScript, which makes it the cheapest possible
// proof that frames can leave the box. H.264 is the bandwidth optimisation
// after the pipeline is known good, not before.

/// Encodes an IOSurface to JPEG on the CPU, optionally downscaled.
///
/// Kept as the fallback behind `--imageio`, and as the reference the
/// hardware path is measured against. Every step here runs on the cores:
/// the surface read, the CGContext resize and the JPEG encode.
///
/// Downscaling happens explicitly rather than via
/// kCGImageDestinationImageMaxPixelSize, which CGImageDestinationAddImage
/// does not reliably honour -- only the AddImageFromSource variant does.
func encodeJPEGWithImageIO(from surface: IOSurface, maxDimension: Int, quality: Double) -> Data? {
    IOSurfaceLock(surface, .readOnly, nil)
    let width = IOSurfaceGetWidth(surface)
    let height = IOSurfaceGetHeight(surface)
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(
              data: IOSurfaceGetBaseAddress(surface),
              width: width,
              height: height,
              bitsPerComponent: 8,
              bytesPerRow: IOSurfaceGetBytesPerRow(surface),
              space: colorSpace,
              bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                  | CGImageAlphaInfo.premultipliedFirst.rawValue
          ),
          let full = ctx.makeImage() else {
        IOSurfaceUnlock(surface, .readOnly, nil)
        return nil
    }
    IOSurfaceUnlock(surface, .readOnly, nil)

    var image = full
    let longest = max(width, height)
    if maxDimension > 0, longest > maxDimension {
        let factor = Double(maxDimension) / Double(longest)
        let tw = Int((Double(width) * factor).rounded())
        let th = Int((Double(height) * factor).rounded())
        if let scaleCtx = CGContext(
            data: nil,
            width: tw,
            height: th,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                | CGBitmapInfo.byteOrder32Little.rawValue
        ) {
            scaleCtx.interpolationQuality = .medium
            scaleCtx.draw(full, in: CGRect(x: 0, y: 0, width: tw, height: th))
            if let scaled = scaleCtx.makeImage() { image = scaled }
        }
    }

    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil) else {
        return nil
    }
    CGImageDestinationAddImage(
        dest, image,
        [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary
    )
    guard CGImageDestinationFinalize(dest) else { return nil }
    return out as Data
}

/// One connected browser.
private final class MJPEGClient {
    let connection: NWConnection
    var streaming = false
    /// Dropped-frame gate. A phone on wifi cannot absorb 60 fps of JPEG, and
    /// queueing what it cannot take converts "slow" into "minutes behind".
    /// One frame in flight at a time; newer frames replace nothing, they are
    /// simply skipped.
    var inFlight = false

    init(connection: NWConnection) {
        self.connection = connection
    }
}

let mjpegBoundary = "quernframe"

final class MJPEGServer {
    private let port: NWEndpoint.Port
    private let bindAll: Bool
    private let fps: Double
    private let maxDimension: Int
    private let quality: Double
    private let useImageIO: Bool
    private let useH264: Bool
    private let hardware: HardwareJPEGEncoder
    private let h264: HardwareH264Encoder
    /// Set when a client attaches. H.264 frames depend on earlier ones, so a
    /// late joiner decodes nothing until the next IDR -- rather than make it
    /// wait for the periodic one, mint a keyframe on demand.
    private var pendingKeyframe = false
    private var recorder: Recorder?

    private var listener: NWListener?
    private let queue = DispatchQueue(label: "quern.sim-preview.http")
    /// Guards `clients` and the counters. Frames arrive on whichever queue
    /// the source uses -- the simulator's framebuffer queue or the capture
    /// output's -- so this is no longer single-queue state.
    private let lock = NSLock()
    private var clients: [ObjectIdentifier: MJPEGClient] = [:]
    /// Next time an encode is due. Deadline-based rather than
    /// "1/fps since the last encode": resetting to the arrival time throws
    /// away the remainder, so a source running at 49 fps against a 30 fps
    /// target skips every other frame and lands on 24.5 instead of 30.
    private var nextDeadline = Date.distantPast
    /// Frames handed to publish(), before throttling. The gap between this
    /// and the served rate is what the throttle is actually doing.
    private var framesOffered = 0

    private var framesEncoded = 0
    private var bytesSent = 0
    private var lastReport = Date()

    init(port: UInt16, bindAll: Bool, fps: Double, maxDimension: Int, quality: Double,
         useImageIO: Bool, useH264: Bool = false, bitrate: Int = 2_000_000,
         recorder: Recorder? = nil) {
        self.port = NWEndpoint.Port(rawValue: port) ?? 8422
        self.bindAll = bindAll
        self.fps = fps
        self.maxDimension = maxDimension
        self.quality = quality
        self.useImageIO = useImageIO
        self.useH264 = useH264
        self.hardware = HardwareJPEGEncoder(maxDimension: maxDimension, quality: quality)
        self.h264 = HardwareH264Encoder(
            maxDimension: maxDimension, bitrate: bitrate, expectedFPS: fps
        )
        self.recorder = recorder
        // The writer must open on a keyframe. The first frame of a fresh
        // compression session is one anyway, but ask explicitly rather than
        // rely on that.
        self.pendingKeyframe = recorder != nil
    }

    func start() throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true

        // Loopback unless told otherwise. An unauthenticated live video feed
        // of a device screen is not something to put on a shared network by
        // default -- remote viewing has to be an explicit choice.
        //
        // requiredLocalEndpoint pins the bind address, and it is mutually
        // exclusive with NWListener's `on:` port argument -- setting both is
        // EINVAL, not a narrower bind.
        let listener: NWListener
        if bindAll {
            listener = try NWListener(using: params, on: port)
        } else {
            params.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: port)
            listener = try NWListener(using: params)
        }
        listener.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
        listener.stateUpdateHandler = { state in
            if case .failed(let err) = state { simLogErr("[sim-preview] listener failed: \(err)") }
        }
        listener.start(queue: queue)
        self.listener = listener

        let host = bindAll ? "0.0.0.0" : "127.0.0.1"
        simLogErr("[preview] MJPEG on http://\(host):\(port.rawValue)/  "
            + "(fps \(Int(fps)), max \(maxDimension)px, q\(quality), "
            + "codec \(useH264 ? "H.264" : "MJPEG"), "
            + "encoder \(useImageIO ? "ImageIO/CPU" : "VideoToolbox"))")
        if bindAll {
            simLogErr("[sim-preview] WARNING: bound to all interfaces, no auth -- "
                + "anyone on this network can watch the screen")
        }
    }

    func stop() {
        hardware.invalidate()
        h264.invalidate()
        listener?.cancel()
        listener = nil
        for client in clients.values { client.connection.cancel() }
        clients.removeAll()
    }

    // MARK: connections

    private func accept(_ conn: NWConnection) {
        let client = MJPEGClient(connection: conn)
        lock.lock()
        clients[ObjectIdentifier(conn)] = client
        lock.unlock()
        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .failed, .cancelled:
                self?.queue.async { self?.drop(conn) }
            default:
                break
            }
        }
        conn.start(queue: queue)
        readRequest(client)
    }

    private func drop(_ conn: NWConnection) {
        lock.lock()
        clients.removeValue(forKey: ObjectIdentifier(conn))
        lock.unlock()
    }

    private func readRequest(_ client: MJPEGClient) {
        client.connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) {
            [weak self] data, _, isComplete, error in
            guard let self else { return }
            if error != nil || isComplete {
                client.connection.cancel()
                return
            }
            guard let data, let head = String(data: data, encoding: .utf8) else {
                client.connection.cancel()
                return
            }
            let path = head.split(separator: "\r\n").first
                .flatMap { $0.split(separator: " ").dropFirst().first }
                .map(String.init) ?? "/"
            self.route(client, path: path)
        }
    }

    private func route(_ client: MJPEGClient, path: String) {
        if path.hasPrefix("/stream") {
            let contentType = useH264
                ? "video/h264"
                : "multipart/x-mixed-replace; boundary=\(mjpegBoundary)"
            let header = """
            HTTP/1.1 200 OK\r
            Content-Type: \(contentType)\r
            Cache-Control: no-store\r
            Connection: close\r
            \r\n
            """
            client.connection.send(
                content: header.data(using: .utf8),
                completion: .contentProcessed { _ in }
            )
            self.lock.lock()
            client.streaming = true
            self.pendingKeyframe = true
            let total = self.clients.count
            self.lock.unlock()
            simLogErr("[preview] client attached (\(total) total)")
        } else {
            let html = """
            <!doctype html><meta charset=utf-8><title>Quern sim preview</title>
            <style>body{margin:0;background:#111;display:grid;place-items:center;
            height:100vh}img{max-height:100vh;max-width:100vw}</style>
            <img src="/stream">
            """
            let response = """
            HTTP/1.1 200 OK\r
            Content-Type: text/html; charset=utf-8\r
            Content-Length: \(html.utf8.count)\r
            Connection: close\r
            \r
            \(html)
            """
            client.connection.send(content: response.data(using: .utf8), completion: .contentProcessed { _ in
                client.connection.cancel()
            })
        }
    }

    // MARK: publishing

    /// Called by the frame source for every frame it produces.
    ///
    /// Encodes synchronously, on the caller's queue, and hands only the
    /// finished JPEG to the server queue. That is not a micro-optimisation:
    /// the IOSurface behind an AVCaptureVideoDataOutput sample buffer belongs
    /// to a recycling pool and may be reused the moment the delegate returns.
    /// Deferring the pixel read to another queue would encode whichever frame
    /// the pool handed out next, or a tear between two of them. Encoding here
    /// also gives the capture path real backpressure, since
    /// alwaysDiscardsLateVideoFrames drops while we are busy.
    func publish(_ frame: CapturedFrame) {
        let surface = frame.surface
        let now = Date()
        lock.lock()
        // A recording is a consumer too: keep encoding when nobody is
        // watching, or the file stops whenever the last viewer leaves.
        let watching = clients.values.contains { $0.streaming } || recorder != nil
        // Encode at most fps times a second. Sources happily emit 60 fps
        // during animation; JPEG at that rate is a lot of CPU for frames
        // nobody can tell apart.
        framesOffered += 1
        let interval = 1.0 / fps
        let due = now >= nextDeadline
        if watching && due {
            // Advance by one interval to keep phase. If we have fallen more
            // than an interval behind -- an idle screen producing no frames,
            // or a stall -- resync to now instead of emitting a catch-up
            // burst of stale frames.
            nextDeadline += interval
            if nextDeadline <= now { nextDeadline = now + interval }
        }
        lock.unlock()
        guard watching, due else { return }

        if useH264 {
            lock.lock()
            let wantKey = pendingKeyframe
            pendingKeyframe = false
            lock.unlock()
            guard let out = h264.encode(frame, forceKeyframe: wantKey) else { return }
            recorder?.append(out.sample, isKeyframe: out.isKeyframe)
            // Elementary stream: the NAL start codes are the framing, so
            // there is no per-frame envelope to add.
            queue.async { [weak self] in self?.fanOut(out.annexB) }
            return
        }

        // Fall back rather than drop the frame: a VideoToolbox session can
        // fail to create, and a preview that silently goes black is worse
        // than one that quietly costs more CPU.
        let encoded = useImageIO
            ? encodeJPEGWithImageIO(from: surface, maxDimension: maxDimension, quality: quality)
            : (hardware.encode(surface)
                ?? encodeJPEGWithImageIO(from: surface, maxDimension: maxDimension, quality: quality))
        guard let jpeg = encoded else { return }

        var part = Data()
        part.append("--\(mjpegBoundary)\r\n".data(using: .utf8)!)
        part.append("Content-Type: image/jpeg\r\n".data(using: .utf8)!)
        part.append("Content-Length: \(jpeg.count)\r\n\r\n".data(using: .utf8)!)
        part.append(jpeg)
        part.append("\r\n".data(using: .utf8)!)

        queue.async { [weak self] in self?.fanOut(part) }
    }

    private func fanOut(_ part: Data) {
        lock.lock()
        let watchers = clients.values.filter { $0.streaming && !$0.inFlight }
        for client in watchers { client.inFlight = true }
        framesEncoded += 1
        bytesSent += part.count * max(watchers.count, 1)
        let active = clients.values.filter { $0.streaming }.count
        lock.unlock()

        for client in watchers {
            client.connection.send(content: part, completion: .contentProcessed {
                [weak self, weak client] _ in
                guard let self, let client else { return }
                self.lock.lock()
                client.inFlight = false
                self.lock.unlock()
            })
        }
        report(Date(), active: active)
    }

    private func report(_ now: Date, active: Int) {
        lock.lock()
        let elapsed = now.timeIntervalSince(lastReport)
        guard elapsed >= 2.0 else { lock.unlock(); return }
        let fps = Double(framesEncoded) / elapsed
        let offered = Double(framesOffered) / elapsed
        let kbps = Double(bytesSent) * 8.0 / elapsed / 1000.0
        framesEncoded = 0
        framesOffered = 0
        bytesSent = 0
        lastReport = now
        lock.unlock()
        simLogErr(String(format: "[preview] source %.1f fps -> served %.1f fps, %.0f kbps, %d client(s)",
                         offered, fps, kbps, active))
    }
}


/// Builds the recorder and the server together, because the server owns the
/// encoder and the recorder needs its output.
///
/// The server object is created when *either* serving or recording is
/// wanted, but its listener only starts when serving. (The class is still
/// called MJPEGServer and now also does H.264 and recording — that name is
/// a leftover, and worth fixing when this gets extracted.)
func makeRecordingAndServer(_ tuning: StreamTuning, label: String)
    -> (Recorder?, MJPEGServer?, [DispatchSourceSignal])? {
    var recorder: Recorder?
    var signals: [DispatchSourceSignal] = []
    if let path = tuning.recordPath {
        do {
            let r = try Recorder(url: URL(fileURLWithPath: path))
            recorder = r
            signals = installRecordingSignalHandlers(r)
            simLogErr("[record] \(path) (H.264 \(tuning.bitrate / 1000) kbps, "
                + "max \(tuning.fps) fps)")
        } catch {
            simLogErr("[record] could not open \(path): \(error)")
            return nil
        }
    }
    guard tuning.serve || recorder != nil else { return (nil, nil, signals) }

    let server = MJPEGServer(
        port: tuning.port, bindAll: tuning.bindAll, fps: tuning.fps,
        maxDimension: tuning.maxDimension, quality: tuning.quality,
        useImageIO: tuning.useImageIO, useH264: tuning.useH264,
        bitrate: tuning.bitrate, recorder: recorder
    )
    if tuning.serve {
        do { try server.start() } catch {
            simLogErr("[preview] could not bind port \(tuning.port): \(error)")
            return nil
        }
    }
    return (recorder, server, signals)
}

// MARK: - Simulator mode app delegate

class SimAppDelegate: NSObject, NSApplicationDelegate {
    private let opts: SimOptions
    private var preview: SimPreviewWindow?
    private var server: MJPEGServer?
    private var framebuffer: SimFramebuffer?
    private var recorder: Recorder?
    private var signalSources: [DispatchSourceSignal] = []

    init(opts: SimOptions) {
        self.opts = opts
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        loadAppIcon()
        // With no window there is nothing to put in the Dock, and a bouncing
        // Dock tile for a headless streamer is just noise.
        if !opts.tuning.window { NSApp.setActivationPolicy(.accessory) }

        guard let (recorder, server, signals) =
            makeRecordingAndServer(opts.tuning, label: opts.udid) else {
            NSApplication.shared.terminate(nil)
            return
        }
        self.recorder = recorder
        self.server = server
        self.signalSources = signals

        if opts.tuning.window {
            let preview = SimPreviewWindow(udid: opts.udid, viaCGImage: opts.viaCGImage)
            preview.onWindowClosed = { _ in NSApplication.shared.terminate(nil) }
            self.preview = preview
        }

        // One source, two sinks. Both are optional and neither owns the
        // framebuffer, which is the shape the real frame-source protocol
        // will need anyway.
        let framebuffer = SimFramebuffer(udid: opts.udid) { [weak self] frame in
            guard let self else { return }
            self.preview?.present(frame.surface)
            self.server?.publish(frame)
        }
        do {
            try framebuffer.start()
            self.framebuffer = framebuffer
            simLogErr("[sim-preview] streaming \(opts.udid)")
        } catch {
            simLogErr("[sim-preview] failed: \(error)")
            NSApplication.shared.terminate(nil)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        framebuffer?.stop()
        server?.stop()
        recorder?.finish()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool {
        opts.tuning.window
    }
}

// MARK: - Hardware JPEG encoder

/// JPEG via VideoToolbox instead of ImageIO.
///
/// Same bytes on the wire, same `<img>` on the client. The difference is
/// where the work happens: measured on an M4, ImageIO's CGImageDestination
/// path runs at 99-100% CPU-to-wall, while this one runs at ~23% and about
/// a sixth of the CPU time. See tools/encode-bench.swift.
///
/// Note that `UsingHardwareAcceleratedVideoEncoder` reports **false** for
/// the JPEG codec even though the work plainly leaves the cores. Do not
/// gate anything on that property here.
///
/// Scaling is the session's job: VideoToolbox resamples a mismatched input
/// buffer down to the dimensions the session was created with, which keeps
/// the resize on the media engine rather than in a CGContext.
final class HardwareJPEGEncoder {
    private let quality: Double
    private let maxDimension: Int

    private var session: VTCompressionSession?
    private var sessionWidth = 0
    private var sessionHeight = 0
    /// Frames arrive on whichever queue the source uses, and `encode` is
    /// called synchronously from there.
    private let lock = NSLock()

    init(maxDimension: Int, quality: Double) {
        self.maxDimension = maxDimension
        self.quality = quality
    }

    deinit { invalidate() }

    func invalidate() {
        lock.lock()
        if let session {
            VTCompressionSessionInvalidate(session)
            self.session = nil
        }
        lock.unlock()
    }

    /// Synchronous on purpose.
    ///
    /// `CompleteFrames` after every frame gives up media-engine pipelining,
    /// which costs wall time but not CPU. It buys the same contract the
    /// ImageIO path had: the surface is fully read before returning. That
    /// matters because a capture buffer's IOSurface can be rewritten in
    /// place once we let go of it, and the simulator's framebuffer surface
    /// is a single persistent surface that is always rewritten in place --
    /// retaining it would not stop that.
    func encode(_ surface: IOSurface) -> Data? {
        let sourceWidth = IOSurfaceGetWidth(surface)
        let sourceHeight = IOSurfaceGetHeight(surface)
        guard sourceWidth > 0, sourceHeight > 0 else { return nil }

        let (targetWidth, targetHeight) = target(sourceWidth, sourceHeight)

        lock.lock()
        defer { lock.unlock() }

        guard ensureSession(width: targetWidth, height: targetHeight) else { return nil }
        guard let session else { return nil }

        // Returns +1 through an Unmanaged out-param, hence takeRetainedValue.
        // This wraps the surface, it does not copy it -- which is exactly why
        // the encode below has to finish before we return.
        var unmanaged: Unmanaged<CVPixelBuffer>?
        guard CVPixelBufferCreateWithIOSurface(nil, surface, nil, &unmanaged) == kCVReturnSuccess,
              let pixelBuffer = unmanaged?.takeRetainedValue() else { return nil }

        var encoded: Data?
        let status = VTCompressionSessionEncodeFrame(
            session, imageBuffer: pixelBuffer,
            presentationTimeStamp: CMTime(value: 0, timescale: 30),
            duration: .invalid, frameProperties: nil, infoFlagsOut: nil
        ) { status, _, sample in
            guard status == noErr, let sample,
                  let block = CMSampleBufferGetDataBuffer(sample) else { return }
            let length = CMBlockBufferGetDataLength(block)
            guard length > 0 else { return }
            var data = Data(count: length)
            let copied = data.withUnsafeMutableBytes { raw -> Bool in
                guard let base = raw.baseAddress else { return false }
                return CMBlockBufferCopyDataBytes(
                    block, atOffset: 0, dataLength: length, destination: base
                ) == kCMBlockBufferNoErr
            }
            if copied { encoded = data }
        }
        guard status == noErr else {
            simLogErr("[preview] VT encode failed: \(status)")
            return nil
        }
        VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
        return encoded
    }

    private func target(_ width: Int, _ height: Int) -> (Int, Int) {
        let longest = max(width, height)
        guard maxDimension > 0, longest > maxDimension else { return (width, height) }
        let factor = Double(maxDimension) / Double(longest)
        // Even dimensions: odd sizes are legal for JPEG but a reliable
        // source of off-by-one chroma handling across decoders.
        func even(_ v: Double) -> Int { max(2, Int(v.rounded()) & ~1) }
        return (even(Double(width) * factor), even(Double(height) * factor))
    }

    /// Caller holds `lock`.
    private func ensureSession(width: Int, height: Int) -> Bool {
        if session != nil, sessionWidth == width, sessionHeight == height { return true }
        if let existing = session {
            VTCompressionSessionInvalidate(existing)
            session = nil
        }
        var created: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_JPEG, encoderSpecification: nil,
            imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: nil, refcon: nil, compressionSessionOut: &created
        )
        guard status == noErr, let created else {
            simLogErr("[preview] VTCompressionSessionCreate(JPEG) failed: \(status)")
            return false
        }
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_RealTime,
                             value: kCFBooleanTrue)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_Quality,
                             value: NSNumber(value: quality))
        VTCompressionSessionPrepareToEncodeFrames(created)
        session = created
        sessionWidth = width
        sessionHeight = height
        simLogErr("[preview] VideoToolbox JPEG session \(width)x\(height) q\(quality)")
        return true
    }
}

// MARK: - Hardware H.264 encoder

/// H.264 via VideoToolbox, for the case MJPEG cannot serve: many devices,
/// watched remotely.
///
/// The API is the same `VTCompressionSession` the JPEG path uses, but three
/// things differ and all of them matter.
///
/// 1. Output is **AVCC** — each NAL prefixed with a 4-byte big-endian length,
///    with SPS/PPS carried out-of-band in the format description rather than
///    inline. Annex-B (`00 00 00 01` start codes, parameter sets inline) is
///    what a raw elementary stream wants, so we convert and re-inject.
/// 2. Frames are *not* independent. A viewer joining mid-stream cannot decode
///    until a keyframe arrives, which is why `forceKeyframe` exists — the one
///    thing Android's screenrecord cannot do.
/// 3. Bitrate is a target we set, not an outcome of quality-per-frame.
final class HardwareH264Encoder {
    struct Encoded {
        /// The encoder's own sample buffer, timing intact. Recording takes
        /// this; the Annex-B rendering is only for the wire.
        let sample: CMSampleBuffer
        let annexB: Data
        let isKeyframe: Bool
    }

    private let maxDimension: Int
    private let bitrate: Int
    private let expectedFPS: Double

    private var session: VTCompressionSession?
    private var sessionWidth = 0
    private var sessionHeight = 0
    private var frameIndex: Int64 = 0
    private let lock = NSLock()

    /// Cached Annex-B parameter sets, refreshed whenever the format
    /// description changes. Re-sent ahead of every keyframe so a late joiner
    /// can start decoding without a side channel.
    private var parameterSetsAnnexB: Data?

    init(maxDimension: Int, bitrate: Int, expectedFPS: Double) {
        self.maxDimension = maxDimension
        self.bitrate = bitrate
        self.expectedFPS = expectedFPS
    }

    deinit { invalidate() }

    func invalidate() {
        lock.lock()
        if let session {
            VTCompressionSessionInvalidate(session)
            self.session = nil
        }
        lock.unlock()
    }

    /// Synchronous, for the same reason the JPEG encoder is: the IOSurface is
    /// wrapped, not copied, and both sources rewrite theirs in place.
    func encode(_ frame: CapturedFrame, forceKeyframe: Bool) -> Encoded? {
        let surface = frame.surface
        let sw = IOSurfaceGetWidth(surface), sh = IOSurfaceGetHeight(surface)
        guard sw > 0, sh > 0 else { return nil }
        let (tw, th) = target(sw, sh)

        lock.lock()
        defer { lock.unlock() }
        guard ensureSession(width: tw, height: th), let session else { return nil }

        var unmanaged: Unmanaged<CVPixelBuffer>?
        guard CVPixelBufferCreateWithIOSurface(nil, surface, nil, &unmanaged) == kCVReturnSuccess,
              let pixelBuffer = unmanaged?.takeRetainedValue() else { return nil }

        var props: CFDictionary?
        if forceKeyframe {
            props = [kVTEncodeFrameOptionKey_ForceKeyFrame: kCFBooleanTrue] as CFDictionary
        }

        var out: Encoded?
        // Real capture time, not frame_index/expectedFPS. The synthetic
        // version was harmless for streaming and wrong for recording: a
        // source running at 49fps against a 60fps nominal plays 22% fast,
        // and an idle gap collapses to nothing instead of showing as a
        // pause. Anything correlating video with logs needs the real clock.
        let pts = frame.time

        let status = VTCompressionSessionEncodeFrame(
            session, imageBuffer: pixelBuffer, presentationTimeStamp: pts,
            duration: .invalid, frameProperties: props, infoFlagsOut: nil
        ) { [weak self] status, _, sample in
            guard status == noErr, let sample, let self else { return }
            out = self.package(sample)
        }
        guard status == noErr else {
            simLogErr("[preview] H.264 encode failed: \(status)")
            return nil
        }
        VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
        return out
    }

    // MARK: - AVCC -> Annex-B

    private func package(_ sample: CMSampleBuffer) -> Encoded? {
        let keyframe = isKeyframe(sample)

        if keyframe, let fd = CMSampleBufferGetFormatDescription(sample) {
            parameterSetsAnnexB = extractParameterSets(fd)
        }

        guard let block = CMSampleBufferGetDataBuffer(sample) else { return nil }
        let length = CMBlockBufferGetDataLength(block)
        guard length > 0 else { return nil }
        var avcc = Data(count: length)
        let ok = avcc.withUnsafeMutableBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            return CMBlockBufferCopyDataBytes(
                block, atOffset: 0, dataLength: length, destination: base
            ) == kCMBlockBufferNoErr
        }
        guard ok else { return nil }

        var outData = Data()
        // Parameter sets ahead of every keyframe, not just the first. Costs a
        // few dozen bytes per IDR and means a viewer can join on any keyframe
        // rather than only at stream start.
        if keyframe, let ps = parameterSetsAnnexB { outData.append(ps) }

        // Walk the AVCC length-prefixed NALs and re-emit them Annex-B.
        //
        // Assemble the 4-byte length by hand rather than loading a UInt32:
        // each NAL advances the cursor by an arbitrary payload size, so the
        // next length field lands at an arbitrary offset, and a typed load
        // there traps on alignment.
        let start = Data([0x00, 0x00, 0x00, 0x01])
        var i = 0
        while i + 4 <= length {
            let n = Int(UInt32(avcc[i]) << 24 | UInt32(avcc[i + 1]) << 16
                | UInt32(avcc[i + 2]) << 8 | UInt32(avcc[i + 3]))
            i += 4
            guard n > 0, i + n <= length else { break }
            outData.append(start)
            outData.append(avcc.subdata(in: i..<(i + n)))
            i += n
        }
        return Encoded(sample: sample, annexB: outData, isKeyframe: keyframe)
    }

    private func isKeyframe(_ sample: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(
            sample, createIfNecessary: false
        ) as? [[CFString: Any]], let first = attachments.first else {
            return true  // no attachments at all: treat as sync
        }
        // "not sync" absent or false => this is a sync sample.
        if let notSync = first[kCMSampleAttachmentKey_NotSync] as? Bool { return !notSync }
        return true
    }

    private func extractParameterSets(_ fd: CMFormatDescription) -> Data? {
        var out = Data()
        let start = Data([0x00, 0x00, 0x00, 0x01])
        var count = 0
        guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            fd, parameterSetIndex: 0, parameterSetPointerOut: nil,
            parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil
        ) == noErr else { return nil }

        for idx in 0..<count {
            var ptr: UnsafePointer<UInt8>?
            var size = 0
            guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                fd, parameterSetIndex: idx, parameterSetPointerOut: &ptr,
                parameterSetSizeOut: &size, parameterSetCountOut: nil,
                nalUnitHeaderLengthOut: nil
            ) == noErr, let ptr else { continue }
            out.append(start)
            out.append(Data(bytes: ptr, count: size))
        }
        return out.isEmpty ? nil : out
    }

    // MARK: - session

    private func target(_ w: Int, _ h: Int) -> (Int, Int) {
        let longest = max(w, h)
        guard maxDimension > 0, longest > maxDimension else { return (even(w), even(h)) }
        let f = Double(maxDimension) / Double(longest)
        return (even(Int((Double(w) * f).rounded())), even(Int((Double(h) * f).rounded())))
    }
    private func even(_ v: Int) -> Int { max(2, v & ~1) }

    /// Caller holds `lock`.
    private func ensureSession(width: Int, height: Int) -> Bool {
        if session != nil, sessionWidth == width, sessionHeight == height { return true }
        if let existing = session { VTCompressionSessionInvalidate(existing); session = nil }
        parameterSetsAnnexB = nil

        var created: VTCompressionSession?
        let spec: [CFString: Any] = [
            kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder: true
        ]
        let status = VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: spec as CFDictionary,
            imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: nil, refcon: nil, compressionSessionOut: &created
        )
        guard status == noErr, let created else {
            simLogErr("[preview] VTCompressionSessionCreate(H264) failed: \(status)")
            return false
        }
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        // No B-frames: they add a reordering delay for a live view, and the
        // bitrate they save is not worth it here.
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_AllowFrameReordering,
                             value: kCFBooleanFalse)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_ProfileLevel,
                             value: kVTProfileLevel_H264_High_AutoLevel)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_AverageBitRate,
                             value: NSNumber(value: bitrate))
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_ExpectedFrameRate,
                             value: NSNumber(value: expectedFPS))
        // A periodic IDR bounds join latency even without an explicit request.
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_MaxKeyFrameInterval,
                             value: NSNumber(value: Int(expectedFPS * 2)))
        var hw: CFTypeRef?
        var isHW = false
        if VTSessionCopyProperty(created,
            key: kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,
            allocator: nil, valueOut: &hw) == noErr, let n = hw as? NSNumber { isHW = n.boolValue }
        VTCompressionSessionPrepareToEncodeFrames(created)

        session = created
        sessionWidth = width
        sessionHeight = height
        frameIndex = 0
        simLogErr("[preview] VideoToolbox H.264 \(width)x\(height) "
            + "@ \(bitrate / 1000) kbps target, hardware=\(isHW ? "YES" : "no")")
        return true
    }
}

// MARK: - Recording

/// Writes encoded frames to an .mp4 via `AVAssetWriter` passthrough.
///
/// Passthrough, not re-encode: the compression session already produced
/// H.264 sample buffers with correct timing, so the writer only has to
/// container them. That also means the Annex-B conversion is bypassed
/// entirely — Annex-B carries no timestamps, so a recording built from it
/// would have to invent them.
final class Recorder {
    private let writer: AVAssetWriter
    /// Created on the first sample, not at init.
    ///
    /// A passthrough input (nil outputSettings) has no way to describe the
    /// media it will carry, so `canAdd` refuses it unless given a
    /// `sourceFormatHint`. That hint is the encoder's format description,
    /// which does not exist until the first frame comes out.
    private var input: AVAssetWriterInput?
    private let lock = NSLock()

    private var started = false
    private var finished = false
    private var firstPTS: CMTime = .invalid
    private var lastPTS: CMTime = .invalid
    private(set) var framesWritten = 0
    private(set) var framesDropped = 0

    init(url: URL) throws {
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
    }

    /// Caller holds `lock`.
    private func startIfNeeded(with sample: CMSampleBuffer) -> Bool {
        if let input { return input.isReadyForMoreMediaData || true }
        guard let hint = CMSampleBufferGetFormatDescription(sample) else {
            simLogErr("[record] first sample has no format description")
            return false
        }
        let created = AVAssetWriterInput(
            mediaType: .video, outputSettings: nil, sourceFormatHint: hint
        )
        created.expectsMediaDataInRealTime = true
        guard writer.canAdd(created) else {
            simLogErr("[record] AVAssetWriter rejected the passthrough input")
            return false
        }
        writer.add(created)
        input = created
        guard writer.startWriting() else {
            simLogErr("[record] startWriting failed: "
                + (writer.error?.localizedDescription ?? "?"))
            return false
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        // Session starts at the first frame's real timestamp, so the movie's
        // timeline is the host clock rather than zero-based.
        writer.startSession(atSourceTime: pts)
        firstPTS = pts
        return true
    }

    func append(_ sample: CMSampleBuffer, isKeyframe: Bool) {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }

        if !started {
            // A file that opens on a non-keyframe is undecodable until the
            // next IDR, which for a short recording can mean the whole thing.
            guard isKeyframe else {
                framesDropped += 1
                return
            }
            guard startIfNeeded(with: sample) else {
                finished = true
                return
            }
            started = true
        }

        guard let input, input.isReadyForMoreMediaData else {
            framesDropped += 1
            return
        }
        if input.append(sample) {
            framesWritten += 1
            lastPTS = CMSampleBufferGetPresentationTimeStamp(sample)
        } else {
            framesDropped += 1
            simLogErr("[record] append failed: \(writer.error?.localizedDescription ?? "?")")
        }
    }

    /// Blocking. An mp4 whose moov atom was never written is not a shorter
    /// recording, it is an unopenable file, so this has to complete before
    /// the process exits.
    func finish() {
        lock.lock()
        if finished || !started {
            finished = true
            lock.unlock()
            simLogErr("[record] nothing recorded")
            return
        }
        finished = true
        let written = framesWritten, dropped = framesDropped
        let duration = CMTimeGetSeconds(CMTimeSubtract(lastPTS, firstPTS))
        lock.unlock()

        input?.markAsFinished()
        let done = DispatchSemaphore(value: 0)
        writer.finishWriting { done.signal() }
        _ = done.wait(timeout: .now() + 10)

        let fps = duration > 0 ? Double(written) / duration : 0
        simLogErr(String(
            format: "[record] wrote %d frames over %.2fs wall (%.1f fps), %d dropped -> %@",
            written, duration, fps, dropped, writer.outputURL.path))
        if writer.status == .failed {
            simLogErr("[record] FAILED: \(writer.error?.localizedDescription ?? "?")")
        }
    }
}

/// Finishes a recording on SIGINT/SIGTERM.
///
/// Without this, every recording ended with ^C or `kill` is a file with no
/// moov atom — unopenable, not merely truncated. The default signal action
/// has to be ignored first, or the process dies before the handler runs.
func installRecordingSignalHandlers(_ recorder: Recorder) -> [DispatchSourceSignal] {
    var sources: [DispatchSourceSignal] = []
    for sig in [SIGINT, SIGTERM] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler {
            simLogErr("[record] caught signal, finalising")
            recorder.finish()
            exit(0)
        }
        source.resume()
        sources.append(source)
    }
    return sources
}

// MARK: - Physical device frame source

func fourCC(_ value: OSType) -> String {
    let bytes = [
        UInt8((value >> 24) & 0xff), UInt8((value >> 16) & 0xff),
        UInt8((value >> 8) & 0xff), UInt8(value & 0xff),
    ]
    return String(bytes: bytes, encoding: .ascii) ?? "\(value)"
}

enum CaptureError: Error, CustomStringConvertible {
    case deviceNotFound(String)
    case cannotAddInput
    case cannotAddOutput
    case noIOSurface

    var description: String {
        switch self {
        case .deviceNotFound(let m): return "no connected capture device matching \"\(m)\""
        case .cannotAddInput: return "session rejected the device input"
        case .cannotAddOutput: return "session rejected the video data output"
        case .noIOSurface: return "sample buffers are not IOSurface-backed"
        }
    }
}

/// Frames from a USB-connected iOS device.
///
/// The shipping preview attaches only an `AVCaptureVideoPreviewLayer`, which
/// draws to the screen and hands back no pixels -- fine for a local window,
/// useless for streaming. Adding an `AVCaptureVideoDataOutput` to the *same*
/// session yields sample buffers without disturbing the layer: one session,
/// one device, two consumers.
///
/// The pixel format is pinned to 32BGRA so these buffers match the
/// simulator's framebuffer layout byte for byte and both sources can share
/// one encoder. Left alone, a DAL device negotiates YUV, which `encodeJPEG`
/// would happily misread as BGRA and render as garbage.
final class CaptureFrameSource: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()

    private let device: AVCaptureDevice
    private let output = AVCaptureVideoDataOutput()
    private let queue = DispatchQueue(label: "quern.capture.frames", qos: .userInteractive)
    private let onFrame: (CapturedFrame) -> Void
    private var describedFormat = false
    private var warnedNoSurface = false

    init(device: AVCaptureDevice, onFrame: @escaping (CapturedFrame) -> Void) {
        self.device = device
        self.onFrame = onFrame
        super.init()
    }

    func start() throws {
        session.beginConfiguration()
        do {
            let input = try AVCaptureDeviceInput(device: device)
            guard session.canAddInput(input) else {
                session.commitConfiguration()
                throw CaptureError.cannotAddInput
            }
            session.addInput(input)
        } catch let error as CaptureError {
            throw error
        } catch {
            session.commitConfiguration()
            throw error
        }

        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        // Drop rather than queue. A slow encoder must cost frames, not
        // latency -- a preview that is correct but ten seconds late is worse
        // than one that skips.
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)

        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            throw CaptureError.cannotAddOutput
        }
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
    }

    func stop() {
        session.stopRunning()
        output.setSampleBufferDelegate(nil, queue: nil)
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        if !describedFormat {
            describedFormat = true
            simLogErr("[device-preview] \(CVPixelBufferGetWidth(pixels))"
                + "x\(CVPixelBufferGetHeight(pixels)) px, "
                + "format \(fourCC(CVPixelBufferGetPixelFormatType(pixels)))")
        }

        // The whole reason both sources can share an encoder: a CVPixelBuffer
        // from this output is IOSurface-backed, so it arrives as the same
        // type the simulator framebuffer hands over.
        guard let ref = CVPixelBufferGetIOSurface(pixels)?.takeUnretainedValue() else {
            if !warnedNoSurface {
                warnedNoSurface = true
                simLogErr("[device-preview] \(CaptureError.noIOSurface)")
            }
            return
        }
        // Real capture time from AVFoundation, already on the host clock —
        // strictly better than stamping on arrival, so use it where it exists.
        onFrame(CapturedFrame(
            surface: unsafeBitCast(ref, to: IOSurface.self),
            time: CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        ))
    }
}

/// Window for a physical device. Unlike the simulator window this does not
/// push frames itself -- it shares the capture session with the data output
/// and lets AVFoundation drive the layer, which is strictly better than
/// re-rendering surfaces we already handed to the encoder.
final class CaptureStreamWindow: NSObject, NSWindowDelegate {
    let window: NSWindow
    var onWindowClosed: (() -> Void)?

    init(title: String, session: AVCaptureSession) {
        let screenFrame = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let w: CGFloat = 400
        let h: CGFloat = 710
        window = NSWindow(
            contentRect: NSRect(x: 50, y: screenFrame.height - h - 80, width: w, height: h),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = title
        window.isReleasedWhenClosed = false

        let previewLayer = AVCaptureVideoPreviewLayer(session: session)
        previewLayer.videoGravity = .resizeAspect
        previewLayer.frame = NSRect(x: 0, y: 0, width: w, height: h)
        previewLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]

        let view = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        view.wantsLayer = true
        view.layer?.addSublayer(previewLayer)
        window.contentView = view

        super.init()
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        onWindowClosed?()
    }
}

// MARK: - Physical device mode app delegate

class DeviceStreamAppDelegate: NSObject, NSApplicationDelegate {
    private let opts: DeviceStreamOptions
    private var source: CaptureFrameSource?
    private var server: MJPEGServer?
    private var previewWindow: CaptureStreamWindow?
    private var recorder: Recorder?
    private var signalSources: [DispatchSourceSignal] = []

    init(opts: DeviceStreamOptions) {
        self.opts = opts
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        loadAppIcon()
        if !opts.tuning.window { NSApp.setActivationPolicy(.accessory) }

        enableScreenCaptureDevices()
        // Same 3s settle the shipping path uses: the DAL plugin publishes
        // its devices asynchronously after the opt-in, so enumerating
        // immediately finds nothing.
        simLogErr("[device-preview] waiting for capture devices...")
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in
            self?.begin()
        }
    }

    private func begin() {
        let devices = discoverDevices()
        let match = opts.match.lowercased()
        let device = devices.first { $0.localizedName.lowercased().contains(match) }
        guard let device else {
            simLogErr("[device-preview] \(CaptureError.deviceNotFound(opts.match))")
            if devices.isEmpty {
                simLogErr("[device-preview] no capture devices at all -- is the "
                    + "device connected by USB, unlocked and trusted?")
            } else {
                simLogErr("[device-preview] available: "
                    + devices.map { $0.localizedName }.joined(separator: ", "))
            }
            NSApplication.shared.terminate(nil)
            return
        }
        simLogErr("[device-preview] using \(device.localizedName)")

        guard let (recorder, server, signals) =
            makeRecordingAndServer(opts.tuning, label: device.localizedName) else {
            NSApplication.shared.terminate(nil)
            return
        }
        self.recorder = recorder
        self.server = server
        self.signalSources = signals

        let source = CaptureFrameSource(device: device) { [weak self] frame in
            self?.server?.publish(frame)
        }
        do {
            try source.start()
            self.source = source
        } catch {
            simLogErr("[device-preview] failed: \(error)")
            NSApplication.shared.terminate(nil)
            return
        }

        if opts.tuning.window {
            let win = CaptureStreamWindow(
                title: device.localizedName, session: source.session
            )
            win.onWindowClosed = { NSApplication.shared.terminate(nil) }
            previewWindow = win
        }
        simLogErr("[device-preview] streaming \(device.localizedName)")
    }

    func applicationWillTerminate(_ notification: Notification) {
        source?.stop()
        server?.stop()
        recorder?.finish()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool {
        opts.tuning.window
    }
}

// MARK: - Main

setlinebuf(stdout)

let mode = parseArgs()

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let delegate: NSApplicationDelegate
switch mode {
case .interactive:
    delegate = InteractiveDelegate()
case .simUDID(let opts):
    delegate = SimAppDelegate(opts: opts)
case .deviceStream(let opts):
    delegate = DeviceStreamAppDelegate(opts: opts)
default:
    delegate = AppDelegate(mode: mode)
}

app.delegate = delegate
app.activate(ignoringOtherApps: true)
app.run()
