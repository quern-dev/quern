#!/usr/bin/env swift
// ios-preview: Live preview of connected iOS device screens.
// Uses CoreMediaIO opt-in to discover iPhone screen capture devices,
// then opens an AVCaptureSession preview window per device.
//
// Usage:
//   ios-preview              # preview all connected devices
//   ios-preview --list       # list devices and exit
//   ios-preview "iPhone 11"  # preview devices matching a name substring
//   ios-preview 0 2          # preview devices by index
//   ios-preview --interactive # JSON Lines protocol on stdin/stdout
//
// Build: swiftc -o ios-preview tools/ios-preview/main.swift \
//          macos/QuernMedia/Sources/QuernMedia/Encode/JPEGFraming.swift \
//          macos/QuernMedia/Sources/QuernMedia/Capture/SimulatorList.swift \
//          -framework AVFoundation -framework CoreMediaIO -framework AppKit
//
// Named main.swift because it is top-level code: Swift allows that only
// in a file with that name, and it has to compile alongside a second file
// so the frame parser can live somewhere with a test target.

import AVFoundation
import AppKit
import CoreImage
import CoreMediaIO
import ImageIO
import Foundation

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

// MARK: - Booted simulators

/// Keeps the list of booted simulators current.
///
/// A simulator is not a capture device, so a `DiscoverySession` never sees
/// one -- which is why the Devices menu used to list only USB phones. Nothing
/// announces a boot or a shutdown to this process either, so the list is
/// polled: every few seconds in the background, so it is already right when
/// the menu opens, and again as it opens, so a change made in the last few
/// seconds still appears while the menu is up.
final class SimulatorWatcher {
    /// Main thread only.
    private(set) var booted: [BootedSimulator] = []
    /// Called on the main thread when `booted` changes.
    var onChange: (() -> Void)?

    private var timer: Timer?
    private var refreshing = false
    private var reportedFailure = false

    func start(every interval: TimeInterval = 4) {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    /// Asks `simctl` again, off the main thread. A failed ask keeps the last
    /// answer rather than emptying the list: "could not ask" is not "nothing
    /// is booted", and treating it as such would make every simulator vanish
    /// from the menu on one slow `simctl`.
    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        DispatchQueue.global(qos: .utility).async {
            let found = Self.query()
            DispatchQueue.main.async {
                self.refreshing = false
                guard let found else {
                    if !self.reportedFailure {
                        self.reportedFailure = true
                        fputs("Could not list booted simulators with simctl\n", stderr)
                    }
                    return
                }
                self.reportedFailure = false
                guard found != self.booted else { return }
                self.booted = found
                self.onChange?()
            }
        }
    }

    /// The booted simulators, or nil if `simctl` could not be asked.
    static func query() -> [BootedSimulator]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        process.arguments = ["simctl", "list", "devices", "booted", "-j"]
        let out = Pipe()
        process.standardOutput = out
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        // Bounded: a hung simctl would otherwise leave `refreshing` set for
        // good, freezing the list, and hang the launch check that calls this
        // on the main thread.
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
            if process.isRunning { process.terminate() }
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else { return nil }
        return SimulatorList.parse(data)
    }
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

enum FilterMode {
    case all
    case listOnly
    case interactive
    case byArgs([String])
}

