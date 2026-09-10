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
// Build: swiftc -o tools/ios-preview tools/ios-preview.swift -framework AVFoundation -framework CoreMediaIO -framework AppKit

import AVFoundation
import AppKit
import CoreMediaIO
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
        case .listOnly, .interactive:
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
        switch cmd {
        case "add":
            guard let name = json["name"] as? String else {
                emit(["event": "error", "message": "add requires 'name'"])
                return
            }
            let position = json["position"] as? Int ?? nextPosition()
            handleAdd(name: name, position: position)

        case "remove":
            guard let name = json["name"] as? String else {
                emit(["event": "error", "message": "remove requires 'name'"])
                return
            }
            handleRemove(name: name)

        case "list":
            handleList()

        case "quit":
            handleQuit()

        default:
            emit(["event": "error", "message": "Unknown command: \(cmd)"])
        }
    }

    // MARK: Command handlers

    func handleAdd(name: String, position: Int) {
        // Already previewing?
        if sessions[name] != nil {
            emit(["event": "add_failed", "name": name, "error": "Already previewing"])
            return
        }

        // Find device by exact name
        guard let device = allDevices.first(where: { $0.localizedName == name }) else {
            emit(["event": "add_failed", "name": name, "error": "Device not found"])
            return
        }

        // Create session
        let session = PreviewSession(device: device, position: position)

        if session.session.inputs.isEmpty {
            session.stop()
            emit(["event": "add_failed", "name": name, "error": "Cannot create input"])
            return
        }

        session.onWindowClosed = { [weak self] closedName in
            self?.onWindowClosed(name: closedName)
        }

        sessions[name] = session
        positions.insert(position)

        // Start capture, then emit added after a brief delay for CoreMediaIO
        session.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            self.emit(["event": "added", "name": name])
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

    func handleRemove(name: String) {
        guard let session = sessions[name] else {
            emit(["event": "error", "message": "Not previewing: \(name)"])
            return
        }

        session.onWindowClosed = nil  // Prevent double event
        session.stop()
        sessions.removeValue(forKey: name)
        // Release position (we don't track which position maps to which session, so just rebuild)
        rebuildPositions()
        emit(["event": "removed", "name": name])
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
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let str = String(data: data, encoding: .utf8) else {
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
