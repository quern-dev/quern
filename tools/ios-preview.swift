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
        if seen.insert(d.uniqueID).inserted {
            result.append(d)
        }
    }
    return result
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

func setupMenuBar() {
    let mainMenu = NSMenu()

    // App menu
    let appMenuItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "About Quern Preview", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    let quitItem = NSMenuItem(title: "Quit Quern Preview", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appMenu.addItem(quitItem)
    appMenuItem.submenu = appMenu
    mainMenu.addItem(appMenuItem)

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

func setupMenuBarInteractive(delegate: InteractiveDelegate) {
    let mainMenu = NSMenu()

    // App menu
    let appMenuItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "About Quern Preview", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    let quitItem = NSMenuItem(title: "Quit Quern Preview", action: #selector(InteractiveDelegate.menuQuit(_:)), keyEquivalent: "q")
    quitItem.target = delegate
    appMenu.addItem(quitItem)
    appMenuItem.submenu = appMenu
    mainMenu.addItem(appMenuItem)

    // Devices menu
    let devicesMenuItem = NSMenuItem()
    let devicesMenu = NSMenu(title: "Devices")
    devicesMenu.delegate = delegate.devicesMenuDelegate
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

// MARK: - Devices menu delegate

class DevicesMenuDelegate: NSObject, NSMenuDelegate {
    weak var interactiveDelegate: InteractiveDelegate?

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        guard let delegate = interactiveDelegate else { return }

        let devices = delegate.allDevices
        if devices.isEmpty {
            let noDevices = NSMenuItem(title: "No Devices Found", action: nil, keyEquivalent: "")
            noDevices.isEnabled = false
            menu.addItem(noDevices)
        } else {
            for device in devices {
                let item = NSMenuItem(title: device.localizedName, action: #selector(DevicesMenuDelegate.toggleDevice(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = device.localizedName
                if delegate.sessions[device.localizedName] != nil {
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
        guard let delegate = interactiveDelegate,
              let name = sender.representedObject as? String else { return }

        if delegate.sessions[name] != nil {
            delegate.handleRemove(name: name)
        } else {
            let position = delegate.nextPosition()
            delegate.handleAdd(name: name, position: position)
        }
    }

    @objc func refreshDevices(_ sender: NSMenuItem) {
        guard let delegate = interactiveDelegate else { return }
        enableScreenCaptureDevices()
        delegate.allDevices = discoverDevices()

        let deviceList = delegate.allDevices.map { d -> [String: String] in
            return ["name": d.localizedName, "id": d.uniqueID]
        }
        delegate.emit(["event": "devices", "devices": deviceList, "previewing": Array(delegate.sessions.keys)] as [String: Any])
    }
}

// MARK: - Preview window

class PreviewWindow {
    let window: NSWindow
    let session: AVCaptureSession
    let device: AVCaptureDevice

    init(device: AVCaptureDevice, index: Int) {
        self.device = device
        self.session = AVCaptureSession()

        session.beginConfiguration()
        do {
            let input = try AVCaptureDeviceInput(device: device)
            if session.canAddInput(input) {
                session.addInput(input)
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

        window.makeKeyAndOrderFront(nil)
    }

    func start() { session.startRunning() }
    func stop() { session.stopRunning() }
}

// MARK: - App delegate (standalone mode)

class AppDelegate: NSObject, NSApplicationDelegate {
    var previews: [PreviewWindow] = []
    let mode: FilterMode

    init(mode: FilterMode) {
        self.mode = mode
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        ProcessInfo.processInfo.processName = "Quern Preview"
        loadAppIcon()
        setupMenuBar()
        enableScreenCaptureDevices()
        fputs("Waiting for devices...\n", stderr)

        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
            self.onDevicesReady()
        }
    }

    func onDevicesReady() {
        let allDevices = discoverDevices()

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
            previews.append(preview)
            fputs("  Preview window created for \(device.localizedName)\n", stderr)
        }
        // Stagger session starts to avoid CoreMediaIO race conditions
        startNextSession(index: 0)
        print("Close all windows or Ctrl+C to quit.")
    }

    func startNextSession(index: Int) {
        guard index < previews.count else { return }
        previews[index].start()
        if index + 1 < previews.count {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                self.startNextSession(index: index + 1)
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false  // User quits via ⌘Q or menu
    }
}

// MARK: - Interactive mode: PreviewSession

class PreviewSession: NSObject, NSWindowDelegate {
    let deviceName: String
    let window: NSWindow
    let session: AVCaptureSession
    var onWindowClosed: ((String) -> Void)?

    init(device: AVCaptureDevice, position: Int) {
        self.deviceName = device.localizedName
        self.session = AVCaptureSession()

        session.beginConfiguration()
        do {
            let input = try AVCaptureDeviceInput(device: device)
            if session.canAddInput(input) {
                session.addInput(input)
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
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    func start() { session.startRunning() }

    func stop() {
        session.stopRunning()
        window.delegate = nil
        window.close()
    }

    func windowWillClose(_ notification: Notification) {
        session.stopRunning()
        onWindowClosed?(deviceName)
    }
}

// MARK: - Interactive delegate

class InteractiveDelegate: NSObject, NSApplicationDelegate {
    var sessions: [String: PreviewSession] = [:]
    var allDevices: [AVCaptureDevice] = []
    var positions: Set<Int> = []
    let devicesMenuDelegate = DevicesMenuDelegate()

    func applicationDidFinishLaunching(_ notification: Notification) {
        ProcessInfo.processInfo.processName = "Quern Preview"
        loadAppIcon()
        devicesMenuDelegate.interactiveDelegate = self
        setupMenuBarInteractive(delegate: self)
        enableScreenCaptureDevices()
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
            // EOF — stdin closed
            DispatchQueue.main.async {
                NSApplication.shared.terminate(nil)
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