func parseArgs() -> FilterMode {
    let args = Array(CommandLine.arguments.dropFirst())
    if args.isEmpty { return .all }
    if args.contains("--list") || args.contains("-l") { return .listOnly }
    if args.contains("--interactive") { return .interactive }
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

/// Sessions are addressed by `AVCaptureDevice.uniqueID`, not by name.
///
/// Two phones of the same model report the same `localizedName`, so a
/// name-keyed session table could only ever hold one of them, and unplugging
/// one left the other's window unreachable. The unique ID is also what
/// survives a re-plug, which a name does not: the old entry kept the name
/// taken and the returning device could not attach.
protocol PreviewController: AnyObject {
    var allDevices: [AVCaptureDevice] { get set }
    var activeSessionKeys: Set<String> { get }
    var simulators: SimulatorWatcher { get }
    func togglePreview(key: String, position: Int)
    /// Opens or closes a simulator's preview. Keyed by udid, like every
    /// simulator session.
    func toggleSimulator(_ simulator: BootedSimulator, position: Int)
    func nextPosition() -> Int
}

// MARK: - Devices menu delegate

class DevicesMenuDelegate: NSObject, NSMenuDelegate {
    weak var controller: PreviewController?
    /// The menu while it is open, so a change in the simulator list can be
    /// shown without the person closing and reopening it.
    private weak var openMenu: NSMenu?

    func menuNeedsUpdate(_ menu: NSMenu) {
        guard let controller = controller else { return }
        // Re-discover devices every time the menu opens — AVCaptureDevice
        // references go stale after capture sessions are torn down.
        enableScreenCaptureDevices()
        controller.allDevices = discoverDevices()
        populate(menu)
    }

    func menuWillOpen(_ menu: NSMenu) {
        openMenu = menu
        controller?.simulators.refresh()
    }

    func menuDidClose(_ menu: NSMenu) {
        openMenu = nil
    }

    /// The simulator list changed. Rebuilds the menu in place if it is open.
    func simulatorsChanged() {
        if let menu = openMenu { populate(menu) }
    }

    private func populate(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let controller = controller else { return }

        let devices = controller.allDevices
        let simulators = controller.simulators.booted
        if devices.isEmpty && simulators.isEmpty {
            let none = NSMenuItem(title: "No Devices Found", action: nil, keyEquivalent: "")
            none.isEnabled = false
            menu.addItem(none)
        }
        for device in devices {
            let item = NSMenuItem(title: device.localizedName, action: #selector(toggleDevice(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = device.uniqueID
            if controller.activeSessionKeys.contains(device.uniqueID) {
                item.state = .on
            }
            menu.addItem(item)
        }
        if !simulators.isEmpty {
            if !devices.isEmpty { menu.addItem(.separator()) }
            let header = NSMenuItem(title: "Simulators", action: nil, keyEquivalent: "")
            header.isEnabled = false
            menu.addItem(header)
            for simulator in simulators {
                let item = NSMenuItem(title: simulator.menuTitle, action: #selector(toggleSimulator(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = simulator.udid
                if controller.activeSessionKeys.contains(simulator.udid) {
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
              let key = sender.representedObject as? String else { return }
        controller.togglePreview(key: key, position: controller.nextPosition())
    }

    @objc func toggleSimulator(_ sender: NSMenuItem) {
        guard let controller = controller,
              let udid = sender.representedObject as? String,
              let simulator = controller.simulators.booted.first(where: { $0.udid == udid })
        else { return }
        controller.toggleSimulator(simulator, position: controller.nextPosition())
    }

    @objc func refreshDevices(_ sender: NSMenuItem) {
        guard let controller = controller else { return }
        enableScreenCaptureDevices()
        controller.allDevices = discoverDevices()
        controller.simulators.refresh()
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
        onWindowClosed?(device.uniqueID)
    }
}

// MARK: - Simulator preview without a server

/// A simulator preview this process runs itself: its own `quern-media`, and
/// a stream window on it.
///
/// For when there is no quern server to ask -- the standalone app the menu
/// bar's Screen Mirror opens, or an interactive one whose server has gone.
/// With a server, the server starts `quern-media` instead, so it can report
/// and stop the stream. The arguments are the server's (`STREAM_ARGS` in
/// server/device/media/preview.py), and tests/test_preview.py checks the two
/// copies agree.
final class LocalSimulatorPreview {
    static let streamArguments = [
        "--max-dim", "0", "--quality", "0.85", "--fps", "60", "--exit-with-parent",
    ]

    /// Installed beside this bundle, in quern's bin directory.
    static var binary: URL {
        Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("quern-media")
    }

    let simulator: BootedSimulator
    let position: Int
    private var port: UInt16 = 0
    private var process: Process?
    private var stderrPipe: Pipe?
    private var stderrTail: [String] = []
    private(set) var session: StreamPreviewSession?
    private var stopped = false
    private var relaunches = 0

    /// Called once, on the main queue, when the preview is gone: its window
    /// closed, or it could not start (with the reason).
    var onEnded: ((String, String?) -> Void)?

    init(simulator: BootedSimulator, position: Int) {
        self.simulator = simulator
        self.position = position
    }

    func start() {
        guard FileManager.default.isExecutableFile(atPath: Self.binary.path) else {
            // Not "run quern setup": setup does not build it. The server
            // builds it the first time it previews or records a simulator.
            end(error: "quern-media has not been built on this Mac yet. Quern builds it "
                + "the first time it previews a simulator: start quern and open this "
                + "simulator with preview_device, then try again.")
            return
        }
        guard let port = Self.freeLoopbackPort() else {
            end(error: "No free loopback port for the stream.")
            return
        }
        self.port = port
        launch()
        waitUntilServing(deadline: Date().addingTimeInterval(20))
    }

    func stop() {
        guard !stopped else { return }
        stopped = true
        session?.onWindowClosed = nil
        session?.stop()
        session = nil
        process?.terminationHandler = nil
        process?.terminate()
        process = nil
        // Cleared here as well as at EOF: with the termination handler gone
        // nothing else would, and a readability handler on a closed pipe is
        // called over and over with nothing -- a core spinning per preview.
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe = nil
    }

    private func launch() {
        let process = Process()
        process.executableURL = Self.binary
        process.arguments = ["--sim-udid", simulator.udid, "--serve", String(port)]
            + Self.streamArguments
        let err = Pipe()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = err
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            // End of file. Left installed, the handler is called again at
            // once with nothing, forever.
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            self?.appendStderr(data)
        }
        process.terminationHandler = { [weak self] ended in
            // Whatever it said last is usually why it exited, and can still
            // be in the pipe when this runs.
            err.fileHandleForReading.readabilityHandler = nil
            let rest = err.fileHandleForReading.readDataToEndOfFile()
            if !rest.isEmpty { self?.appendStderr(rest) }
            DispatchQueue.main.async { self?.exited(ended) }
        }
        do {
            try process.run()
            self.process = process
            stderrPipe = err
        } catch {
            end(error: "Could not start quern-media: \(error.localizedDescription)")
        }
    }

    /// quern-media exited. Before the window opened, that is the reason the
    /// preview failed. After, the window is off air and reconnecting, so a
    /// fresh quern-media on the same port brings it back -- after a simulator
    /// reboot, say. Retried with backoff for as long as the window is open,
    /// which is cheap while the simulator stays shut down: each attempt
    /// exits at once.
    private func exited(_ ended: Process) {
        guard !stopped, ended === process else { return }
        process = nil
        guard session != nil else {
            let why = stderrTail.last ?? "exit status \(ended.terminationStatus)"
            end(error: "quern-media stopped before serving: \(why)")
            return
        }
        let delay = min(5.0, 1.0 * pow(2.0, Double(min(relaunches, 3))))
        relaunches += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.stopped, self.process == nil, self.session != nil else { return }
            self.launch()
        }
    }

    private func appendStderr(_ data: Data) {
        let lines = String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.stderrTail = Array((self.stderrTail + lines).suffix(5))
        }
    }

    private func waitUntilServing(deadline: Date) {
        let port = self.port
        DispatchQueue.global(qos: .userInitiated).async {
            let serving = Self.accepts(port: port)
            DispatchQueue.main.async { [weak self] in
                guard let self, !self.stopped else { return }
                if serving {
                    self.openWindow()
                } else if self.process == nil {
                    return  // exited(_:) has reported it
                } else if Date() >= deadline {
                    self.end(error: "quern-media did not start serving within 20s.")
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                        self.waitUntilServing(deadline: deadline)
                    }
                }
            }
        }
    }

    private func openWindow() {
        // By source as well as port, so a reconnect cannot land on another
        // simulator that has taken the port meanwhile: quern-media says 409.
        let url = URL(string: "http://127.0.0.1:\(port)/stream?source=\(simulator.udid)")!
        let session = StreamPreviewSession(
            sessionKey: simulator.udid, title: simulator.name, url: url, position: position
        )
        session.onWindowClosed = { [weak self] _ in self?.end(error: nil) }
        session.onFailed = { [weak self] message in self?.end(error: message) }
        session.onOffAir = { [weak self] reason in
            if reason == nil { self?.relaunches = 0 }
        }
        self.session = session
        session.start()
    }

    private func end(error: String?) {
        let wasStopped = stopped
        stop()
        guard !wasStopped else { return }
        onEnded?(simulator.udid, error)
    }

    /// A loopback port nothing is using, from the kernel. Closed again before
    /// quern-media binds it, so another process could take it in between;
    /// quern-media then fails to bind and says so, which is reported.
    static func freeLoopbackPort() -> UInt16? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, len) == 0 && getsockname(fd, $0, &len) == 0
            }
        }
        return bound ? UInt16(bigEndian: addr.sin_port) : nil
    }

    static func accepts(port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }
}

