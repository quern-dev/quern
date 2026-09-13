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
//   ios-preview --sim-udid <UDID>  # preview a booted simulator (headless)
//
// Build: swiftc -o tools/ios-preview tools/ios-preview.swift -framework AVFoundation -framework CoreMediaIO -framework AppKit

import AVFoundation
import AppKit
import CoreMediaIO
import Foundation
import IOSurface
import ObjectiveC

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
    /// Simulator framebuffer preview. `viaCGImage` selects the copy-through
    /// render path instead of handing the IOSurface to CALayer directly.
    case simUDID(udid: String, viaCGImage: Bool)
    case byArgs([String])
}

func parseArgs() -> FilterMode {
    let args = Array(CommandLine.arguments.dropFirst())
    if args.isEmpty { return .all }
    if args.contains("--list") || args.contains("-l") { return .listOnly }
    if args.contains("--interactive") { return .interactive }
    if let i = args.firstIndex(of: "--sim-udid"), i + 1 < args.count {
        return .simUDID(udid: args[i + 1], viaCGImage: args.contains("--cgimage"))
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
        case .listOnly, .interactive, .simUDID:
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
    private let onSurface: (IOSurface) -> Void

    private var ioClient: NSObject?
    private var descriptors: [NSObject] = []
    private var callbackUUIDs: [ObjectIdentifier: NSUUID] = [:]

    init(udid: String, onSurface: @escaping (IOSurface) -> Void) {
        self.udid = udid
        self.onSurface = onSurface
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
        if let best { onSurface(best) }
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
    private var framebuffer: SimFramebuffer?

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

    func start() throws {
        let fb = SimFramebuffer(udid: udid) { [weak self] surface in
            self?.present(surface)
        }
        try fb.start()
        framebuffer = fb
    }

    func stop() {
        framebuffer?.stop()
        framebuffer = nil
        window.delegate = nil
        window.close()
    }

    func windowWillClose(_ notification: Notification) {
        framebuffer?.stop()
        framebuffer = nil
        onWindowClosed?(udid)
    }

    private func present(_ surface: IOSurface) {
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

// MARK: - Simulator mode app delegate

class SimAppDelegate: NSObject, NSApplicationDelegate {
    private let udid: String
    private let viaCGImage: Bool
    private var preview: SimPreviewWindow?

    init(udid: String, viaCGImage: Bool) {
        self.udid = udid
        self.viaCGImage = viaCGImage
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        loadAppIcon()
        let preview = SimPreviewWindow(udid: udid, viaCGImage: viaCGImage)
        preview.onWindowClosed = { _ in NSApplication.shared.terminate(nil) }
        do {
            try preview.start()
            self.preview = preview
            simLogErr("[sim-preview] streaming \(udid)")
        } catch {
            simLogErr("[sim-preview] failed: \(error)")
            NSApplication.shared.terminate(nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
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
case .simUDID(let udid, let viaCGImage):
    delegate = SimAppDelegate(udid: udid, viaCGImage: viaCGImage)
default:
    delegate = AppDelegate(mode: mode)
}

app.delegate = delegate
app.activate(ignoringOtherApps: true)
app.run()
