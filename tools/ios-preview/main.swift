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
//          -framework AVFoundation -framework CoreMediaIO -framework AppKit
//
// Named main.swift because it is top-level code: Swift allows that only
// in a file with that name, and it has to compile alongside a second file
// so the frame parser can live somewhere with a test target.

import AVFoundation
import AppKit
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
    func togglePreview(key: String, position: Int)
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
                item.representedObject = device.uniqueID
                if controller.activeSessionKeys.contains(device.uniqueID) {
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
        onWindowClosed?(device.uniqueID)
    }
}

// MARK: - App delegate (standalone mode)

class AppDelegate: NSObject, NSApplicationDelegate, PreviewController {
    var allDevices: [AVCaptureDevice] = []
    var activePreviews: [String: PreviewWindow] = [:]
    let devicesMenuDelegate = DevicesMenuDelegate()
    let mode: FilterMode
    private var deviceObservers: [NSObjectProtocol] = []

    var activeSessionKeys: Set<String> {
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
    private let lock = NSLock()
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var stopped = false
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
        lock.unlock()
        task.resume()
    }

    /// Safe from any queue. `stop()` is called on the main queue, while the
    /// delegate callbacks below run on URLSession's own queue.
    func stop() {
        lock.lock()
        stopped = true
        let task = self.task
        let session = self.session
        self.session = nil
        self.task = nil
        lock.unlock()

        task?.cancel()
        session?.invalidateAndCancel()
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
final class StreamPreviewSession: NSObject, NSWindowDelegate, PreviewSessionKind {
    let sessionKey: String
    let window: NSWindow
    var onWindowClosed: ((String) -> Void)?

    private let url: URL
    private let imageLayer = CALayer()
    private var client: MJPEGClient?
    private var haveSized = false
    private var connected = false
    private var reported = false

    init(sessionKey: String, title: String, url: URL, position: Int) {
        self.sessionKey = sessionKey
        self.url = url

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

        imageLayer.frame = NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight)
        imageLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
        // Matches AVCaptureVideoPreviewLayer's .resizeAspect, so a stream
        // whose window has not been sized yet letterboxes rather than stretches.
        imageLayer.contentsGravity = .resizeAspect
        imageLayer.backgroundColor = NSColor.black.cgColor

        let view = NSView(frame: NSRect(x: 0, y: 0, width: windowWidth, height: windowHeight))
        view.wantsLayer = true
        view.layer?.addSublayer(imageLayer)
        window.contentView = view

        super.init()
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    /// Called once the stream's HTTP response arrives, on the main queue.
    var onConnected: (() -> Void)?

    /// Called once, on the main queue, when the stream ends or fails. The
    /// flag says whether the add had already been acknowledged, which decides
    /// whether the server hears a failed add or a closed window.
    var onFailed: ((Bool, String) -> Void)?

    func start() {
        let client = MJPEGClient(
            url: url,
            onConnected: { [weak self] in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.connected = true
                    self.onConnected?()
                }
            },
            onFrame: { [weak self] image in
                DispatchQueue.main.async { self?.show(image) }
            },
            onError: { [weak self] message in
                // A dead stream used to be logged and nothing else: the
                // window stayed on screen showing its last frame, the
                // URLSession kept the delegate alive, and the server went on
                // believing the preview was running -- the same failure the
                // capture path handles in deviceVanished.
                DispatchQueue.main.async {
                    guard let self, !self.reported else { return }
                    self.reported = true
                    fputs("  stream \(self.sessionKey): \(message)\n", stderr)
                    let wasAcknowledged = self.connected
                    self.stop()
                    self.onFailed?(wasAcknowledged, message)
                }
            }
        )
        self.client = client
        client.start()
    }

    func stop() {
        client?.stop()
        client = nil
        window.delegate = nil
        window.close()
    }

    private func show(_ image: CGImage) {
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
    let devicesMenuDelegate = DevicesMenuDelegate()
    private var deviceObservers: [NSObjectProtocol] = []

    var activeSessionKeys: Set<String> {
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

        // `stop()` clears the window delegate, so a stream that dies reports
        // itself here rather than through windowWillClose. Before the
        // acknowledgement it is a failed add; after it, the window is simply
        // gone, and the server tears down quern-media on that.
        session.onFailed = { [weak self, weak session] wasAcknowledged, message in
            guard let self, let session, self.sessions[key] === session else { return }
            self.sessions.removeValue(forKey: key)
            self.rebuildPositions()
            if wasAcknowledged {
                self.emit(["event": "window_closed", "key": key])
            } else {
                self.emit([
                    "event": "add_failed", "key": key,
                    "error": message, "id": id as Any,
                ])
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
        NSApplication.shared.terminate(nil)
    }

    @objc func menuQuit(_ sender: Any?) {
        handleQuit()
    }

    func togglePreview(key: String, position: Int) {
        if sessions[key] != nil {
            handleRemove(key: key)
        } else {
            handleAdd(name: key, position: position)
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