/// Tells the person why a simulator preview they asked for did not open.
func reportPreviewFailure(_ simulator: String, _ message: String) {
    fputs("  could not preview \(simulator): \(message)\n", stderr)
    let alert = NSAlert()
    alert.messageText = "Could not preview \(simulator)"
    alert.informativeText = message
    alert.alertStyle = .warning
    alert.runModal()
}

// MARK: - App delegate (standalone mode)

class AppDelegate: NSObject, NSApplicationDelegate, PreviewController {
    var allDevices: [AVCaptureDevice] = []
    var activePreviews: [String: PreviewWindow] = [:]
    /// Simulator previews, each with its own quern-media. Keyed by udid.
    var simulatorPreviews: [String: LocalSimulatorPreview] = [:]
    let simulators = SimulatorWatcher()
    let devicesMenuDelegate = DevicesMenuDelegate()
    let mode: FilterMode
    private var deviceObservers: [NSObjectProtocol] = []

    var activeSessionKeys: Set<String> {
        return Set(activePreviews.keys).union(simulatorPreviews.keys)
    }

    init(mode: FilterMode) {
        self.mode = mode
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        ProcessInfo.processInfo.processName = "Quern Preview"
        loadAppIcon()
        devicesMenuDelegate.controller = self
        setupMenuBar(devicesMenuDelegate: devicesMenuDelegate)
        simulators.onChange = { [weak self] in self?.devicesMenuDelegate.simulatorsChanged() }
        simulators.start()
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
        let key = device.uniqueID
        allDevices.removeAll { $0.uniqueID == key }
        guard let preview = activePreviews[key] else { return }
        fputs("  \(device.localizedName) disconnected — closing its window\n", stderr)
        preview.onWindowClosed = nil  // we are already removing it
        preview.stop()
        activePreviews.removeValue(forKey: key)
    }

    /// Plugging a phone in opens its window, so the app keeps showing what is
    /// attached rather than a snapshot of whatever was attached at launch.
    ///
    /// Honours the launch filter: started with no arguments means "everything",
    /// so anything new qualifies, but `ios-preview "iPhone 11"` asked for one
    /// device and must not sprout windows for the rest. List mode never gets
    /// here -- it prints and exits.
    private func deviceAppeared(_ device: AVCaptureDevice) {
        if !allDevices.contains(where: { $0.uniqueID == device.uniqueID }) {
            allDevices.append(device)
        }

        guard activePreviews[device.uniqueID] == nil else { return }

        switch mode {
        case .all:
            break
        case .byArgs(let args):
            guard !filterDevices([device], args: args).isEmpty else { return }
        case .listOnly, .interactive:
            return
        }

        fputs("  \(device.localizedName) connected — opening its window\n", stderr)
        togglePreview(key: device.uniqueID, position: nextPosition())
    }

    func onDevicesReady() {
        allDevices = discoverDevices()

        // Opened with no phone attached but a simulator booted, the app stays
        // up: the simulators are in the Devices menu. Quitting there is what
        // made the menu bar's Screen Mirror useless without a cable.
        if allDevices.isEmpty, case .all = mode {
            // Could not ask is not "none booted": staying up costs nothing,
            // and the watcher fills the menu if there are any.
            let booted = SimulatorWatcher.query()
            if booted == nil || booted?.isEmpty == false {
                fputs("No USB devices. Booted simulators are in the Devices menu.\n", stderr)
                return
            }
        }
        if allDevices.isEmpty {
            fputs("No iOS devices found.\n", stderr)
            fputs("Make sure your iPhone is connected via USB, unlocked, and trusted, "
                + "or boot a simulator.\n", stderr)
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
            preview.onWindowClosed = { [weak self] key in
                self?.activePreviews.removeValue(forKey: key)
            }
            activePreviews[device.uniqueID] = preview
            fputs("  Preview window created for \(device.localizedName)\n", stderr)
        }
        // Stagger session starts to avoid CoreMediaIO race conditions
        startNextSession(keys: devices.map { $0.uniqueID }, index: 0)
        print("Close all windows or Ctrl+C to quit.")
    }

    func startNextSession(keys: [String], index: Int) {
        guard index < keys.count, let preview = activePreviews[keys[index]] else { return }
        preview.start()
        if index + 1 < keys.count {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                self.startNextSession(keys: keys, index: index + 1)
            }
        }
    }

    func togglePreview(key: String, position: Int) {
        if let preview = activePreviews[key] {
            preview.onWindowClosed = nil
            preview.stop()
            activePreviews.removeValue(forKey: key)
        } else {
            guard let device = allDevices.first(where: { $0.uniqueID == key }) else { return }
            let preview = PreviewWindow(device: device, index: position)
            preview.onWindowClosed = { [weak self] closedKey in
                self?.activePreviews.removeValue(forKey: closedKey)
            }
            activePreviews[key] = preview
            preview.start()
        }
    }

    func toggleSimulator(_ simulator: BootedSimulator, position: Int) {
        if let preview = simulatorPreviews.removeValue(forKey: simulator.udid) {
            preview.stop()
            return
        }
        let preview = LocalSimulatorPreview(simulator: simulator, position: position)
        preview.onEnded = { [weak self] udid, error in
            self?.simulatorPreviews.removeValue(forKey: udid)
            if let error { reportPreviewFailure(simulator.name, error) }
        }
        simulatorPreviews[simulator.udid] = preview
        preview.start()
    }

    func nextPosition() -> Int {
        var pos = 0
        // Simulator previews by the position they were given, so one still
        // starting -- no window yet -- is not handed out twice.
        let used = Set(activePreviews.values.map { Int(($0.window.frame.origin.x - 50) / 420) })
            .union(simulatorPreviews.values.map(\.position))
        while used.contains(pos) { pos += 1 }
        return pos
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false  // User quits via ⌘Q or menu
    }

    /// A quern-media this app started would otherwise outlive it.
    func applicationWillTerminate(_ notification: Notification) {
        for preview in simulatorPreviews.values { preview.stop() }
        simulatorPreviews.removeAll()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        return true
    }
}

// MARK: - Interactive mode: PreviewSession

// MARK: - Session kinds

/// What the controller needs of a preview, whatever is behind it.
///
/// Two kinds exist. A `PreviewSession` mirrors a CoreMediaIO capture device;
/// a `StreamPreviewSession` displays an MJPEG stream served by `quern-media`.
/// The second exists because a simulator is not a capture device and never
/// appears in a `DiscoverySession`, so it cannot be previewed the first way
/// at all.
///
/// `sessionKey` is deliberately opaque here: a capture device's
/// `AVCaptureDevice.uniqueID`, a stream's udid. The controller and the server
/// only ever echo it back, so neither has to know which it is holding.
protocol PreviewSessionKind: AnyObject {
    var sessionKey: String { get }
    var onWindowClosed: ((String) -> Void)? { get set }
    func start()
    func stop()
}

// MARK: - MJPEG stream client

/// Reads an MJPEG stream and hands back one image per frame.
///
/// The framing itself — why markers rather than the multipart boundary — is
/// documented on `JPEGFraming`, which owns it.
final class MJPEGClient: NSObject, URLSessionDataDelegate {
    /// Frame extraction lives in `QuernMedia/Encode/JPEGFraming.swift`, which
    /// `build_preview_bundle` compiles alongside this file. It is the one part
    /// of this client that is pure enough to test, and this file has no test
    /// target — which is how a 15s inactivity timeout and a black window both
    /// shipped from here.
    private var framing = JPEGFraming()

    private let url: URL
    private let onConnected: () -> Void
    private let onFrame: (CGImage) -> Void
    private let onError: (String) -> Void

    /// Guards `session`, `task` and `stopped`, which `stop()` writes from the
    /// main queue while the delegate callbacks read them on URLSession's.
    /// `framing` and `announced` are not guarded: both are touched only from
    /// the delegate queue, which is serial.
    /// Silence longer than this means the stream is gone, not merely idle.
    ///
    /// Both URLSession timeouts are unbounded on purpose — see `start()` — so
    /// nothing else notices a peer that stops sending without closing. That
    /// is a dropped network, or a producer wedged with its socket open; a
    /// process that dies sends a FIN and arrives as didCompleteWithError.
    ///
    /// The server repeats its last frame every few seconds so that silence
    /// means something. This sits well above that: missing one repeat is a
    /// hiccup, missing several is a dead peer.
    private static let idleTimeout: TimeInterval = 20

    private let lock = NSLock()
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var stopped = false
    private var lastDataAt = Date()
    private var watchdog: DispatchSourceTimer?
    /// `multipart/x-mixed-replace` is a sequence of responses as far as
    /// URLSession is concerned, so this delegate call arrives once per *frame*,
    /// not once per stream. Measured: the acknowledgement fired on every frame
    /// until this gate went in.
    private var announced = false

    init(
        url: URL,
        onConnected: @escaping () -> Void,
        onFrame: @escaping (CGImage) -> Void,
        onError: @escaping (String) -> Void
    ) {
        self.url = url
        self.onConnected = onConnected
        self.onFrame = onFrame
        self.onError = onError
        super.init()
    }

    func start() {
        let config = URLSessionConfiguration.default
        // Both timeouts are effectively off, and they have to be.
        // `timeoutIntervalForRequest` is an inactivity timeout that keeps
        // running while a response is open, and the server sends only when a
        // frame arrives -- there is no keepalive. An idle simulator composites
        // nothing, so at 15s a preview of a still screen died exactly 15
        // seconds after its first frame. Measured: "The request timed out."
        // followed by window_closed at 21.6s against a first frame at 6.6s.
        //
        // Nothing is lost by waiting forever. A stream that really has gone
        // closes the connection, and that arrives as didCompleteWithError.
        config.timeoutIntervalForRequest = .greatestFiniteMagnitude
        config.timeoutIntervalForResource = .greatestFiniteMagnitude
        let session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
        let task = session.dataTask(with: url)
        lock.lock()
        self.session = session
        self.task = task
        lastDataAt = Date()
        lock.unlock()
        startWatchdog()
        task.resume()
    }

    /// Safe from any queue. `stop()` is called on the main queue, while the
    /// delegate callbacks below run on URLSession's own queue.
    func stop() {
        lock.lock()
        stopped = true
        let task = self.task
        let session = self.session
        let timer = watchdog
        self.session = nil
        self.task = nil
        watchdog = nil
        lock.unlock()

        timer?.cancel()
        task?.cancel()
        session?.invalidateAndCancel()
    }

    private func startWatchdog() {
        let timer = DispatchSource.makeTimerSource(
            queue: DispatchQueue(label: "quern.preview.watchdog")
        )
        let tick = Self.idleTimeout / 4
        timer.schedule(deadline: .now() + tick, repeating: tick)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let silent = Date().timeIntervalSince(self.lastDataAt)
            let alreadyStopped = self.stopped
            self.lock.unlock()
            guard !alreadyStopped, silent >= Self.idleTimeout else { return }
            self.onError(String(
                format: "no data for %.0fs — the stream is gone, not idle", silent
            ))
        }
        lock.lock()
        watchdog = timer
        lock.unlock()
        timer.resume()
    }

    private var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        // `cancel()` is asynchronous, so a response already dispatched can
        // land after stop() returned. Downstream identity checks happen to
        // catch it today; not firing callbacks for a stopped client is the
        // guarantee this class should be making itself.
        guard !isStopped else {
            completionHandler(.cancel)
            return
        }

        // The acknowledgement signal. Deliberately not "first frame": the
        // simulator framebuffer is event-driven and costs nothing while idle,
        // so a simulator sitting on a static screen sends no frames at all and
        // an add waiting for one would time out on a working preview.
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 200 {
            if !announced {
                announced = true
                onConnected()
            }
            completionHandler(.allow)
        } else {
            onError("stream returned HTTP \(code)")
            completionHandler(.cancel)
        }
    }

    func urlSession(
        _ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data
    ) {
        guard !isStopped else { return }
        lock.lock()
        lastDataAt = Date()
        lock.unlock()
        for jpeg in framing.append(data) {
            guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil),
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
                // Dropped silently before. A frame that will not decode --
                // two SOIs with no EOI between them produce one, since marker
                // scanning cannot tell them apart -- looked identical to a
                // screen that had not changed.
                fputs("  stream \(url.absoluteString): dropped a frame that "
                    + "would not decode (\(jpeg.count) bytes)\n", stderr)
                continue
            }
            onFrame(image)
        }
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?
    ) {
        guard !isStopped else { return }
        onError(error?.localizedDescription ?? "stream ended")
    }

}

// MARK: - Stream-backed preview window

/// A preview window fed by an MJPEG stream instead of a capture device.
///
/// The window opens at the same default size as a capture preview and takes
/// the stream's own proportions from the first frame that arrives -- the
/// stream advertises no dimensions before then, which is the same problem
/// `StreamAspectSizer` solves for capture devices by polling the input port.
///
/// A stream that drops after it has been working leaves the window open,
/// greyed under an OFF AIR label, and reconnects until it comes back or the
/// window is closed. It used to close the window: a simulator rebooting, or
/// quern-media restarting, took the preview away along with the person's
/// window position, and a frozen last frame left on screen would have read
/// as a live one. A stream that never worked still fails the add.
final class StreamPreviewSession: NSObject, NSWindowDelegate, PreviewSessionKind {
    let sessionKey: String
    let window: NSWindow
    var onWindowClosed: ((String) -> Void)?

    private let url: URL
    private let title: String
    private let imageLayer = CALayer()
    private let offAirLayer = CALayer()
    private var client: MJPEGClient?
    private var haveSized = false
    private var connected = false
    private var closed = false
    private(set) var isOffAir = false
    private var retries = 0
    private var retry: DispatchWorkItem?
    /// Which connection's callbacks are current. A counter rather than the
    /// client itself: capturing the client in its own callbacks made each
    /// one keep itself alive, and an off-air window leaked one per retry.
    private var generation = 0

    init(sessionKey: String, title: String, url: URL, position: Int) {
        self.sessionKey = sessionKey
        self.url = url
        self.title = title

        let screenFrame = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
        let windowWidth: CGFloat = 400
        let windowHeight: CGFloat = 710
        let xOffset = CGFloat(position) * (windowWidth + 20) + 50
        let yOffset = screenFrame.height - windowHeight - 80

        window = NSWindow(
            contentRect: NSRect(x: xOffset, y: yOffset, width: windowWidth, height: windowHeight),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = title
        window.isReleasedWhenClosed = false

        let bounds = NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight)
        imageLayer.frame = bounds
        imageLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
        // Matches AVCaptureVideoPreviewLayer's .resizeAspect, so a stream
        // whose window has not been sized yet letterboxes rather than stretches.
        imageLayer.contentsGravity = .resizeAspect
        imageLayer.backgroundColor = NSColor.black.cgColor

        let view = NSView(frame: bounds)
        view.wantsLayer = true
        // The greyscale filter below is a Core Image filter on a layer, which
        // AppKit only applies to a view that opts in.
        view.layerUsesCoreImageFilters = true
        view.layer?.addSublayer(imageLayer)
        Self.buildOffAirLayer(offAirLayer, bounds: bounds, scale: window.backingScaleFactor)
        view.layer?.addSublayer(offAirLayer)
        window.contentView = view

        super.init()
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    /// A dimming layer over the picture, with "OFF AIR" in a red box centred
    /// on it. Hidden until the stream drops.
    private static func buildOffAirLayer(_ layer: CALayer, bounds: NSRect, scale: CGFloat) {
        layer.frame = bounds
        layer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
        layer.backgroundColor = NSColor.black.withAlphaComponent(0.6).cgColor
        layer.isHidden = true
        layer.layoutManager = CAConstraintLayoutManager()

        let red = NSColor(calibratedRed: 0.89, green: 0.2, blue: 0.2, alpha: 1)
        let font = NSFont.systemFont(ofSize: 34, weight: .heavy)
        let text = NSAttributedString(string: "OFF AIR", attributes: [
            .font: font, .foregroundColor: red, .kern: 4,
        ])
        let size = text.size()

        let label = CATextLayer()
        label.string = text
        label.contentsScale = scale
        label.alignmentMode = .center
        label.bounds = CGRect(x: 0, y: 0, width: ceil(size.width), height: ceil(size.height))

        let box = CALayer()
        box.bounds = CGRect(x: 0, y: 0, width: label.bounds.width + 44, height: label.bounds.height + 20)
        box.borderColor = red.cgColor
        box.borderWidth = 3
        box.cornerRadius = 8
        box.backgroundColor = NSColor.black.withAlphaComponent(0.6).cgColor
        box.layoutManager = CAConstraintLayoutManager()
        for layer in [label, box] {
            layer.addConstraint(CAConstraint(attribute: .midX, relativeTo: "superlayer", attribute: .midX))
            layer.addConstraint(CAConstraint(attribute: .midY, relativeTo: "superlayer", attribute: .midY))
        }
        box.addSublayer(label)
        layer.addSublayer(box)
    }

    /// Called once the stream's HTTP response first arrives, on the main
    /// queue. Not again after a reconnect: the add was acknowledged once.
    var onConnected: (() -> Void)?

    /// Called once, on the main queue, if the stream fails before it ever
    /// connected. The window has closed itself by then.
    var onFailed: ((String) -> Void)?

    /// Called on the main queue when the window goes off air (with the
    /// reason) and when it comes back (with nil).
    var onOffAir: ((String?) -> Void)?

    func start() {
        connect()
    }

    private func connect() {
        generation += 1
        let mine = generation
        let client = MJPEGClient(
            url: url,
            onConnected: { [weak self] in
                DispatchQueue.main.async {
                    guard let self, !self.closed, self.generation == mine else { return }
                    guard !self.connected else { return }
                    self.connected = true
                    self.onConnected?()
                }
            },
            onFrame: { [weak self] image in
                DispatchQueue.main.async {
                    guard let self, !self.closed, self.generation == mine else { return }
                    self.show(image)
                }
            },
            onError: { [weak self] message in
                DispatchQueue.main.async {
                    guard let self, !self.closed, self.generation == mine else { return }
                    self.streamFailed(message)
                }
            }
        )
        self.client = client
        client.start()
    }

    /// A stream that never connected fails the add, as it always has. One
    /// that had been working goes off air and is retried.
    private func streamFailed(_ message: String) {
        client?.stop()
        client = nil
        generation += 1  // nothing more from the client just stopped
        fputs("  stream \(sessionKey): \(message)\n", stderr)
        guard connected else {
            closed = true
            window.delegate = nil
            window.close()
            onFailed?(message)
            return
        }
        setOffAir(true, reason: message)
        // 0.5s doubling to 5s: quick enough to catch a restart, slow enough
        // that a simulator left shut down costs almost nothing.
        let delay = min(5.0, 0.5 * pow(2.0, Double(min(retries, 4))))
        retries += 1
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.closed else { return }
            self.connect()
        }
        retry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func setOffAir(_ off: Bool, reason: String? = nil) {
        guard off != isOffAir else { return }
        isOffAir = off
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        offAirLayer.isHidden = !off
        imageLayer.filters = off
            ? [CIFilter(name: "CIColorControls", parameters: [kCIInputSaturationKey: 0])].compactMap { $0 }
            : nil
        CATransaction.commit()
        window.title = off ? "\(title) — Off Air" : title
        onOffAir?(off ? (reason ?? "stream lost") : nil)
    }

    func stop() {
        closed = true
        retry?.cancel()
        retry = nil
        client?.stop()
        client = nil
        window.delegate = nil
        window.close()
    }

    private func show(_ image: CGImage) {
        if isOffAir {
            retries = 0
            setOffAir(false)
        }
        // Layer contents are not animatable here; without this every frame
        // cross-fades into the last and the preview smears.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        imageLayer.contents = image
        CATransaction.commit()

        guard !haveSized, image.width > 0, image.height > 0 else { return }
        haveSized = true
        fputs("  stream \(sessionKey): first frame \(image.width)x\(image.height)\n", stderr)
        let aspect = CGFloat(image.width) / CGFloat(image.height)
        let contentHeight = window.contentView?.frame.height ?? 710
        window.setContentSize(NSSize(width: (contentHeight * aspect).rounded(), height: contentHeight))
        window.contentAspectRatio = NSSize(width: image.width, height: image.height)
    }

    func windowWillClose(_ notification: Notification) {
        closed = true
        retry?.cancel()
        retry = nil
        client?.stop()
        client = nil
        onWindowClosed?(sessionKey)
    }
}


class PreviewSession: NSObject, NSWindowDelegate, PreviewSessionKind {
    let deviceName: String
    let sessionKey: String
    let window: NSWindow
    let session: AVCaptureSession
    var onWindowClosed: ((String) -> Void)?
    private var input: AVCaptureDeviceInput?
    private var sizer: StreamAspectSizer?

    init(device: AVCaptureDevice, position: Int) {
        self.deviceName = device.localizedName
        self.sessionKey = device.uniqueID
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
        onWindowClosed?(sessionKey)
    }
}

// MARK: - Interactive delegate

class InteractiveDelegate: NSObject, NSApplicationDelegate, PreviewController {
    var sessions: [String: any PreviewSessionKind] = [:]
    var allDevices: [AVCaptureDevice] = []
    var positions: Set<Int> = []
    var stdinConnected = true
    let simulators = SimulatorWatcher()
    /// Simulator previews opened from the menu after the server went away,
    /// each with its own quern-media. Keyed by udid.
    var localSimulators: [String: LocalSimulatorPreview] = [:]
    /// Previews the person closed while the server may still be opening
    /// them. Closing one mid-add fails that add, and the person should not
    /// then be told their own close was an error.
    var closedByPerson: Set<String> = []
    let devicesMenuDelegate = DevicesMenuDelegate()
    private var deviceObservers: [NSObjectProtocol] = []

    var activeSessionKeys: Set<String> {
        return Set(sessions.keys).union(localSimulators.keys)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        ProcessInfo.processInfo.processName = "Quern Preview"
        loadAppIcon()
        devicesMenuDelegate.controller = self
        setupMenuBar(devicesMenuDelegate: devicesMenuDelegate, quitTarget: self, quitAction: #selector(menuQuit(_:)))
        simulators.onChange = { [weak self] in self?.devicesMenuDelegate.simulatorsChanged() }
        simulators.start()
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
            // `key` is what this speaks; `name` is accepted because a person
            // driving it by hand has the name and not the ID.
            guard let identifier = (json["key"] ?? json["name"]) as? String else {
                emit(["event": "error", "message": "add requires 'key' or 'name'"])
                return
            }
            let position = json["position"] as? Int ?? nextPosition()
            handleAdd(name: identifier, position: position, id: id)

        case "add_stream":
            guard let key = (json["key"] ?? json["name"]) as? String else {
                emit(["event": "error", "message": "add_stream requires 'key'"])
                return
            }
            guard let urlString = json["url"] as? String, let url = URL(string: urlString) else {
                emit([
                    "event": "add_failed", "key": key,
                    "error": "add_stream requires a valid 'url'", "id": id as Any,
                ])
                return
            }
            let position = json["position"] as? Int ?? nextPosition()
            let title = json["title"] as? String ?? key
            handleAddStream(key: key, title: title, url: url, position: position, id: id)

        case "remove":
            guard let key = (json["key"] ?? json["name"]) as? String else {
                emit(["event": "error", "message": "remove requires 'key' or 'name'"])
                return
            }
            handleRemove(key: key, id: id)

        case "open_failed":
            // A simulator picked from the Devices menu that the server could
            // not open. The person is looking at this app, not the server log.
            let key = (json["key"] as? String) ?? ""
            if closedByPerson.remove(key) != nil { break }
            let name = simulators.booted.first(where: { $0.udid == key })?.name ?? key
            reportPreviewFailure(name, json["error"] as? String ?? "unknown error")

        case "list":
            handleList()

        case "quit":
            handleQuit()

        default:
            emit(["event": "error", "message": "Unknown command: \(cmd)"])
        }
    }

    // MARK: Command handlers

    /// Opens a window on a CoreMediaIO capture device.
    ///
    /// `identifier` is resolved to the device's `uniqueID`, which is what the
    /// session is filed under and what every event about it echoes back. A
    /// caller that sent a name therefore gets replies keyed by ID; the server
    /// sends the ID it was handed in `ready`, so for it the two are the same.
    func handleAdd(name identifier: String, position: Int, id: String? = nil) {
        // Both forms are accepted because both are things a caller reasonably
        // holds -- the server has the ID from `ready`, a person driving this
        // by hand has the name off the menu. The ID wins: two phones of the
        // same model share a name, and matching on it picks whichever was
        // discovered first.
        guard let device = allDevices.first(where: { $0.uniqueID == identifier })
            ?? allDevices.first(where: { $0.localizedName == identifier }) else {
            emit([
                "event": "add_failed", "key": identifier,
                "error": "Device not found", "id": id as Any,
            ])
            return
        }

        let key = device.uniqueID
        if sessions[key] != nil {
            emit([
                "event": "add_failed", "key": key,
                "error": "Already previewing", "id": id as Any,
            ])
            return
        }

        let session = PreviewSession(device: device, position: position)

        if session.session.inputs.isEmpty {
            session.stop()
            emit([
                "event": "add_failed", "key": key,
                "error": "Cannot create input", "id": id as Any,
            ])
            return
        }

        session.onWindowClosed = { [weak self] closedKey in
            self?.onWindowClosed(name: closedKey)
        }

        sessions[key] = session
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
            guard let session, self.sessions[key] === session else {
                self.emit([
                    "event": "add_failed",
                    "key": key,
                    "error": "Window closed before the preview was acknowledged",
                    "id": id as Any,
                ])
                return
            }
            self.emit(["event": "added", "key": key, "id": id as Any])
        }
    }

    /// A window whose device is gone must close here too, but the server is
    /// the one tracking what is previewing, so it is told rather than left to
    /// discover the mismatch on its next command. Reported as `disconnected`
    /// and not `removed`: the server asked for neither, and a caller that
    /// requested this preview should be able to tell "the phone was unplugged"
    /// from "someone called remove".
    private func deviceVanished(_ device: AVCaptureDevice) {
        let key = device.uniqueID
        allDevices.removeAll { $0.uniqueID == key }
        if let session = sessions[key] {
            fputs("  \(device.localizedName) disconnected — closing its window\n", stderr)
            session.onWindowClosed = nil
            session.stop()
            sessions.removeValue(forKey: key)
            rebuildPositions()
        }
        // Emitted whether or not a window was open. The server prunes its
        // available-devices list on this event, so returning early for a
        // device nobody was previewing left the server advertising an
        // unplugged phone until something forced a refresh.
        emit(["event": "disconnected", "key": key, "name": device.localizedName])
    }

    /// No window is opened here on purpose. In interactive mode the server
    /// decides what is on screen, and a window appearing by itself would
    /// contradict the caller that asked for a specific set. Announce it
    /// instead, so the server can offer it or open it deliberately.
    private func deviceAppeared(_ device: AVCaptureDevice) {
        if !allDevices.contains(where: { $0.uniqueID == device.uniqueID }) {
            allDevices.append(device)
        }
        emit(["event": "connected", "key": device.uniqueID, "name": device.localizedName])
    }

    /// Opens a window on an MJPEG stream rather than a capture device.
    ///
    /// How a simulator reaches the screen: `quern-media` captures its
    /// framebuffer and serves it, the server passes the URL here. Filed under
    /// the caller's key -- a udid in practice -- which every event about it
    /// echoes back, exactly as a capture device echoes its name.
    func handleAddStream(key: String, title: String, url: URL, position: Int, id: String? = nil) {
        if sessions[key] != nil {
            emit([
                "event": "add_failed", "key": key,
                "error": "Already previewing", "id": id as Any,
            ])
            return
        }

        let session = StreamPreviewSession(
            sessionKey: key, title: title, url: url, position: position
        )
        session.onWindowClosed = { [weak self] closedKey in
            self?.onWindowClosed(name: closedKey)
        }

        // A stream that fails before it ever connected closes its window and
        // fails the add. One that drops later goes off air and reconnects;
        // the server hears about it so preview_status can say so.
        session.onFailed = { [weak self, weak session] message in
            guard let self, let session, self.sessions[key] === session else { return }
            self.sessions.removeValue(forKey: key)
            self.rebuildPositions()
            self.emit([
                "event": "add_failed", "key": key,
                "error": message, "id": id as Any,
            ])
        }
        session.onOffAir = { [weak self, weak session] reason in
            guard let self, let session, self.sessions[key] === session else { return }
            if let reason {
                self.emit(["event": "off_air", "key": key, "reason": reason])
            } else {
                self.emit(["event": "on_air", "key": key])
            }
        }

        // Acknowledged when the stream's response arrives, not after a fixed
        // delay: a capture device is ready on a timer because CoreMediaIO
        // offers nothing better, whereas a stream says so itself. The same
        // identity re-check applies -- the window can be closed inside the
        // wait, and acknowledging anyway would have the server record a
        // preview with no window and then refuse to open a fresh one.
        session.onConnected = { [weak self, weak session] in
            guard let self else { return }
            guard let session, self.sessions[key] === session else { return }
            self.emit(["event": "added", "key": key, "id": id as Any])
        }

        sessions[key] = session
        positions.insert(position)
        session.start()
    }

    func handleRemove(key: String, id: String? = nil) {
        guard let session = sessions[key] else {
            emit(["event": "error", "message": "Not previewing: \(key)"])
            return
        }

        session.onWindowClosed = nil  // Prevent double event
        session.stop()
        sessions.removeValue(forKey: key)
        // Release position (we don't track which position maps to which session, so just rebuild)
        rebuildPositions()
        emit(["event": "removed", "key": key, "id": id as Any])
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
        for preview in localSimulators.values { preview.stop() }
        localSimulators.removeAll()
        NSApplication.shared.terminate(nil)
    }

    @objc func menuQuit(_ sender: Any?) {
        handleQuit()
    }

    func togglePreview(key: String, position: Int) {
        if sessions[key] != nil {
            closeFromMenu(key)
        } else {
            handleAdd(name: key, position: position)
        }
    }

    /// A preview unticked in the Devices menu is reported as a closed window,
    /// which is what it is to the server. `handleRemove` answers the server's
    /// own remove with `removed`, and the server drops an unsolicited one --
    /// so a capture preview closed this way stayed in preview_status, and a
    /// simulator's quern-media kept running.
    private func closeFromMenu(_ key: String) {
        guard let session = sessions[key] else { return }
        session.onWindowClosed = nil
        session.stop()
        closedByPerson.insert(key)
        onWindowClosed(name: key)
    }

    func toggleSimulator(_ simulator: BootedSimulator, position: Int) {
        if sessions[simulator.udid] != nil {
            closeFromMenu(simulator.udid)
        } else if let local = localSimulators.removeValue(forKey: simulator.udid) {
            local.stop()
        } else if stdinConnected {
            // The server opens it, so it owns the stream; the window arrives
            // as an ordinary add_stream.
            closedByPerson.remove(simulator.udid)
            emit(["event": "open_simulator", "key": simulator.udid, "name": simulator.name])
        } else {
            let preview = LocalSimulatorPreview(simulator: simulator, position: position)
            preview.onEnded = { [weak self] udid, error in
                self?.localSimulators.removeValue(forKey: udid)
                if let error { reportPreviewFailure(simulator.name, error) }
            }
            localSimulators[simulator.udid] = preview
            preview.start()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        return true
    }

    // MARK: Helpers

    func onWindowClosed(name: String) {
        sessions.removeValue(forKey: name)
        rebuildPositions()
        emit(["event": "window_closed", "key": name])
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

// MARK: - Main

setlinebuf(stdout)

let mode = parseArgs()

let app = NSApplication.shared
app.setActivationPolicy(.regular)

let delegate: NSApplicationDelegate
switch mode {
case .interactive:
    delegate = InteractiveDelegate()
default:
    delegate = AppDelegate(mode: mode)
}

app.delegate = delegate
app.activate(ignoringOtherApps: true)
app.run()
