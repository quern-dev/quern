#!/usr/bin/env swift
// sim-bridge.swift — Native simulator control for Quern
//
// Long-lived subprocess that exposes accessibility tree queries,
// HID input injection, and IOSurface screenshots via a JSON Lines
// protocol over stdin/stdout. Replaces idb for simulator UI automation.
//
// Uses Apple's private frameworks (CoreSimulator, SimulatorKit,
// AccessibilityPlatformTranslation) loaded at runtime via dlopen.
// Requires Xcode 26+ and Apple Silicon.
//
// Techniques ported from github.com/tddworks/baguette (MIT),
// which credits cameroncooke/AXe and Silbercue/SilbercueSwift.

import Foundation
import ObjectiveC
import CoreGraphics
import IOSurface
import ImageIO

// MARK: - Logging

func logErr(_ message: String) {
    fputs("[sim-bridge] \(message)\n", stderr)
}

func dlerrorString() -> String {
    guard let err = dlerror() else { return "(null)" }
    return String(cString: err)
}

// MARK: - Framework Loading

nonisolated(unsafe) private var frameworksLoaded = false

func loadFrameworks() {
    guard !frameworksLoaded else { return }
    frameworksLoaded = true

    let coreSim = "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator"
    if dlopen(coreSim, RTLD_NOW | RTLD_GLOBAL) == nil {
        logErr("CoreSimulator load failed: \(dlerrorString())")
    }

    let dev = developerDir()
    if let simKit = simulatorKitPath(at: dev) {
        if dlopen(simKit, RTLD_NOW | RTLD_GLOBAL) == nil {
            logErr("SimulatorKit load failed: \(dlerrorString())")
        }
    } else {
        // Name both layouts: "not found" against a developer directory that
        // plainly contains Xcode sends the reader looking in the wrong place.
        logErr("""
            SimulatorKit not found under \(dev). Looked for \
            \(simulatorKitRelativePaths.joined(separator: " and ")).
            """)
    }

    let axPath = "/System/Library/PrivateFrameworks/AccessibilityPlatformTranslation.framework/AccessibilityPlatformTranslation"
    if dlopen(axPath, RTLD_NOW | RTLD_GLOBAL) == nil {
        logErr("AccessibilityPlatformTranslation load failed: \(dlerrorString())")
    }
}

func developerDir() -> String {
    if let dev = xcodeSelectDir(), hasSimulatorKit(at: dev) { return dev }
    if let dev = scanApplications() { return dev }
    return xcodeSelectDir() ?? "/Applications/Xcode.app/Contents/Developer"
}

private func xcodeSelectDir() -> String? {
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

/// Where the SimulatorKit binary sits, relative to a developer directory.
///
/// Xcode 27 moved it out of the developer directory altogether:
///
///   <= 26  Xcode.app/Contents/Developer/Library/PrivateFrameworks/SimulatorKit.framework
///   27+    Xcode.app/Contents/SharedFrameworks/SimulatorKit.framework
///
/// The second is a *sibling* of `Developer`, not a relocation within it, so
/// nothing rooted at the developer directory can reach it without stepping up
/// a level. Both are checked rather than switching on a version: it costs one
/// `stat`, and it keeps working whichever layout the next Xcode ships.
private let simulatorKitRelativePaths = [
    "Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit",
    "../SharedFrameworks/SimulatorKit.framework/SimulatorKit",
]

/// The SimulatorKit binary for this developer directory, or nil if absent.
///
/// Returns the path rather than a yes/no so that every caller loads the file it
/// actually found. Answering "yes it exists" and leaving each caller to rebuild
/// the path from a constant is how three call sites came to disagree with
/// reality at once.
func simulatorKitPath(at dev: String) -> String? {
    for relative in simulatorKitRelativePaths {
        let path = ((dev as NSString).appendingPathComponent(relative) as NSString)
            .standardizingPath
        if FileManager.default.fileExists(atPath: path) { return path }
    }
    return nil
}

private func hasSimulatorKit(at dev: String) -> Bool {
    return simulatorKitPath(at: dev) != nil
}

private func scanApplications() -> String? {
    let canonical = "/Applications/Xcode.app/Contents/Developer"
    if hasSimulatorKit(at: canonical) { return canonical }
    let entries = (try? FileManager.default.contentsOfDirectory(atPath: "/Applications")) ?? []
    for app in entries.sorted()
    where app.hasPrefix("Xcode") && app.hasSuffix(".app") && app != "Xcode.app" {
        let dev = "/Applications/\(app)/Contents/Developer"
        if hasSimulatorKit(at: dev) { return dev }
    }
    return nil
}

// MARK: - ObjC Runtime Helpers

func invokeClassObjWithObjAndError(
    _ cls: AnyClass, _ sel: Selector, _ arg: AnyObject, _ err: inout NSError?
) -> NSObject? {
    guard let metaCls = object_getClass(cls),
          let imp = class_getMethodImplementation(metaCls, sel) else { return nil }
    typealias Fn = @convention(c) (AnyClass, Selector, AnyObject, AutoreleasingUnsafeMutablePointer<NSError?>) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(cls, sel, arg, &err) as? NSObject
}

func invokeObjWithError(
    _ target: NSObject, _ sel: Selector, _ err: inout NSError?
) -> NSObject? {
    guard let imp = class_getMethodImplementation(type(of: target), sel) else { return nil }
    typealias Fn = @convention(c) (AnyObject, Selector, AutoreleasingUnsafeMutablePointer<NSError?>) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(target, sel, &err) as? NSObject
}

// MARK: - Device Discovery

func sharedServiceContext() -> NSObject? {
    guard let cls = NSClassFromString("SimServiceContext") else { return nil }
    let sel = NSSelectorFromString("sharedServiceContextForDeveloperDir:error:")
    var err: NSError?
    let ctx = invokeClassObjWithObjAndError(cls, sel, developerDir() as NSString, &err)
    if ctx == nil, let err { logErr("sharedServiceContext: \(err)") }
    return ctx
}

func defaultDeviceSet() -> NSObject? {
    guard let ctx = sharedServiceContext() else { return nil }
    let sel = NSSelectorFromString("defaultDeviceSetWithError:")
    guard ctx.responds(to: sel) else { return nil }
    var err: NSError?
    return invokeObjWithError(ctx, sel, &err)
}

func availableDevices() -> [NSObject] {
    guard let set = defaultDeviceSet() else { return [] }
    return (set.value(forKey: "availableDevices") as? [NSObject]) ?? []
}

func resolveDevice(udid: String) -> NSObject? {
    for device in availableDevices() {
        if (device.value(forKey: "UDID") as? NSUUID)?.uuidString == udid {
            return device
        }
    }
    return nil
}

func listDevices() -> [[String: Any]] {
    availableDevices().map { device in
        let udid = (device.value(forKey: "UDID") as? NSUUID)?.uuidString ?? ""
        let name = (device.value(forKey: "name") as? String) ?? "Unknown"
        let rawState = (device.value(forKey: "state") as? NSNumber)?.uintValue ?? 1
        let state: String
        switch rawState {
        case 3: state = "booted"
        default: state = "shutdown"
        }
        let runtimeName = (device.value(forKey: "runtime") as? NSObject).flatMap { rt -> String? in
            (rt.value(forKey: "name") as? String) ?? (rt.value(forKey: "versionString") as? String)
        } ?? ""
        return ["udid": udid, "name": name, "state": state, "runtime": runtimeName]
    }
}

func devicePointSize(for device: NSObject) -> CGSize {
    let fallback = CGSize(width: 393, height: 852)
    guard let deviceType = device.value(forKey: "deviceType") as? NSObject else { return fallback }
    let pixelSize: CGSize
    if let raw = deviceType.value(forKey: "mainScreenSize") as? CGSize {
        pixelSize = raw
    } else if let nsv = deviceType.value(forKey: "mainScreenSize") as? NSValue {
        pixelSize = nsv.sizeValue
    } else {
        return fallback
    }
    let scale = (deviceType.value(forKey: "mainScreenScale") as? NSNumber)?.doubleValue ?? 3.0
    guard scale > 0 else { return fallback }
    return CGSize(width: pixelSize.width / scale, height: pixelSize.height / scale)
}

// MARK: - Panels (#536)
//
// iPhone Duo (Xcode 27.1) is one device with two built-in panels: a cover
// (screen 1, `primary`, lit folded) and an inner panel (screen 3,
// `primary-1`, lit unfolded, landscape). Everything above assumed one screen
// -- `devicePointSize` is the device type's *main* screen, touches went to
// `0x32` -- so once the Duo unfolded every tap was accepted and landed
// nowhere, screenshots were of the dark cover, and accessibility frames were
// squeezed into the cover's size. Measured on Xcode 27.1 / iOS 27.1:
//
// - Each built-in panel takes touch on its own digitizer, `0x40000000 |
//   screenID`; `0x32` is a slot the last-created panel owns, and reached
//   neither panel when unfolded. (tddworks/baguette found the registration
//   and documented it in docs/features/iphone-duo/design.md.)
// - Nothing on the host says which panel is lit: both report powered and
//   connected, and their backlight state never changes with the fold. The
//   framebuffer does -- the unlit panel is black -- so that is the test.
// - The digitizer and accessibility both work in the panel's own portrait
//   points; `uiOrientation` (UIDeviceOrientation: 1 portrait, 2 upside down,
//   3 landscape left, 4 landscape right) says how the interface is turned on
//   it. Callers work in the interface's points, so both are rotated here.
//
// A device with one built-in panel never reaches any of this: `litPanel`
// answers nil and every caller keeps the single-screen path unchanged.

struct Panel {
    let screenID: UInt32
    let deviceName: String
    /// UIDeviceOrientation of the interface on this panel.
    let orientation: UInt32
    /// Points, laid out as the framebuffer is (portrait for both Duo panels).
    let nativeSize: CGSize
    let lit: Bool
    /// The device's other built-in panels' native sizes: what SpringBoard's
    /// stale root reports while this one is lit (see `AXFrameTransform`).
    var otherNativeSizes: [CGSize] = []

    var target: UInt32 { 0x4000_0000 | screenID }
    var landscape: Bool { orientation == 3 || orientation == 4 }
    /// Points as the interface is turned: what callers tap and read in.
    var screenSize: CGSize {
        landscape ? CGSize(width: nativeSize.height, height: nativeSize.width) : nativeSize
    }

    /// Interface point -> panel point.
    func toPanel(_ p: CGPoint) -> CGPoint {
        let w = nativeSize.width, h = nativeSize.height
        switch orientation {
        case 2: return CGPoint(x: w - p.x, y: h - p.y)
        case 3: return CGPoint(x: p.y, y: h - p.x)
        case 4: return CGPoint(x: w - p.y, y: p.x)
        default: return p
        }
    }

    /// Panel rect -> interface rect: the inverse of `toPanel`, for frames.
    func toScreen(_ r: CGRect) -> CGRect {
        let w = nativeSize.width, h = nativeSize.height
        switch orientation {
        case 2: return CGRect(x: w - r.maxX, y: h - r.maxY, width: r.width, height: r.height)
        case 3: return CGRect(x: h - r.maxY, y: r.minX, width: r.height, height: r.width)
        case 4: return CGRect(x: r.minY, y: w - r.maxX, width: r.height, height: r.width)
        default: return r
        }
    }

    func json(includeLit: Bool = true) -> [String: Any] {
        var d: [String: Any] = [
            "screen_id": Int(screenID), "device_name": deviceName,
            "orientation": Int(orientation),
            "native_size": [Double(nativeSize.width), Double(nativeSize.height)],
            "size": [Double(screenSize.width), Double(screenSize.height)],
        ]
        if includeLit { d["lit"] = lit }
        return d
    }
}

/// The screen properties a display port's descriptor carries, read through
/// typed IMPs: the descriptor and its properties are XPC proxies that claim
/// every selector, so `value(forKey:)` on a wrong guess raises rather than
/// returning nil. `screenType` 0 is a built-in panel.
///
/// Neither `responds(to:)` nor `class_getMethodImplementation` can say
/// whether a proxy has a method -- the first answers yes to everything, the
/// second returns the forwarding IMP -- and an unrecognised selector raises
/// an exception Swift cannot catch, which would end the bridge on every tap.
/// So what is called is checked against the interfaces CoreSimulator declares
/// (review): an Xcode whose `SimScreen` / `SimScreenProperties` lack any of
/// these has no panels as far as this file is concerned, and every caller
/// stays on the single-screen path. The return types are the declared ones
/// (`I` for screenID and uiOrientation, `Q` for screenType).
///
/// Asked on first use, not as a global initialiser: this file is top-level
/// code, so a `let` here runs at launch, before CoreSimulator is loaded, and
/// answered false for the life of the bridge -- every Duo read as one screen.
/// Only a yes is kept; a no is cheap to ask again.
nonisolated(unsafe) var screenPropertiesConfirmed = false

/// Every declaration the panel code calls, with the return type it reads
/// it as: what is missing or typed otherwise, by name. Empty means all are
/// there. Pure and uncached, so the self-test can hold the cached answer to
/// it -- and say which one changed, on an Xcode that changes one.
func screenInterfaceProblems() -> [String] {
    let wanted: [(String, String, String)] = [
        ("SimScreen", "screenProperties", "@"),
        ("SimScreenProperties", "screenID", "I"),
        ("SimScreenProperties", "uiOrientation", "I"),
        ("SimScreenProperties", "screenType", "Q"),
        ("SimScreenProperties", "deviceName", "@"),
    ]
    var problems: [String] = []
    for (protoName, method, type) in wanted {
        guard let proto = objc_getProtocol(protoName) else {
            problems.append("\(protoName) is not declared"); continue
        }
        let d = protocol_getMethodDescription(proto, NSSelectorFromString(method), true, true)
        guard d.name != nil, let types = d.types else {
            problems.append("\(protoName) has no \(method)"); continue
        }
        let encoding = String(cString: types)
        if !encoding.hasPrefix(type) {
            problems.append("\(protoName).\(method) returns \(encoding.prefix(1)), not \(type)")
        }
    }
    return problems
}

func screenPropertiesAvailable() -> Bool {
    if screenPropertiesConfirmed { return true }
    screenPropertiesConfirmed = screenInterfaceProblems().isEmpty
    return screenPropertiesConfirmed
}

/// Whether a device can be remembered as having one panel, from one read.
/// One built-in panel, or none on an Xcode without the screen interfaces:
/// there every device reads none for good, since CoreSimulator is loaded
/// before the bridge answers anything, and asking again cost each screenshot
/// a trip through the bridge's lock on Xcode 26 (review). None on an Xcode
/// that has them is a device still booting, and is asked again.
func recordsSinglePanel(found: Int, interfacesAvailable: Bool) -> Bool {
    found == 1 || (found == 0 && !interfacesAvailable)
}

func screenProperties(of descriptor: NSObject)
    -> (screenID: UInt32, deviceName: String, screenType: UInt64, orientation: UInt32)? {
    guard screenPropertiesAvailable(),
          let screen = objc_getProtocol("SimScreen"), descriptor.conforms(to: screen) else { return nil }
    let propsSel = NSSelectorFromString("screenProperties")
    guard let props = descriptor.perform(propsSel)?.takeUnretainedValue() as? NSObject,
          let cls = object_getClass(props) else { return nil }
    typealias U32Fn = @convention(c) (AnyObject, Selector) -> UInt32
    typealias U64Fn = @convention(c) (AnyObject, Selector) -> UInt64
    typealias ObjFn = @convention(c) (AnyObject, Selector) -> AnyObject?
    func imp(_ name: String) -> (IMP, Selector)? {
        let sel = NSSelectorFromString(name)
        return class_getMethodImplementation(cls, sel).map { ($0, sel) }
    }
    guard let (idImp, idSel) = imp("screenID"), let (oImp, oSel) = imp("uiOrientation"),
          let (typeImp, typeSel) = imp("screenType"),
          let (nameImp, nameSel) = imp("deviceName") else { return nil }
    let id = unsafeBitCast(idImp, to: U32Fn.self)(props, idSel)
    let orientation = unsafeBitCast(oImp, to: U32Fn.self)(props, oSel)
    let type = unsafeBitCast(typeImp, to: U64Fn.self)(props, typeSel)
    let name = unsafeBitCast(nameImp, to: ObjFn.self)(props, nameSel) as? String ?? ""
    return (id, name, type, orientation)
}

/// Whether a framebuffer shows anything: 256 samples on a grid, any channel
/// above 8. An unlit panel is exactly black; a lit one showing a dark
/// interface still has a status bar.
func surfaceIsLit(_ surface: IOSurface) -> Bool {
    IOSurfaceLock(surface, .readOnly, nil)
    defer { IOSurfaceUnlock(surface, .readOnly, nil) }
    let w = IOSurfaceGetWidth(surface), h = IOSurfaceGetHeight(surface)
    let bpr = IOSurfaceGetBytesPerRow(surface)
    guard w > 0, h > 0 else { return false }
    let base = IOSurfaceGetBaseAddress(surface).assumingMemoryBound(to: UInt8.self)
    for gy in 0..<16 {
        for gx in 0..<16 {
            let px = base + ((gy * 2 + 1) * h / 32) * bpr + ((gx * 2 + 1) * w / 32) * 4
            if px[0] > 8 || px[1] > 8 || px[2] > 8 { return true }
        }
    }
    return false
}

/// Every built-in panel's display descriptor, with what it says about itself.
func integratedPanels(device: NSObject) -> [(Panel, NSObject)] {
    guard screenPropertiesAvailable(),
          let io = device.perform(NSSelectorFromString("io"))?.takeUnretainedValue() as? NSObject
    else { return [] }
    io.perform(NSSelectorFromString("updateIOPorts"))
    guard let ports = io.value(forKey: "deviceIOPorts") as? [NSObject] else { return [] }
    let scale = (device.value(forKey: "deviceType") as? NSObject)
        .flatMap { ($0.value(forKey: "mainScreenScale") as? NSNumber)?.doubleValue } ?? 3.0
    let pidSel = NSSelectorFromString("portIdentifier"), descSel = NSSelectorFromString("descriptor")
    let surfSel = NSSelectorFromString("framebufferSurface")
    var found: [(Panel, NSObject)] = []
    for port in ports where port.responds(to: pidSel) {
        guard let pid = port.perform(pidSel)?.takeUnretainedValue(),
              "\(pid)" == "com.apple.framebuffer.display",
              let desc = port.perform(descSel)?.takeUnretainedValue() as? NSObject,
              let props = screenProperties(of: desc), props.screenType == 0,
              let surfObj = desc.perform(surfSel)?.takeUnretainedValue() else { continue }
        let surface = unsafeBitCast(surfObj, to: IOSurface.self)
        let native = CGSize(width: Double(IOSurfaceGetWidth(surface)) / scale,
                            height: Double(IOSurfaceGetHeight(surface)) / scale)
        found.append((Panel(screenID: props.screenID, deviceName: props.deviceName,
                            orientation: props.orientation, nativeSize: native,
                            lit: surfaceIsLit(surface)), desc))
    }
    return found.sorted { $0.0.screenID < $1.0.screenID }
}

/// The last panel each device was found lit on, for the moment neither or
/// both read lit -- mid-fold, or a panel showing a black screen.
nonisolated(unsafe) var lastLitPanel: [String: UInt32] = [:]

/// Devices found to have exactly one built-in panel. How many panels a
/// device has is its device type's, so it never changes for a UDID; and
/// finding out costs ~15ms of XPC and framebuffer reads, which a phone would
/// otherwise pay on every tap and every read for nothing. Recorded only on a
/// definite one: a device still booting reports none, and is asked again.
nonisolated(unsafe) var singlePanelDevices: Set<String> = []

/// Which panel is lit, from what each says about itself -- pure, so the
/// self-test drives it. Nil for fewer than two panels: a phone, and the one
/// case this must never get wrong, since every caller then keeps the
/// single-screen path. Neither or both lit (mid-fold, or a lit panel showing
/// a black screen): the one lit last, else the cover, which a Duo boots on.
func choosePanel(_ panels: [Panel], last: UInt32?) -> Panel? {
    guard panels.count > 1 else { return nil }
    let lit = panels.filter { $0.lit }
    var chosen: Panel
    if lit.count == 1 {
        chosen = lit[0]
    } else if let last, let p = panels.first(where: { $0.screenID == last }) {
        chosen = p
    } else {
        chosen = panels.first(where: { $0.deviceName == "primary" }) ?? panels[0]
    }
    chosen.otherNativeSizes = panels.filter { $0.screenID != chosen.screenID }.map { $0.nativeSize }
    return chosen
}

/// The lit panel of a device with more than one built-in panel, its
/// descriptor, and every built-in panel; nil lit for every other device.
func readPanels(udid: String, device: NSObject) -> (lit: (Panel, NSObject)?, all: [Panel]) {
    if singlePanelDevices.contains(udid) { return (nil, []) }
    let found = integratedPanels(device: device)
    if recordsSinglePanel(found: found.count, interfacesAvailable: screenPropertiesAvailable()) {
        singlePanelDevices.insert(udid)
    }
    let all = found.map { $0.0 }
    guard let chosen = choosePanel(all, last: lastLitPanel[udid]),
          let desc = found.first(where: { $0.0.screenID == chosen.screenID })?.1 else { return (nil, all) }
    lastLitPanel[udid] = chosen.screenID
    return ((chosen, desc), all)
}

func litPanelAndDescriptor(udid: String, device: NSObject) -> (Panel, NSObject)? {
    readPanels(udid: udid, device: device).lit
}

func litPanel(udid: String, device: NSObject) -> Panel? {
    litPanelAndDescriptor(udid: udid, device: device)?.0
}

/// The rotations checked against measurements on the Duo's inner panel
/// (669x951 native points; Xcode 27.1, iOS 27.1). Each case is a tap that
/// opened the app it aimed at, or a frame read against the screenshot.
func panelSelfTest() -> [String] {
    var failures: [String] = []
    func panel(_ o: UInt32) -> Panel {
        Panel(screenID: 3, deviceName: "primary-1", orientation: o,
              nativeSize: CGSize(width: 669, height: 951), lit: true)
    }
    func near(_ a: CGFloat, _ b: CGFloat, _ tol: CGFloat = 0.002) -> Bool { abs(a - b) <= tol }
    // A screen fraction -> the digitizer fraction that landed on it.
    let taps: [(UInt32, CGPoint, CGPoint)] = [
        (3, CGPoint(x: 0.570, y: 0.123), CGPoint(x: 0.123, y: 0.430)),   // Settings icon
        (4, CGPoint(x: 0.570, y: 0.123), CGPoint(x: 0.877, y: 0.570)),   // Settings icon
        (4, CGPoint(x: 0.947, y: 0.569), CGPoint(x: 0.431, y: 0.947)),   // Safari icon
        (2, CGPoint(x: 0.261, y: 0.120), CGPoint(x: 0.739, y: 0.880)),   // Settings icon
        (1, CGPoint(x: 0.261, y: 0.120), CGPoint(x: 0.261, y: 0.120)),
    ]
    for (o, screen, expected) in taps {
        let p = panel(o)
        let space = TouchSpace(size: p.screenSize, target: p.target, panel: p)
        let got = space.normalize(CGPoint(x: screen.x * p.screenSize.width,
                                          y: screen.y * p.screenSize.height))
        if !near(got.x, expected.x) || !near(got.y, expected.y) {
            failures.append("orientation \(o): screen \(screen) -> \(got), measured \(expected)")
        }
    }
    // The home screen's Safari frame, native points -> where it is on screen
    // in landscape right: (871, 348) 64x64, read against the screenshot.
    let safari = panel(4).toScreen(CGRect(x: 257.3, y: 871.2, width: 64, height: 64))
    if !near(safari.minX, 871.2, 1) || !near(safari.minY, 347.7, 1) {
        failures.append("orientation 4: Safari frame -> \(safari), measured (871, 348)")
    }
    // toScreen undoes toPanel, every orientation.
    for o: UInt32 in 1...4 {
        let p = panel(o)
        let point = CGPoint(x: 123, y: 45)
        let there = p.toPanel(point)
        let back = p.toScreen(CGRect(origin: there, size: .zero)).origin
        if !near(back.x, point.x, 0.01) || !near(back.y, point.y, 0.01) {
            failures.append("orientation \(o): toScreen(toPanel(\(point))) = \(back)")
        }
    }
    // A single-panel device keeps the old space: its own size, 0x32, no turn.
    let flat = TouchSpace(size: CGSize(width: 393, height: 852), target: touchDigitizer, panel: nil)
    let mid = flat.normalize(CGPoint(x: 196.5, y: 426))
    if !near(mid.x, 0.5) || !near(mid.y, 0.5) || flat.target != 0x32 {
        failures.append("single panel: \(mid) target \(flat.target)")
    }
    if panel(3).target != 0x4000_0003 { failures.append("target \(panel(3).target)") }

    // Frames with a size: a SpringBoard icon, panel -> screen -> panel, and
    // its width and height swap in landscape (review: only zero-size rects
    // were checked, so min/max and swap errors survived).
    let icon = CGRect(x: 257.3, y: 800.0, width: 64, height: 90)
    for o: UInt32 in 1...4 {
        let p = panel(o)
        let onScreen = p.toScreen(icon)
        let swapped = p.landscape
        if !near(onScreen.width, swapped ? 90 : 64, 0.01) || !near(onScreen.height, swapped ? 64 : 90, 0.01) {
            failures.append("orientation \(o): size \(onScreen.size)")
        }
        if onScreen.minX < 0 || onScreen.minY < 0 || onScreen.maxX > p.screenSize.width + 0.01
            || onScreen.maxY > p.screenSize.height + 0.01 {
            failures.append("orientation \(o): \(onScreen) leaves the \(p.screenSize) screen")
        }
        // Each corner of the screen rect, taken back, is a corner of the icon.
        let corners = [CGPoint(x: onScreen.minX, y: onScreen.minY), CGPoint(x: onScreen.maxX, y: onScreen.maxY)]
            .map { p.toPanel($0) }
        let back = CGRect(x: min(corners[0].x, corners[1].x), y: min(corners[0].y, corners[1].y),
                          width: abs(corners[0].x - corners[1].x), height: abs(corners[0].y - corners[1].y))
        if !near(back.minX, icon.minX, 0.01) || !near(back.minY, icon.minY, 0.01)
            || !near(back.width, icon.width, 0.01) || !near(back.height, icon.height, 0.01) {
            failures.append("orientation \(o): \(icon) -> \(onScreen) -> \(back)")
        }
    }

    // Screenshot rotation: a marker pixel at the framebuffer's top-left lands
    // where `toScreen` puts that corner, every orientation.
    for o: UInt32 in 1...4 {
        let small = Panel(screenID: 3, deviceName: "primary-1", orientation: o,
                          nativeSize: CGSize(width: 4, height: 2), lit: true)
        guard let marked = markerImage(width: 4, height: 2),
              let turned = rotated(marked, orientation: o),
              let at = markerPosition(turned) else {
            failures.append("orientation \(o): screenshot rotation could not run"); continue
        }
        let expected = small.toScreen(CGRect(x: 0, y: 0, width: 1, height: 1)).origin
        if Int(expected.x) != at.x || Int(expected.y) != at.y || turned.width != Int(small.screenSize.width) {
            failures.append("orientation \(o): marker at \(at) in \(turned.width)x\(turned.height), expected \(expected)")
        }
    }

    // The AX rule: rotate only a root the size of another panel (SpringBoard).
    var inner = panel(3)
    inner.otherNativeSizes = [CGSize(width: 466, height: 678)]
    let springboard = AXFrameTransform(rootFrame: CGRect(x: 0, y: 0, width: 466, height: 678),
                                       pointSize: .zero, panel: inner,
                                       panelSpace: AXFrameTransform.panelSpace(
                                           root: CGRect(x: 0, y: 0, width: 466, height: 678), panel: inner))
    let app = AXFrameTransform(rootFrame: CGRect(x: 0, y: 0, width: 669, height: 951),
                               pointSize: .zero, panel: inner,
                               panelSpace: AXFrameTransform.panelSpace(
                                   root: CGRect(x: 0, y: 0, width: 669, height: 951), panel: inner))
    let odd = AXFrameTransform.panelSpace(root: CGRect(x: 0, y: 0, width: 402, height: 874), panel: inner)
    if !springboard.panelSpace || app.panelSpace || odd {
        failures.append("panelSpace: springboard \(springboard.panelSpace) app \(app.panelSpace) other \(odd)")
    }
    let r = CGRect(x: 10, y: 20, width: 30, height: 40)
    if app.map(r) != r { failures.append("an app's frames were turned: \(app.map(r))") }
    if springboard.map(r) != inner.toScreen(r) { failures.append("SpringBoard's frames were not turned") }
    // Hit-tests take panel points whichever the tree's space (measured).
    let hit = CGPoint(x: 691, y: 374)
    if app.unmap(hit) != inner.toPanel(hit) || springboard.unmap(hit) != inner.toPanel(hit) {
        failures.append("unmap is not toPanel: \(app.unmap(hit)), \(springboard.unmap(hit))")
    }

    // Choosing the lit panel.
    func p(_ id: UInt32, _ name: String, _ lit: Bool) -> Panel {
        Panel(screenID: id, deviceName: name, orientation: 1,
              nativeSize: CGSize(width: id == 1 ? 466 : 669, height: id == 1 ? 678 : 951), lit: lit)
    }
    if choosePanel([p(1, "PurpleMain", true)], last: nil) != nil {
        failures.append("a one-panel device chose a panel")
    }
    if choosePanel([], last: 3) != nil { failures.append("no panels chose one") }
    if choosePanel([p(1, "primary", false), p(3, "primary-1", true)], last: 1)?.screenID != 3 {
        failures.append("the lit panel was not chosen")
    }
    if choosePanel([p(1, "primary", false), p(3, "primary-1", false)], last: 3)?.screenID != 3 {
        failures.append("neither lit: the last lit was not kept")
    }
    if choosePanel([p(1, "primary", true), p(3, "primary-1", true)], last: nil)?.screenID != 1 {
        failures.append("both lit, nothing known: not the cover")
    }
    if choosePanel([p(3, "primary-1", false), p(1, "primary", false)], last: nil)?.screenID != 1 {
        failures.append("neither lit, nothing known: not the cover")
    }
    if let chosen = choosePanel([p(1, "primary", false), p(3, "primary-1", true)], last: nil),
       chosen.otherNativeSizes != [CGSize(width: 466, height: 678)] {
        failures.append("other sizes: \(chosen.otherNativeSizes)")
    }

    // Lit or black, on a synthetic framebuffer.
    for (value, expected) in [(0, false), (6, false), (20, true), (255, true)] {
        guard let surface = filledSurface(width: 64, height: 64, value: UInt8(value)) else {
            failures.append("could not make a test surface"); break
        }
        if surfaceIsLit(surface) != expected { failures.append("surface of \(value) read lit=\(!expected)") }
    }

    // The interface check answers from what is loaded now: as a global
    // initialiser it ran before CoreSimulator was, said no for good, and
    // every Duo read as one screen -- found live, after every other check here
    // passed. So the cached answer must agree with a fresh look, whatever this
    // Xcode declares. (CI's Xcode 26.6 declares only part of it, which is not a
    // failure: it has no foldable. What it lacks is reported, below.)
    if screenPropertiesAvailable() != screenInterfaceProblems().isEmpty {
        failures.append("the interface check says \(screenPropertiesAvailable()) but CoreSimulator " +
                        "declares \(screenInterfaceProblems().isEmpty ? "all of them" : "\(screenInterfaceProblems())")")
    }
    // A device is remembered as one-panel on one panel, or on none where the
    // interfaces are missing; not on none where they are (a booting device).
    if !recordsSinglePanel(found: 1, interfacesAvailable: true)
        || !recordsSinglePanel(found: 0, interfacesAvailable: false)
        || recordsSinglePanel(found: 0, interfacesAvailable: true)
        || recordsSinglePanel(found: 2, interfacesAvailable: true) {
        failures.append("recordsSinglePanel decides wrongly")
    }

    // The target is what reaches the message: 0x6c and 0x10c (review: a
    // wrapAndPatch writing 0x32 regardless survived).
    if resolveHIDSymbols(),
       let event = makeDigitizerEvent(point: CGPoint(x: 0.5, y: 0.5), identifier: 1, isDown: true) {
        for t: UInt32 in [touchDigitizer, 0x4000_0003] {
            guard let msg = withExtendedLifetime(event, { wrapAndPatch(event: event, target: t) }) else {
                failures.append("no message for target \(t)"); continue
            }
            let a = msg.load(fromByteOffset: 0x6c, as: UInt32.self)
            let b = malloc_size(msg) >= 0x110 ? msg.load(fromByteOffset: 0x10c, as: UInt32.self) : t
            free(msg)
            if a != t || b != t { failures.append("target \(t) wrote \(a), \(b)") }
        }
    } else {
        failures.append("SimulatorKit's digitizer builders did not load")
    }
    return failures
}

/// A width x height image, black but for one white pixel at the top-left.
func markerImage(width: Int, height: Int) -> CGImage? {
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                              bytesPerRow: width * 4, space: space,
                              bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                                  | CGImageAlphaInfo.premultipliedFirst.rawValue),
          let data = ctx.data else { return nil }
    memset(data, 0, width * height * 4)
    let px = data.assumingMemoryBound(to: UInt8.self)   // row 0 is the top
    px[0] = 255; px[1] = 255; px[2] = 255; px[3] = 255
    return ctx.makeImage()
}

/// Where an image's white pixel is, top-left origin.
func markerPosition(_ image: CGImage) -> (x: Int, y: Int)? {
    let w = image.width, h = image.height
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                              space: space, bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                                  | CGImageAlphaInfo.premultipliedFirst.rawValue) else { return nil }
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    guard let data = ctx.data else { return nil }
    let px = data.assumingMemoryBound(to: UInt8.self)
    for y in 0..<h { for x in 0..<w where px[(y * w + x) * 4 + 1] > 128 { return (x, y) } }
    return nil
}

/// An IOSurface of BGRA pixels all set to `value` (alpha opaque).
func filledSurface(width: Int, height: Int, value: UInt8) -> IOSurface? {
    let props: [IOSurfacePropertyKey: Any] = [
        .width: width, .height: height, .bytesPerElement: 4,
        .pixelFormat: 0x4247_5241,  // 'BGRA'
    ]
    guard let surface = IOSurface(properties: props) else { return nil }
    surface.lock(options: [], seed: nil)
    let base = surface.baseAddress.assumingMemoryBound(to: UInt8.self)
    for i in 0..<(surface.bytesPerRow * height) { base[i] = (i % 4 == 3) ? 255 : value }
    surface.unlock(options: [], seed: nil)
    return surface
}

/// Where a touch goes and how a point gets there: the size callers' points
/// are in, the digitizer, and the rotation into the panel's own coordinates.
struct TouchSpace {
    let size: CGSize
    let target: UInt32
    let panel: Panel?

    /// Interface point -> the 0...1 digitizer coordinate, clamped.
    func normalize(_ p: CGPoint) -> CGPoint {
        guard let panel else {
            return CGPoint(x: CGFloat(clamp01(Double(p.x / size.width))),
                           y: CGFloat(clamp01(Double(p.y / size.height))))
        }
        let q = panel.toPanel(p)
        return CGPoint(x: CGFloat(clamp01(Double(q.x / panel.nativeSize.width))),
                       y: CGFloat(clamp01(Double(q.y / panel.nativeSize.height))))
    }
}

func touchSpace(udid: String, device: NSObject) -> TouchSpace {
    if let panel = litPanel(udid: udid, device: device) {
        return TouchSpace(size: panel.screenSize, target: panel.target, panel: panel)
    }
    return TouchSpace(size: devicePointSize(for: device), target: touchDigitizer, panel: nil)
}

// MARK: - Accessibility Bridge

/// TokenDispatcher — bridge-token delegate for AXPTranslator.
/// Routes XPC requests to the correct SimDevice.
final class TokenDispatcher: NSObject, @unchecked Sendable {
    private let lock = NSLock()
    private var deviceForToken: [String: NSObject] = [:]
    private var deadlineForToken: [String: Date] = [:]

    func register(device: NSObject, token: String, deadline: Date) {
        lock.lock(); defer { lock.unlock() }
        deviceForToken[token] = device
        deadlineForToken[token] = deadline
    }

    func unregister(token: String) {
        lock.lock(); defer { lock.unlock() }
        deviceForToken.removeValue(forKey: token)
        deadlineForToken.removeValue(forKey: token)
    }

    private func lookup(token: String) -> (NSObject, Date)? {
        lock.lock(); defer { lock.unlock() }
        guard let dev = deviceForToken[token] else { return nil }
        return (dev, deadlineForToken[token] ?? Date.distantFuture)
    }

    @objc dynamic func accessibilityTranslationDelegateBridgeCallbackWithToken(
        _ token: NSString
    ) -> Any {
        let key = token as String
        let entry = self.lookup(token: key)
        let block: @convention(block) (AnyObject) -> AnyObject = { [weak self] request in
            guard let self else { return TokenDispatcher.emptyResponse() }
            guard let (device, deadline) = entry else {
                return TokenDispatcher.emptyResponse()
            }
            let remaining = max(0, deadline.timeIntervalSinceNow)
            if remaining <= 0 { return TokenDispatcher.emptyResponse() }
            let timeout = min(remaining, 10.0)
            return self.sendAccessibilityRequest(
                request, to: device, timeout: timeout
            ) ?? TokenDispatcher.emptyResponse()
        }
        return block
    }

    @objc dynamic func accessibilityTranslationConvertPlatformFrameToSystem(
        _ rect: CGRect, withToken token: NSString
    ) -> CGRect {
        rect
    }

    @objc dynamic func accessibilityTranslationRootParentWithToken(
        _ token: NSString
    ) -> AnyObject? {
        nil
    }

    private func sendAccessibilityRequest(
        _ request: AnyObject, to device: NSObject, timeout: Double
    ) -> AnyObject? {
        let sel = NSSelectorFromString("sendAccessibilityRequestAsync:completionQueue:completionHandler:")
        guard let imp = class_getMethodImplementation(type(of: device), sel) else {
            logErr("[ax] SimDevice.sendAccessibilityRequestAsync not found")
            return nil
        }
        typealias Fn = @convention(c) (AnyObject, Selector, AnyObject, DispatchQueue, Any) -> Void
        let send = unsafeBitCast(imp, to: Fn.self)

        let group = DispatchGroup()
        group.enter()
        let queue = DispatchQueue(label: "sim-bridge.ax.xpc")
        final class Box: @unchecked Sendable { var value: AnyObject? }
        let box = Box()
        let completion: @convention(block) (AnyObject?) -> Void = { response in
            box.value = response
            group.leave()
        }
        send(device, sel, request, queue, completion as Any)
        if group.wait(timeout: .now() + timeout) == .timedOut {
            logErr("[ax] XPC request timed out after \(timeout)s")
            return nil
        }
        return box.value
    }

    static func emptyResponse() -> AnyObject {
        if let cls = NSClassFromString("AXPTranslatorResponse") {
            let sel = NSSelectorFromString("emptyResponse")
            if let metaCls = object_getClass(cls),
               let imp = class_getMethodImplementation(metaCls, sel) {
                typealias Fn = @convention(c) (AnyClass, Selector) -> AnyObject?
                if let resp = unsafeBitCast(imp, to: Fn.self)(cls, sel) {
                    return resp
                }
            }
        }
        return NSNull()
    }
}

let sharedDispatcher = TokenDispatcher()

nonisolated(unsafe) let sharedTranslator: NSObject? = {
    // Ensure frameworks are loaded before attempting class lookup
    loadFrameworks()
    guard let cls = NSClassFromString("AXPTranslator") else {
        logErr("[ax] AXPTranslator class not found")
        return nil
    }
    let sel = NSSelectorFromString("sharedInstance")
    guard let metaCls = object_getClass(cls),
          let imp = class_getMethodImplementation(metaCls, sel) else {
        logErr("[ax] +sharedInstance not found")
        return nil
    }
    typealias Fn = @convention(c) (AnyClass, Selector) -> AnyObject?
    guard let inst = unsafeBitCast(imp, to: Fn.self)(cls, sel) as? NSObject else {
        logErr("[ax] +sharedInstance returned nil")
        return nil
    }
    inst.setValue(sharedDispatcher, forKey: "bridgeTokenDelegate")
    logErr("[ax] AXPTranslator wired with bridgeTokenDelegate")
    return inst
}()

// MARK: - AX Element Reading

func axString(_ obj: NSObject, _ key: String) -> String? {
    guard let s = obj.value(forKey: key) as? String, !s.isEmpty else { return nil }
    return s
}

func axStringOrNumber(_ obj: NSObject, _ key: String) -> String? {
    let raw = obj.value(forKey: key)
    if let s = raw as? String { return s.isEmpty ? nil : s }
    if let n = raw as? NSNumber { return n.stringValue }
    return nil
}

func axBool(_ obj: NSObject, _ key: String, default fallback: Bool) -> Bool {
    if let n = obj.value(forKey: key) as? NSNumber { return n.boolValue }
    return fallback
}

func axFrame(of element: NSObject) -> CGRect {
    let sel = NSSelectorFromString("accessibilityFrame")
    guard element.responds(to: sel),
          let imp = class_getMethodImplementation(type(of: element), sel) else {
        return .zero
    }
    typealias Fn = @convention(c) (AnyObject, Selector) -> CGRect
    return unsafeBitCast(imp, to: Fn.self)(element, sel)
}

func axChildren(of element: NSObject) -> [NSObject] {
    guard let raw = element.value(forKey: "accessibilityChildren") else { return [] }
    if let arr = raw as? [NSObject] { return arr }
    return []
}

// MARK: - AX Frame Transform

struct AXFrameTransform {
    let rootFrame: CGRect
    let pointSize: CGSize
    /// A multi-panel device's lit panel (#536). Its accessibility frames are
    /// in points, so they are never scaled; but which way they are turned
    /// depends on the app, measured on the unfolded Duo:
    ///
    /// - an app (Settings, Safari) reports its root at the panel's native
    ///   portrait size and every element in the interface's own points --
    ///   already the space callers work in;
    /// - SpringBoard's home screen reports a stale root, the *other* panel's
    ///   size (the cover's, unfolded), and its elements in the lit panel's
    ///   native portrait points, which have to be turned.
    ///
    /// So `panelSpace` is decided per read from the root, keyed on that
    /// positive signal: a root the size of another panel. Anything else --
    /// an app, or a case never measured (a letterboxed or portrait-locked
    /// app) -- is left as an app is (review). The root's frame is never used
    /// for scale.
    var panel: Panel? = nil
    var panelSpace: Bool = false

    static func panelSpace(root: CGRect, panel: Panel) -> Bool {
        panel.otherNativeSizes.contains {
            abs(root.width - $0.width) <= 1 && abs(root.height - $0.height) <= 1
        }
    }

    func map(_ macFrame: CGRect) -> CGRect {
        if let panel { return panelSpace ? panel.toScreen(macFrame) : macFrame }
        guard rootFrame.width > 0, rootFrame.height > 0,
              pointSize.width > 0, pointSize.height > 0 else { return macFrame }
        let scale = pointSize.width / rootFrame.width
        let yOffset = (pointSize.height - rootFrame.height * scale) / 2
        return CGRect(
            x: (macFrame.origin.x - rootFrame.origin.x) * scale,
            y: (macFrame.origin.y - rootFrame.origin.y) * scale + yOffset,
            width: macFrame.size.width * scale,
            height: macFrame.size.height * scale
        )
    }

    /// Invert: device point -> Mac AX point. AXPTranslator's
    /// objectAtPoint expects Mac coordinates, not device points.
    func unmap(_ devicePoint: CGPoint) -> CGPoint {
        if let panel { return panel.toPanel(devicePoint) }
        guard rootFrame.width > 0, rootFrame.height > 0,
              pointSize.width > 0, pointSize.height > 0 else { return devicePoint }
        let scale = pointSize.width / rootFrame.width
        let yOffset = (pointSize.height - rootFrame.height * scale) / 2
        return CGPoint(
            x: devicePoint.x / scale + rootFrame.origin.x,
            y: (devicePoint.y - yOffset) / scale + rootFrame.origin.y
        )
    }
}

// MARK: - AX Tree Walking

let maxDepth = 60
let xpcTimeout: Double = 5.0

/// Walk an AXPMacPlatformElement tree into idb-compatible dicts.
func walkElement(
    _ element: NSObject,
    transform: AXFrameTransform,
    depth: Int,
    deadline: Date,
    nested: Bool
) -> [String: Any] {
    let role = axString(element, "accessibilityRole") ?? "AXUnknown"
    let macFrame = axFrame(of: element)
    // The application is the interface's bounds on a lit panel: its own frame
    // is the panel's native portrait size for an app and the other panel's
    // for SpringBoard -- never the turned screen -- and quern keeps the
    // screen's size from it. Not "depth 0": a hit-test walks from the element
    // it hit.
    let projected = (role == "AXApplication" && transform.panel != nil)
        ? CGRect(origin: .zero, size: transform.panel!.screenSize)
        : transform.map(macFrame)

    // Strip "AX" prefix for the "type" field (idb convention)
    let type: String
    if role.hasPrefix("AX") {
        type = String(role.dropFirst(2))
    } else {
        type = role
    }

    let label = axString(element, "accessibilityLabel") ?? axString(element, "accessibilityTitle")

    var dict: [String: Any] = [
        "type": type,
        "role": role,
        "role_description": axString(element, "accessibilitySubrole") ?? "",
        "AXLabel": label ?? "",
        "AXValue": axStringOrNumber(element, "accessibilityValue") as Any,
        "AXUniqueId": axString(element, "accessibilityIdentifier") as Any,
        "frame": [
            "x": Double(projected.origin.x),
            "y": Double(projected.origin.y),
            "width": Double(projected.size.width),
            "height": Double(projected.size.height),
        ],
        "enabled": axBool(element, "accessibilityEnabled", default: true)
                || axBool(element, "isAccessibilityEnabled", default: false),
        "help": axString(element, "accessibilityHelp") as Any,
        "custom_actions": [] as [Any],
    ]

    if nested && depth < maxDepth && Date() < deadline {
        let kids = axChildren(of: element)
        dict["children"] = kids.map { kid in
            stampElementTranslation(token: currentToken, on: kid)
            return walkElement(kid, transform: transform, depth: depth + 1,
                             deadline: deadline, nested: true)
        }
    }

    return dict
}

/// Flatten a nested tree into a flat list of element dicts (no children key).
func flattenTree(_ dict: [String: Any]) -> [[String: Any]] {
    var result: [[String: Any]] = []
    var flat = dict
    let children = flat.removeValue(forKey: "children") as? [[String: Any]] ?? []
    result.append(flat)
    for child in children {
        result.append(contentsOf: flattenTree(child))
    }
    return result
}

/// Hit-test: find the deepest element containing the point.
func hitTest(_ dict: [String: Any], x: Double, y: Double) -> [String: Any]? {
    guard let frame = dict["frame"] as? [String: Double],
          let fx = frame["x"], let fy = frame["y"],
          let fw = frame["width"], let fh = frame["height"] else { return nil }
    guard x >= fx && x < fx + fw && y >= fy && y < fy + fh else { return nil }
    if let children = dict["children"] as? [[String: Any]] {
        for child in children {
            if let hit = hitTest(child, x: x, y: y) { return hit }
        }
    }
    return dict
}

// Thread-local token for the current AX query
nonisolated(unsafe) var currentToken = ""

func stampTranslation(token: String, on translation: NSObject) {
    translation.setValue(token, forKey: "bridgeDelegateToken")
}

func stampElementTranslation(token: String, on element: NSObject) {
    if let trans = element.value(forKey: "translation") as? NSObject {
        stampTranslation(token: token, on: trans)
    }
}

func stampSubtree(_ element: NSObject, token: String, depth: Int = 0) {
    guard depth < maxDepth else { return }
    let kids = axChildren(of: element)
    for kid in kids {
        stampElementTranslation(token: token, on: kid)
        stampSubtree(kid, token: token, depth: depth + 1)
    }
}

func frontmostApplication(translator: NSObject, token: String) -> NSObject? {
    let sel = NSSelectorFromString("frontmostApplicationWithDisplayId:bridgeDelegateToken:")
    guard translator.responds(to: sel),
          let imp = class_getMethodImplementation(type(of: translator), sel) else {
        logErr("[ax] -frontmostApplicationWithDisplayId:bridgeDelegateToken: not found")
        return nil
    }
    typealias Fn = @convention(c) (AnyObject, Selector, UInt32, AnyObject) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(translator, sel, 0, token as NSString) as? NSObject
}

func macPlatformElement(translator: NSObject, translation: NSObject) -> NSObject? {
    let sel = NSSelectorFromString("macPlatformElementFromTranslation:")
    guard translator.responds(to: sel),
          let imp = class_getMethodImplementation(type(of: translator), sel) else {
        logErr("[ax] -macPlatformElementFromTranslation: not found")
        return nil
    }
    typealias Fn = @convention(c) (AnyObject, Selector, AnyObject) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(translator, sel, translation) as? NSObject
}

/// Server-side hit-test via AXPTranslator. Returns the AXPTranslationObject
/// at the given Mac AX point, or nil if nothing is there.
///
/// idb uses this same selector
/// (`FBSimulatorAccessibilityCommands.m`:885) to drive its describe-point
/// path. The token is passed as a parameter — not via the dispatcher's
/// pre-set bridge delegate — which sidesteps the chicken/egg that the
/// upstream baguette implementation hit.
func objectAtPointOnTranslator(
    translator: NSObject, point: CGPoint, displayId: UInt32, token: String
) -> NSObject? {
    let sel = NSSelectorFromString("objectAtPoint:displayId:bridgeDelegateToken:")
    guard translator.responds(to: sel),
          let imp = class_getMethodImplementation(type(of: translator), sel) else {
        logErr("[ax] -objectAtPoint:displayId:bridgeDelegateToken: not found")
        return nil
    }
    typealias Fn = @convention(c) (
        AnyObject, Selector, CGPoint, UInt32, AnyObject
    ) -> AnyObject?
    return unsafeBitCast(imp, to: Fn.self)(
        translator, sel, point, displayId, token as NSString
    ) as? NSObject
}

func describeUI(udid: String, nested: Bool, hitX: Double? = nil, hitY: Double? = nil) -> Any? {
    guard sharedTranslator != nil else {
        logErr("[ax] translator not available")
        return nil
    }
    guard let device = resolveDevice(udid: udid) else {
        logErr("[ax] device not found: \(udid)")
        return nil
    }

    let token = UUID().uuidString
    currentToken = token
    let deadline = Date().addingTimeInterval(xpcTimeout)
    sharedDispatcher.register(device: device, token: token, deadline: deadline)
    defer {
        sharedDispatcher.unregister(token: token)
        currentToken = ""
    }

    guard let translator = sharedTranslator else { return nil }
    guard let translation = frontmostApplication(translator: translator, token: token) else {
        logErr("[ax] no frontmost application for udid=\(udid)")
        return nil
    }
    stampTranslation(token: token, on: translation)

    guard let rootElement = macPlatformElement(translator: translator, translation: translation) else {
        logErr("[ax] no mac platform element from translation")
        return nil
    }
    stampElementTranslation(token: token, on: rootElement)
    stampSubtree(rootElement, token: token)

    let pointSize = devicePointSize(for: device)
    let rootFrame = axFrame(of: rootElement)
    let panel = litPanel(udid: udid, device: device)
    let transform = AXFrameTransform(
        rootFrame: rootFrame, pointSize: pointSize, panel: panel,
        panelSpace: panel.map { AXFrameTransform.panelSpace(root: rootFrame, panel: $0) } ?? false)

    let tree = walkElement(rootElement, transform: transform, depth: 0,
                          deadline: deadline, nested: true)

    if let hx = hitX, let hy = hitY {
        return hitTest(tree, x: hx, y: hy) ?? tree
    }

    if nested {
        return tree
    } else {
        return flattenTree(tree)
    }
}

/// Server-side point probe: hit-test via AXPTranslator's objectAtPoint
/// rather than walking a static tree. Used to discover elements inside
/// containers that AXP's tree walk reports as childless (e.g. SwiftUI
/// tab bars). Coordinates are device points; converted to Mac AX coords
/// for the underlying API call, and projected back to device points
/// in the returned dict.
func probePoint(udid: String, x: Double, y: Double, nested: Bool) -> Any? {
    guard sharedTranslator != nil else {
        logErr("[ax] translator not available")
        return nil
    }
    guard let device = resolveDevice(udid: udid) else {
        logErr("[ax] device not found: \(udid)")
        return nil
    }

    let token = UUID().uuidString
    currentToken = token
    let deadline = Date().addingTimeInterval(xpcTimeout)
    sharedDispatcher.register(device: device, token: token, deadline: deadline)
    defer {
        sharedDispatcher.unregister(token: token)
        currentToken = ""
    }

    guard let translator = sharedTranslator else { return nil }

    // We need the frontmost app's root frame to invert device-point -> Mac.
    guard let frontmost = frontmostApplication(translator: translator, token: token) else {
        logErr("[ax] no frontmost application for udid=\(udid)")
        return nil
    }
    stampTranslation(token: token, on: frontmost)
    guard let frontmostRoot = macPlatformElement(translator: translator, translation: frontmost) else {
        logErr("[ax] no mac platform element for frontmost translation")
        return nil
    }
    // Stamped before its frame is read, as describeUI does: unstamped, the
    // root answered (0,0,0,0) on any probe after the first in a process, and
    // the lit-panel rule that keys on the root's size misread SpringBoard as
    // an app -- the hit-test then saw every home-screen icon elsewhere and
    // refused the tap as covered (#536). A single-panel transform skips a
    // zero root, which is why this went unnoticed.
    stampElementTranslation(token: token, on: frontmostRoot)
    let pointSize = devicePointSize(for: device)
    let rootFrame = axFrame(of: frontmostRoot)
    let panel = litPanel(udid: udid, device: device)
    let transform = AXFrameTransform(
        rootFrame: rootFrame, pointSize: pointSize, panel: panel,
        panelSpace: panel.map { AXFrameTransform.panelSpace(root: rootFrame, panel: $0) } ?? false)

    let macPoint = transform.unmap(CGPoint(x: x, y: y))

    guard let hitTranslation = objectAtPointOnTranslator(
        // The lit panel's own display: asked on display 0, the hit-test on
        // an unfolded Duo found nothing anywhere (#536).
        translator: translator, point: macPoint, displayId: transform.panel?.screenID ?? 0, token: token
    ) else {
        logErr("[ax] objectAtPoint returned nil for device=(\(x),\(y)) mac=(\(macPoint.x),\(macPoint.y))")
        return nil
    }
    stampTranslation(token: token, on: hitTranslation)
    guard let hitElement = macPlatformElement(translator: translator, translation: hitTranslation) else {
        logErr("[ax] no mac platform element for hit translation")
        return nil
    }
    stampElementTranslation(token: token, on: hitElement)
    stampSubtree(hitElement, token: token)

    let subtree = walkElement(hitElement, transform: transform, depth: 0,
                              deadline: deadline, nested: true)

    if nested {
        return subtree
    } else {
        return flattenTree(subtree)
    }
}

// MARK: - HID Input Injection

// IOHIDDigitizer types
typealias CreateDigitizerFn = @convention(c) (
    CFAllocator?, UInt64, UInt32, UInt32, UInt32, UInt32, UInt32,
    Double, Double, Double, Double, Double, Bool, Bool, UInt32
) -> Unmanaged<CFTypeRef>?

typealias CreateFingerFn = @convention(c) (
    CFAllocator?, UInt64, UInt32, UInt32, UInt32,
    Double, Double, Double, Double, Double, Bool, Bool, UInt32
) -> Unmanaged<CFTypeRef>?

typealias AppendEventFn = @convention(c) (CFTypeRef, CFTypeRef, UInt32) -> Void
typealias TrackpadWrapFn = @convention(c) (UnsafeRawPointer) -> UnsafeMutableRawPointer?
typealias ButtonFn = @convention(c) (UInt32, UInt32, UInt32) -> UnsafeMutableRawPointer?
typealias HIDArbitraryFn = @convention(c) (UInt32, UInt32, UInt32, UInt32) -> UnsafeMutableRawPointer?
typealias KeyboardArbitraryFn = @convention(c) (UInt32, UInt32) -> UnsafeMutableRawPointer?
typealias ModifierKeyBitFn = @convention(c) (UInt32, UInt32) -> UnsafeMutableRawPointer?
typealias ServiceFn = @convention(c) () -> UnsafeMutableRawPointer?

nonisolated(unsafe) var createDigitizerFn: CreateDigitizerFn?
nonisolated(unsafe) var createFingerFn: CreateFingerFn?
nonisolated(unsafe) var appendEventFn: AppendEventFn?
nonisolated(unsafe) var trackpadWrapFn: TrackpadWrapFn?
nonisolated(unsafe) var buttonFn: ButtonFn?
nonisolated(unsafe) var hidArbFn: HIDArbitraryFn?
nonisolated(unsafe) var keyboardArbFn: KeyboardArbitraryFn?
nonisolated(unsafe) var modifierBitFn: ModifierKeyBitFn?
nonisolated(unsafe) var createPointerSvc: ServiceFn?
nonisolated(unsafe) var createMouseSvc: ServiceFn?
nonisolated(unsafe) var hidSymbolsResolved = false
nonisolated(unsafe) var touchIdCounter: UInt32 = 0

let touchDigitizer: UInt32 = 0x32

func nextTouchId() -> UInt32 {
    touchIdCounter &+= 1
    if touchIdCounter == 0 { touchIdCounter = 1 }
    return touchIdCounter
}

func resolveHIDSymbols() -> Bool {
    if hidSymbolsResolved { return true }
    let dev = developerDir()
    guard let kitPath = simulatorKitPath(at: dev),
          let kit = dlopen(kitPath, RTLD_NOW) else { return false }
    let dyld = UnsafeMutableRawPointer(bitPattern: -2)
    guard let pCreateDig = dlsym(dyld, "IOHIDEventCreateDigitizerEvent"),
          let pCreateFin = dlsym(dyld, "IOHIDEventCreateDigitizerFingerEvent"),
          let pAppend    = dlsym(dyld, "IOHIDEventAppendEvent"),
          let pWrap      = dlsym(kit, "IndigoHIDMessageForTrackpadEventFromHIDEventRef")
    else { return false }
    createDigitizerFn = unsafeBitCast(pCreateDig, to: CreateDigitizerFn.self)
    createFingerFn    = unsafeBitCast(pCreateFin, to: CreateFingerFn.self)
    appendEventFn     = unsafeBitCast(pAppend, to: AppendEventFn.self)
    trackpadWrapFn    = unsafeBitCast(pWrap, to: TrackpadWrapFn.self)
    buttonFn = dlsym(kit, "IndigoHIDMessageForButton").map { unsafeBitCast($0, to: ButtonFn.self) }
    hidArbFn = dlsym(kit, "IndigoHIDMessageForHIDArbitrary").map { unsafeBitCast($0, to: HIDArbitraryFn.self) }
    keyboardArbFn = dlsym(kit, "IndigoHIDMessageForKeyboardArbitrary").map { unsafeBitCast($0, to: KeyboardArbitraryFn.self) }
    modifierBitFn = dlsym(kit, "IndigoHIDMessageForModifierKeyBit").map { unsafeBitCast($0, to: ModifierKeyBitFn.self) }
    createPointerSvc = dlsym(kit, "IndigoHIDMessageToCreatePointerService").map { unsafeBitCast($0, to: ServiceFn.self) }
    createMouseSvc = dlsym(kit, "IndigoHIDMessageToCreateMouseService").map { unsafeBitCast($0, to: ServiceFn.self) }
    hidSymbolsResolved = true
    return true
}

// Per-device HID client cache
nonisolated(unsafe) var hidClients: [String: AnyObject] = [:]

final class HIDSendResult {
    let semaphore = DispatchSemaphore(value: 0)
    var error: NSError?
}

/// Send a message and wait for delivery confirmation. Returns false when the
/// client's connection is dead (e.g. the device rebooted after the client was
/// created) or no completion arrives in time.
func sendHIDMessageChecked(_ message: UnsafeMutableRawPointer,
                           to client: AnyObject,
                           timeout: TimeInterval = 1.0) -> Bool {
    let sel = NSSelectorFromString("sendWithMessage:freeWhenDone:completionQueue:completion:")
    guard let cls = object_getClass(client),
          let imp = class_getMethodImplementation(cls, sel) else { return false }

    let result = HIDSendResult()
    let completion: @convention(block) (NSError?) -> Void = { error in
        result.error = error
        result.semaphore.signal()
    }
    typealias Fn = @convention(c) (
        AnyObject, Selector, UnsafeMutableRawPointer, ObjCBool,
        AnyObject?, (@convention(block) (NSError?) -> Void)?
    ) -> Void
    unsafeBitCast(imp, to: Fn.self)(
        client, sel, message, ObjCBool(true),
        DispatchQueue.global(qos: .userInitiated), completion
    )
    if result.semaphore.wait(timeout: .now() + timeout) == .timedOut {
        logErr("[hid] checked send timed out")
        return false
    }
    if let error = result.error {
        logErr("[hid] checked send failed: \(error)")
        return false
    }
    return true
}

/// Verify a cached client still reaches the device's current boot.
/// Re-sending a create-pointer-service message is harmless (it is already
/// sent at client init) and round-trips through the device connection.
func isHIDClientAlive(_ client: AnyObject) -> Bool {
    guard let create = createPointerSvc, let probe = create() else {
        return true // cannot probe — keep legacy behavior
    }
    return sendHIDMessageChecked(probe, to: client)
}

/// Prime the keyboard event path. backboardd creates the keyboard service
/// lazily on the first KEY event; modifier-bit messages sent before that are
/// silently discarded (observed as the first shifted character of a fresh
/// client losing its shift). A bare shift keypress creates the service
/// without producing any text. Called from the typing path only — keyboard
/// events flip the simulator into hardware-keyboard mode (hiding the soft
/// keyboard and surfacing the AutoFill bar), so taps must stay free of them.
func primeKeyboardService(_ client: AnyObject) {
    guard let keyFn = keyboardArbFn else { return }
    if let down = keyFn(0xE1, 1) {
        _ = sendHIDMessageChecked(down, to: client, timeout: 0.5)
    }
    if let up = keyFn(0xE1, 2) {
        _ = sendHIDMessageChecked(up, to: client, timeout: 0.5)
    }
    usleep(20_000)
}

func ensureHIDClient(udid: String) -> AnyObject? {
    if let existing = hidClients[udid] {
        if isHIDClientAlive(existing) {
            return existing
        }
        logErr("[hid] cached client for \(udid) is stale (device rebooted?) — recreating")
        hidClients.removeValue(forKey: udid)
    }
    guard resolveHIDSymbols() else {
        logErr("[hid] symbol resolution failed")
        return nil
    }
    guard let device = resolveDevice(udid: udid) else {
        logErr("[hid] device not found: \(udid)")
        return nil
    }
    guard let cls = NSClassFromString("_TtC12SimulatorKit24SimDeviceLegacyHIDClient") else {
        logErr("[hid] SimDeviceLegacyHIDClient class not found")
        return nil
    }
    let initSel = NSSelectorFromString("initWithDevice:error:")
    guard let imp = class_getMethodImplementation(cls, initSel) else { return nil }
    typealias InitFn = @convention(c) (AnyObject, Selector, AnyObject, AutoreleasingUnsafeMutablePointer<NSError?>) -> AnyObject?
    let initFn = unsafeBitCast(imp, to: InitFn.self)
    guard let metaCls = object_getClass(cls),
          let allocImp = class_getMethodImplementation(metaCls, NSSelectorFromString("alloc")) else { return nil }
    typealias AllocFn = @convention(c) (AnyClass, Selector) -> AnyObject?
    guard let allocated = unsafeBitCast(allocImp, to: AllocFn.self)(cls, NSSelectorFromString("alloc")) else { return nil }

    var err: NSError?
    guard let client = initFn(allocated, initSel, device, &err) else {
        if let err { logErr("[hid] SimDeviceLegacyHIDClient init failed: \(err)") }
        return nil
    }

    // Warm up services
    if let create = createPointerSvc, let msg = create() {
        sendHIDMessage(msg, to: client)
        usleep(20_000)
    }
    if let create = createMouseSvc, let msg = create() {
        sendHIDMessage(msg, to: client)
        usleep(20_000)
    }

    hidClients[udid] = client
    return client
}

func sendHIDMessage(_ message: UnsafeMutableRawPointer, to client: AnyObject) {
    let sel = NSSelectorFromString("sendWithMessage:freeWhenDone:completionQueue:completion:")
    guard let cls = object_getClass(client),
          let imp = class_getMethodImplementation(cls, sel) else { return }
    typealias Fn = @convention(c) (AnyObject, Selector, UnsafeMutableRawPointer, ObjCBool, AnyObject?, AnyObject?) -> Void
    unsafeBitCast(imp, to: Fn.self)(client, sel, message, ObjCBool(true), nil, nil)
}

// IOHIDDigitizer dispatch — Xcode 26 tap/swipe path

func makeDigitizerEvent(point: CGPoint, identifier: UInt32, isDown: Bool) -> CFTypeRef? {
    guard let createParent = createDigitizerFn,
          let createFinger = createFingerFn,
          let appendFn = appendEventFn else { return nil }

    let mask: UInt32 = isDown ? 0x07 : 0x06
    let range = isDown
    let touch = isDown
    let now = mach_absolute_time()
    let transducerFinger: UInt32 = 2

    guard let parentUM = createParent(nil, now, transducerFinger, 0, identifier, mask, 0,
                                      point.x, point.y, 0.0, 0.0, 0.0, range, touch, 0)
    else { return nil }
    let parent = parentUM.takeRetainedValue()

    guard let fingerUM = createFinger(nil, now, 0, identifier, mask,
                                      point.x, point.y, 0.0, 0.0, 0.0, range, touch, 0)
    else { return parent }
    let finger = fingerUM.takeRetainedValue()
    appendFn(parent, finger, 0)
    return parent
}

func wrapAndPatch(event: CFTypeRef, edgeBit: UInt8 = 0,
                  target: UInt32 = touchDigitizer) -> UnsafeMutableRawPointer? {
    guard let wrapFn = trackpadWrapFn else { return nil }
    let raw = Unmanaged.passUnretained(event as AnyObject).toOpaque()
    guard let msg = wrapFn(raw) else { return nil }
    msg.storeBytes(of: target, toByteOffset: 0x6c, as: UInt32.self)
    let size = malloc_size(msg)
    if size >= 0x110 {
        msg.storeBytes(of: target, toByteOffset: 0x10c, as: UInt32.self)
    }
    let edgePresent: UInt8 = edgeBit == 0 ? 0 : 0x04
    msg.storeBytes(of: edgePresent, toByteOffset: 0x3a, as: UInt8.self)
    msg.storeBytes(of: edgeBit, toByteOffset: 0x3b, as: UInt8.self)
    if size >= 0xdc {
        msg.storeBytes(of: edgePresent, toByteOffset: 0xda, as: UInt8.self)
        msg.storeBytes(of: edgeBit, toByteOffset: 0xdb, as: UInt8.self)
    }
    return msg
}

func sendDigitizerEvent(point: CGPoint, identifier: UInt32, isDown: Bool,
                        edgeBit: UInt8 = 0, target: UInt32 = touchDigitizer,
                        client: AnyObject) -> Bool {
    guard let event = makeDigitizerEvent(point: point, identifier: identifier, isDown: isDown) else {
        return false
    }
    let msg: UnsafeMutableRawPointer? = withExtendedLifetime(event) {
        wrapAndPatch(event: event, edgeBit: edgeBit, target: target)
    }
    guard let msg else { return false }
    sendHIDMessage(msg, to: client)
    return true
}

func clamp01(_ v: Double) -> Double { v < 0 ? 0 : (v > 1 ? 1 : v) }

func doTap(udid: String, x: Double, y: Double, hold: Double = 0.05) -> Bool {
    guard let client = ensureHIDClient(udid: udid), let device = resolveDevice(udid: udid) else { return false }
    let space = touchSpace(udid: udid, device: device)
    let point = space.normalize(CGPoint(x: x, y: y))
    let id = nextTouchId()
    guard sendDigitizerEvent(point: point, identifier: id, isDown: true, target: space.target,
                             client: client) else { return false }
    let holdUs = UInt32(max(0.02, hold) * 1_000_000)
    usleep(holdUs)
    return sendDigitizerEvent(point: point, identifier: id, isDown: false, target: space.target,
                              client: client)
}

/// Indigo's screen-edge bits, as the guest reads them at 0x3b/0xdb (#251).
/// The values baguette derived by sweeping IndigoHIDMessageForMouseNSEvent's
/// edge argument and diffing the bytes; quern's offsets were reached
/// independently and match.
let edgeBits: [String: UInt8] = ["left": 0x02, "right": 0x04, "top": 0x08, "bottom": 0x01]

/// `hold` keeps the finger down at the end point for that many seconds before
/// lifting. With no hold a 0.3s swipe releases at speed and the list keeps
/// going: measured on iOS 18.6 it travelled 1020pt for a 389pt drag, which is
/// more than a screen, so a sweep stepping with it skips rows outright. A 0.15s
/// hold brings that down to the drag itself (350pt) and the list stops at once.
///
/// The hold events alternate by half a thousandth of the screen. Repeating the
/// identical point was tried first and changed nothing -- the travel was the
/// same 1000pt -- so the unchanged position is evidently not treated as the
/// finger coming to rest.
///
/// `edge` marks every event of the swipe -- down, each move, the hold and the
/// lift -- as starting from that screen edge, which is what routes it to the
/// system's edge gestures (back, home, Control Centre) instead of the app. A
/// gesture that announced its edge only on touch-down would read as an
/// ordinary drag from its second sample on.
func doSwipe(udid: String, x1: Double, y1: Double, x2: Double, y2: Double, duration: Double = 0.3,
             hold: Double = 0, edge: UInt8 = 0) -> Bool {
    guard let client = ensureHIDClient(udid: udid), let device = resolveDevice(udid: udid) else { return false }
    let space = touchSpace(udid: udid, device: device)
    let target = space.target
    let start = space.normalize(CGPoint(x: x1, y: y1))
    let end = space.normalize(CGPoint(x: x2, y: y2))
    let steps = 10
    let stepMs = UInt32(max(8, (duration * 1000) / Double(steps + 2)))
    let id = nextTouchId()

    guard sendDigitizerEvent(point: start, identifier: id, isDown: true, edgeBit: edge, target: target,
                             client: client) else { return false }
    var ok = 0
    for i in 1...steps {
        usleep(stepMs * 1000)
        let t = Double(i) / Double(steps)
        let p = CGPoint(x: start.x + (end.x - start.x) * CGFloat(t),
                        y: start.y + (end.y - start.y) * CGFloat(t))
        // Move events: use isDown=true for sustained touch (mask 0x07)
        if sendDigitizerEvent(point: p, identifier: id, isDown: true, edgeBit: edge, target: target,
                              client: client) { ok += 1 }
    }
    usleep(stepMs * 1000)
    if hold > 0 {
        let holdSteps = max(1, Int(hold * 1000 / 16))
        for k in 0..<holdSteps {
            let p = CGPoint(x: end.x, y: end.y + CGFloat(k % 2) * 0.0005)
            _ = sendDigitizerEvent(point: p, identifier: id, isDown: true, edgeBit: edge, target: target,
                                   client: client)
            usleep(16_000)
        }
    }
    return sendDigitizerEvent(point: end, identifier: id, isDown: false, edgeBit: edge, target: target,
                              client: client) && ok >= steps / 2
}

func doButton(udid: String, name: String) -> Bool {
    guard let client = ensureHIDClient(udid: udid) else { return false }
    let holdUs: UInt32 = 100_000

    // Normalize: lowercase, drop underscores/hyphens so "SIDE_BUTTON",
    // "side-button", "sideButton" all resolve. Matches idb's name set
    // (HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY) plus our existing names.
    let n = name.lowercased()
        .replacingOccurrences(of: "_", with: "")
        .replacingOccurrences(of: "-", with: "")

    switch n {
    case "home":
        return pressIndigoButton(code: 0x0, holdUs: holdUs, client: client)
    case "lock", "sidebutton":
        return pressIndigoButton(code: 0x1, holdUs: holdUs, client: client)
    case "siri":
        // Long-press side button — the Siri gesture on Face ID iPhones.
        // Simulators generally don't run Siri, but the event matches idb's behavior.
        return pressIndigoButton(code: 0x1, holdUs: 1_500_000, client: client)
    case "applepay":
        // Double-press side/lock button.
        guard pressIndigoButton(code: 0x1, holdUs: 80_000, client: client) else { return false }
        usleep(120_000)
        return pressIndigoButton(code: 0x1, holdUs: 80_000, client: client)
    case "volumeup":
        return pressArbitraryHID(page: 12, usage: 233, holdUs: holdUs, client: client)
    case "volumedown":
        return pressArbitraryHID(page: 12, usage: 234, holdUs: holdUs, client: client)

    default:
        logErr("[hid] unknown button: \(name)")
        return false
    }
}

func pressIndigoButton(code: UInt32, holdUs: UInt32, client: AnyObject) -> Bool {
    guard let bfn = buttonFn else { return false }
    guard let down = bfn(code, 1, 0x33) else { return false }
    sendHIDMessage(down, to: client)
    usleep(holdUs)
    guard let up = bfn(code, 2, 0x33) else { return false }
    sendHIDMessage(up, to: client)
    return true
}

func pressArbitraryHID(page: UInt32, usage: UInt32, holdUs: UInt32, client: AnyObject) -> Bool {
    guard let kfn = hidArbFn else { return false }
    guard let down = kfn(touchDigitizer, page, usage, 1) else { return false }
    sendHIDMessage(down, to: client)
    usleep(holdUs)
    guard let up = kfn(touchDigitizer, page, usage, 2) else { return false }
    sendHIDMessage(up, to: client)
    return true
}

// Hardware keyboard toggle

/// Attach or detach the simulated hardware keyboard. Same CoreSimulator call
/// Simulator.app's "Connect Hardware Keyboard" (shift-cmd-K) toggle uses.
/// With the hardware keyboard enabled the software keyboard stays hidden;
/// disabling it restores the software keyboard for focused text fields.
func setHardwareKeyboard(udid: String, enabled: Bool) -> Bool {
    guard let device = resolveDevice(udid: udid) else {
        logErr("[kb] device not found: \(udid)")
        return false
    }
    let sel = NSSelectorFromString("setHardwareKeyboardEnabled:keyboardType:error:")
    guard device.responds(to: sel),
          let cls = object_getClass(device),
          let imp = class_getMethodImplementation(cls, sel) else {
        logErr("[kb] SimDevice does not respond to setHardwareKeyboardEnabled:keyboardType:error:")
        return false
    }
    typealias Fn = @convention(c) (
        AnyObject, Selector, ObjCBool, UInt32,
        AutoreleasingUnsafeMutablePointer<NSError?>
    ) -> ObjCBool
    var err: NSError?
    let ok = unsafeBitCast(imp, to: Fn.self)(device, sel, ObjCBool(enabled), 0, &err)
    if ok.boolValue == false, let err {
        logErr("[kb] setHardwareKeyboardEnabled failed: \(err)")
    }
    return ok.boolValue
}

// Text typing — decompose ASCII to HID keycodes

/// NSEvent modifier-flag bit index for a HID modifier usage (page 7).
/// IndigoHIDMessageForModifierKeyBit accepts bits 16-20:
/// 16 = caps lock, 17 = shift, 18 = control, 19 = option, 20 = command.
func modifierBit(forUsage usage: UInt32) -> UInt32? {
    switch usage {
    case 0xE1, 0xE5: return 17 // left/right shift
    case 0xE0, 0xE4: return 18 // left/right control
    case 0xE2, 0xE6: return 19 // left/right option
    case 0xE3, 0xE7: return 20 // left/right command
    case 0x39:       return 16 // caps lock
    default:         return nil
    }
}

func doTypeText(udid: String, text: String) -> Bool {
    guard let client = ensureHIDClient(udid: udid) else { return false }

    // backboardd only honors modifier state while the simulated hardware
    // keyboard is attached; with it detached (the default on a fresh boot),
    // shifted characters silently lose their modifier. Attach before typing —
    // the same call Simulator.app makes when its window takes focus. Side
    // effect: the software keyboard hides; callers that need it visible
    // afterward can detach via the set-hardware-keyboard command.
    _ = setHardwareKeyboard(udid: udid, enabled: true)

    // Preferred path: dedicated keyboard messages (same class Simulator.app's own
    // keyboard passthrough emits). The generic HIDArbitrary path drops modifier
    // state on freshly-booted or heavily-loaded simulators — keys land unshifted.
    if keyboardArbFn != nil && modifierBitFn != nil {
        return typeTextViaKeyboardMessages(text: text, client: client)
    }
    return typeTextViaArbitraryHID(text: text, client: client)
}

func typeTextViaKeyboardMessages(text: String, client: AnyObject) -> Bool {
    guard let keyFn = keyboardArbFn, let modFn = modifierBitFn else { return false }

    primeKeyboardService(client)

    for c in text {
        guard let (keyUsage, modifiers) = decomposeCharacter(c) else {
            logErr("[hid] unsupported character: \(c)")
            continue
        }
        let modifierBits = modifiers.compactMap { modifierBit(forUsage: $0) }

        // Modifier-down (stateful modifier bit, survives event batching)
        for bit in modifierBits {
            guard let down = modFn(bit, 1) else { continue }
            sendHIDMessage(down, to: client)
        }
        if !modifierBits.isEmpty {
            usleep(10_000)
        }

        // Key down
        guard let keyDown = keyFn(keyUsage, 1) else { continue }
        sendHIDMessage(keyDown, to: client)
        usleep(20_000)

        // Key up
        guard let keyUp = keyFn(keyUsage, 2) else { continue }
        sendHIDMessage(keyUp, to: client)

        // Modifier-up (reverse order)
        for bit in modifierBits.reversed() {
            guard let up = modFn(bit, 0) else { continue }
            sendHIDMessage(up, to: client)
        }

        usleep(30_000) // 30ms between characters
    }
    return true
}

func typeTextViaArbitraryHID(text: String, client: AnyObject) -> Bool {
    guard let kfn = hidArbFn else {
        logErr("[hid] IndigoHIDMessageForHIDArbitrary unresolved")
        return false
    }

    for c in text {
        guard let (keyUsage, modifiers) = decomposeCharacter(c) else {
            logErr("[hid] unsupported character: \(c)")
            continue
        }

        // Modifier-down
        for mod in modifiers {
            guard let down = kfn(touchDigitizer, 7, mod, 1) else { continue }
            sendHIDMessage(down, to: client)
        }

        // Key down
        guard let keyDown = kfn(touchDigitizer, 7, keyUsage, 1) else { continue }
        sendHIDMessage(keyDown, to: client)
        usleep(20_000)

        // Key up
        guard let keyUp = kfn(touchDigitizer, 7, keyUsage, 2) else { continue }
        sendHIDMessage(keyUp, to: client)

        // Modifier-up (reverse order)
        for mod in modifiers.reversed() {
            guard let up = kfn(touchDigitizer, 7, mod, 2) else { continue }
            sendHIDMessage(up, to: client)
        }

        usleep(30_000) // 30ms between characters
    }
    return true
}

/// Decompose an ASCII character into (HID usage on page 7, [modifier usages]).
/// Returns nil for unsupported characters.
func decomposeCharacter(_ c: Character) -> (UInt32, [UInt32])? {
    guard let scalar = c.unicodeScalars.first,
          c.unicodeScalars.count == 1,
          scalar.isASCII else { return nil }
    let v = Int(scalar.value)

    let shiftUsage: UInt32 = 0xE1

    // Lowercase letters
    if v >= 0x61 && v <= 0x7A {
        return (UInt32(0x04 + v - 0x61), [])
    }
    // Uppercase letters
    if v >= 0x41 && v <= 0x5A {
        return (UInt32(0x04 + v - 0x41), [shiftUsage])
    }
    // Digits 1-9
    if v >= 0x31 && v <= 0x39 {
        return (UInt32(0x1E + v - 0x31), [])
    }
    // Digit 0
    if v == 0x30 { return (0x27, []) }

    // Punctuation map: char -> (usage, shifted?)
    let punctuation: [Int: (UInt32, Bool)] = [
        0x20: (0x2C, false), // space
        0x2D: (0x2D, false), // -
        0x5F: (0x2D, true),  // _
        0x3D: (0x2E, false), // =
        0x2B: (0x2E, true),  // +
        0x5B: (0x2F, false), // [
        0x7B: (0x2F, true),  // {
        0x5D: (0x30, false), // ]
        0x7D: (0x30, true),  // }
        0x5C: (0x31, false), // backslash
        0x7C: (0x31, true),  // |
        0x3B: (0x33, false), // ;
        0x3A: (0x33, true),  // :
        0x27: (0x34, false), // '
        0x22: (0x34, true),  // "
        0x60: (0x35, false), // `
        0x7E: (0x35, true),  // ~
        0x2C: (0x36, false), // ,
        0x3C: (0x36, true),  // <
        0x2E: (0x37, false), // .
        0x3E: (0x37, true),  // >
        0x2F: (0x38, false), // /
        0x3F: (0x38, true),  // ?
        0x21: (0x1E, true),  // !
        0x40: (0x1F, true),  // @
        0x23: (0x20, true),  // #
        0x24: (0x21, true),  // $
        0x25: (0x22, true),  // %
        0x5E: (0x23, true),  // ^
        0x26: (0x24, true),  // &
        0x2A: (0x25, true),  // *
        0x28: (0x26, true),  // (
        0x29: (0x27, true),  // )
        0x09: (0x2B, false), // tab
        0x0A: (0x28, false), // newline -> enter
        0x0D: (0x28, false), // carriage return -> enter
        0x08: (0x2A, false), // backspace -> delete
        0x7F: (0x2A, false), // DEL -> delete
    ]

    if let (usage, shifted) = punctuation[v] {
        return (usage, shifted ? [shiftUsage] : [])
    }
    return nil
}

// MARK: - Screenshot via IOSurface

func doScreenshot(udid: String, quality: Double = 0.8, scale: Int = 1) -> (Data, Int, Int)? {
    guard let device = resolveDevice(udid: udid) else {
        logErr("[screenshot] device not found: \(udid)")
        return nil
    }

    // Get IO client
    guard let ioObj = device.perform(NSSelectorFromString("io"))?.takeUnretainedValue() as? NSObject else {
        logErr("[screenshot] io unavailable")
        return nil
    }

    // Update ports
    ioObj.perform(NSSelectorFromString("updateIOPorts"))

    guard let ports = ioObj.value(forKey: "deviceIOPorts") as? [NSObject] else {
        logErr("[screenshot] no IO ports")
        return nil
    }

    let pidSel = NSSelectorFromString("portIdentifier")
    let descSel = NSSelectorFromString("descriptor")
    let surfSel = NSSelectorFromString("framebufferSurface")

    // Find framebuffer descriptor
    var descriptor: NSObject?
    for port in ports where port.responds(to: pidSel) {
        guard let pid = port.perform(pidSel)?.takeUnretainedValue(),
              "\(pid)" == "com.apple.framebuffer.display",
              port.responds(to: descSel),
              let desc = port.perform(descSel)?.takeUnretainedValue() as? NSObject,
              desc.responds(to: surfSel) else { continue }
        descriptor = desc
        break
    }

    // A multi-panel device's lit panel, not whichever display came first:
    // unfolded, the first was the Duo's dark cover (#536).
    var panel: Panel? = nil
    if let (lit, litDesc) = litPanelAndDescriptor(udid: udid, device: device) {
        descriptor = litDesc
        panel = lit
    }

    guard let desc = descriptor else {
        logErr("[screenshot] no framebuffer descriptor")
        return nil
    }

    // Get the current surface directly
    guard let surfObj = desc.perform(surfSel)?.takeUnretainedValue() else {
        logErr("[screenshot] no framebuffer surface")
        return nil
    }
    let surface = unsafeBitCast(surfObj, to: IOSurface.self)
    let width = IOSurfaceGetWidth(surface)
    let height = IOSurfaceGetHeight(surface)

    // Lock and create CGImage
    IOSurfaceLock(surface, .readOnly, nil)
    defer { IOSurfaceUnlock(surface, .readOnly, nil) }

    let bpr = IOSurfaceGetBytesPerRow(surface)
    let base = IOSurfaceGetBaseAddress(surface)

    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
    guard let ctx = CGContext(
        data: base,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: bpr,
        space: colorSpace,
        bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.premultipliedFirst.rawValue
    ) else { return nil }

    guard let framebufferImage = ctx.makeImage() else { return nil }
    // Upright, as the interface is turned: the framebuffer is the panel's own
    // portrait layout, and `simctl io screenshot` rotates the same way.
    guard let cgImage = panel.map({ rotated(framebufferImage, orientation: $0.orientation) })
            ?? framebufferImage else { return nil }

    // Encode JPEG
    let data = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(data, "public.jpeg" as CFString, 1, nil) else { return nil }
    let options: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: quality]
    CGImageDestinationAddImage(dest, cgImage, options as CFDictionary)
    guard CGImageDestinationFinalize(dest) else { return nil }

    return (data as Data, cgImage.width, cgImage.height)
}

/// A framebuffer image turned to the interface's orientation (UIDeviceOrientation).
/// The same rotation `Panel.toScreen` applies to frames, so a frame read from
/// the tree lands on the matching pixels of this image.
func rotated(_ image: CGImage, orientation: UInt32) -> CGImage? {
    guard orientation != 1, orientation >= 2, orientation <= 4 else { return image }
    let w = image.width, h = image.height
    let landscape = orientation != 2
    let outW = landscape ? h : w, outH = landscape ? w : h
    guard let space = image.colorSpace ?? CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: outW, height: outH, bitsPerComponent: 8,
                              bytesPerRow: 0, space: space,
                              bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                                  | CGBitmapInfo.byteOrder32Little.rawValue) else { return nil }
    // CoreGraphics' origin is bottom-left; these are the panel-to-screen
    // rotations of `Panel.toScreen` expressed in it.
    switch orientation {
    case 2:
        ctx.translateBy(x: CGFloat(outW), y: CGFloat(outH))
        ctx.rotate(by: .pi)
    case 3:
        ctx.translateBy(x: 0, y: CGFloat(outH))
        ctx.rotate(by: -.pi / 2)
    default: // 4
        ctx.translateBy(x: CGFloat(outW), y: 0)
        ctx.rotate(by: .pi / 2)
    }
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    return ctx.makeImage()
}


// MARK: - Multi-finger input (#252)
//
// Pinch, rotate and two-finger pan are one primitive: two or more contacts,
// each moving along its own path. Built the same way as a single touch -- an
// IOHIDEvent digitizer parent, now with one finger child per contact, wrapped
// by IndigoHIDMessageForTrackpadEventFromHIDEventRef -- so it needs no symbol
// a tap does not already use. Measured against a pinch, rotation and pan
// recogniser on iOS 18.6 (the probe app's Gestures tab): a spread from 40pt to
// 240pt reads as scale 5.0, its reverse as 0.17, a 90 degree turn as 83, and a
// 100pt two-finger drag as 90 -- the shortfalls are UIKit starting to measure
// only once the fingers pass its threshold.
//
// baguette builds its two-finger gestures from IndigoHIDMessageForMouseNSEvent
// instead; that path also pinches here, but needs a retry loop for a builder
// that returns nil for ~50ms after a two-finger down. This one has no such
// window.
//
// Not IndigoHIDMessageForScrollEvent: sent with the digitizer target it makes
// backboardd assert in -[SimHIDVirtualServiceManager serviceForIndigoHIDData:]
// and abort, every time (3 of 3 on iOS 18.6), which kills every app on the
// simulator and drops its accessibility connection until the bridge is reset.

func makeMultiFingerEvent(points: [CGPoint], identifiers: [UInt32], isDown: Bool) -> CFTypeRef? {
    guard let createParent = createDigitizerFn,
          let createFinger = createFingerFn,
          let appendFn = appendEventFn,
          !points.isEmpty, points.count == identifiers.count else { return nil }
    let mask: UInt32 = isDown ? 0x07 : 0x06
    let now = mach_absolute_time()
    let transducerFinger: UInt32 = 2
    // The parent sits at the contacts' centroid; each child carries its own.
    let cx = points.map(\.x).reduce(0, +) / CGFloat(points.count)
    let cy = points.map(\.y).reduce(0, +) / CGFloat(points.count)
    guard let parentUM = createParent(nil, now, transducerFinger, 0, identifiers[0], mask, 0,
                                      cx, cy, 0.0, 0.0, 0.0, isDown, isDown, 0)
    else { return nil }
    let parent = parentUM.takeRetainedValue()
    for (index, point) in points.enumerated() {
        guard let fingerUM = createFinger(nil, now, UInt32(index), identifiers[index], mask,
                                          point.x, point.y, 0.0, 0.0, 0.0, isDown, isDown, 0)
        else { return nil }
        appendFn(parent, fingerUM.takeRetainedValue(), 0)
    }
    return parent
}

func sendMultiFingerEvent(points: [CGPoint], identifiers: [UInt32], isDown: Bool,
                          target: UInt32 = touchDigitizer, client: AnyObject) -> Bool {
    guard let event = makeMultiFingerEvent(points: points, identifiers: identifiers, isDown: isDown)
    else { return false }
    let msg: UnsafeMutableRawPointer? = withExtendedLifetime(event) {
        wrapAndPatch(event: event, target: target)
    }
    guard let msg else { return false }
    sendHIDMessage(msg, to: client)
    return true
}

/// Last resort after a lift has failed twice: lift each contact on its own
/// through the single-touch builder -- a different path from the one that
/// just failed -- and drop the cached client, so the next command starts from
/// a fresh connection rather than one with contacts it never released.
///
/// Sent checked, waiting for the simulator to confirm delivery, so `true`
/// means every lift reached it rather than only that it was built: the
/// unchecked send dispatches with no completion, and reporting a recovery on
/// that would claim more than was known (review).
func releaseContacts(points: [CGPoint], identifiers: [UInt32], udid: String,
                     target: UInt32 = touchDigitizer, client: AnyObject) -> Bool {
    var delivered = true
    for (point, id) in zip(points, identifiers) {
        guard let event = makeDigitizerEvent(point: point, identifier: id, isDown: false),
              let msg = withExtendedLifetime(event, { wrapAndPatch(event: event, target: target) }),
              sendHIDMessageChecked(msg, to: client) else {
            delivered = false
            continue
        }
    }
    hidClients.removeValue(forKey: udid)
    return delivered
}

/// Every contact down at the first point of its path, through each waypoint
/// in step, and up at the last. `paths` are in points, one per finger, all the
/// same length (at least two); the caller computes the geometry, so a pinch
/// and a rotation differ only in the waypoints they hand over.
/// Returns (ok, error, badRequest): `badRequest` marks a refusal of what was
/// asked -- malformed paths, a point off the screen -- as against a failure.
func doTouchPaths(udid: String, paths: [[CGPoint]], duration: Double) -> (Bool, String?, Bool) {
    guard paths.count >= 2, let steps = paths.first?.count, steps >= 2,
          paths.allSatisfy({ $0.count == steps }) else {
        return (false, "touch-paths needs two or more paths of the same length, at least two points each", true)
    }
    guard let client = ensureHIDClient(udid: udid), let device = resolveDevice(udid: udid) else {
        return (false, "no HID client for \(udid)", false)
    }
    let space = touchSpace(udid: udid, device: device)
    let size = space.size, target = space.target
    // Refused, not clamped: a finger pinned to the edge turns the scale or
    // angle that was asked for into a different one, and says nothing.
    for path in paths {
        for p in path where p.x < 0 || p.y < 0 || p.x > size.width || p.y > size.height {
            return (false, "point (\(Int(p.x)), \(Int(p.y))) is off the \(Int(size.width))x\(Int(size.height)) screen", true)
        }
    }
    let normalised = paths.map { path in path.map { space.normalize($0) } }
    let ids = paths.map { _ in nextTouchId() }
    let stepUs = UInt32(max(0.008, duration / Double(steps - 1)) * 1_000_000)
    func frame(_ i: Int) -> [CGPoint] { normalised.map { $0[i] } }

    guard sendMultiFingerEvent(points: frame(0), identifiers: ids, isDown: true, target: target,
                               client: client) else {
        return (false, "could not build the touch-down event", false)
    }
    var failed = 0
    for i in 1..<steps {
        usleep(stepUs)
        if !sendMultiFingerEvent(points: frame(i), identifiers: ids, isDown: true, target: target,
                                 client: client) {
            failed += 1
        }
    }
    usleep(16_000)
    // Lifted even after a failed move: fingers left down would hold every
    // later touch on this simulator hostage.
    var lifted = sendMultiFingerEvent(points: frame(steps - 1), identifiers: ids, isDown: false,
                                      target: target, client: client)
    if !lifted {
        // Once more: the same builders just made every other event, and a
        // contact left down holds the simulator's touch state hostage.
        usleep(16_000)
        lifted = sendMultiFingerEvent(points: frame(steps - 1), identifiers: ids, isDown: false,
                                      target: target, client: client)
    }
    if !lifted {
        let released = releaseContacts(points: frame(steps - 1), identifiers: ids, udid: udid,
                                       target: target, client: client)
        return (false, "could not build the touch-up event; " + (released
            ? "a separate lift for each finger was delivered instead"
            : "contacts may still be down until the simulator is next touched"), false)
    }
    // Any dropped waypoint fails the gesture: a rotation missing part of its
    // arc is not the rotation asked for, and the response would report the
    // planned destinations as reached (review). `doSwipe` tolerates half,
    // where a missing step only coarsens a straight line.
    if failed > 0 { return (false, "\(failed) of \(steps - 1) moves could not be built", false) }
    return (true, nil, false)
}

/// `count` taps of `points.count` fingers at once, `interval` seconds apart.
/// One call rather than several `tap`s, because a double tap has to land
/// inside the system's double-tap interval and a round trip per tap can miss
/// it: two taps 0.08s apart read as one double tap, 0.6s apart as two singles.
func doMultiTap(udid: String, points: [CGPoint], count: Int, interval: Double,
                hold: Double = 0.04) -> (Bool, String?, Bool) {
    guard !points.isEmpty, count >= 1 else { return (false, "multi-tap needs a point and a count", true) }
    guard let client = ensureHIDClient(udid: udid), let device = resolveDevice(udid: udid) else {
        return (false, "no HID client for \(udid)", false)
    }
    let space = touchSpace(udid: udid, device: device)
    let size = space.size, target = space.target
    for p in points where p.x < 0 || p.y < 0 || p.x > size.width || p.y > size.height {
        return (false, "point (\(Int(p.x)), \(Int(p.y))) is off the \(Int(size.width))x\(Int(size.height)) screen", true)
    }
    let normalised = points.map { space.normalize($0) }
    for n in 0..<count {
        let ids = points.map { _ in nextTouchId() }
        let down: Bool
        if normalised.count == 1 {
            down = sendDigitizerEvent(point: normalised[0], identifier: ids[0], isDown: true,
                                      target: target, client: client)
        } else {
            down = sendMultiFingerEvent(points: normalised, identifiers: ids, isDown: true,
                                        target: target, client: client)
        }
        guard down else { return (false, "could not build tap \(n + 1)", false) }
        usleep(UInt32(max(0.02, hold) * 1_000_000))
        func lift() -> Bool {
            normalised.count == 1
                ? sendDigitizerEvent(point: normalised[0], identifier: ids[0], isDown: false,
                                     target: target, client: client)
                : sendMultiFingerEvent(points: normalised, identifiers: ids, isDown: false,
                                       target: target, client: client)
        }
        // Retried once, as in doTouchPaths: a finger left down is worse than a
        // late one.
        let up = lift() || { usleep(16_000); return lift() }()
        guard up else {
            let released = releaseContacts(points: normalised, identifiers: ids, udid: udid,
                                           target: target, client: client)
            return (false, "could not lift tap \(n + 1); " + (released
                ? "a separate lift for each finger was delivered instead"
                : "contacts may still be down until the simulator is next touched"), false)
        }
        if n < count - 1 { usleep(UInt32(max(0.0, interval) * 1_000_000)) }
    }
    return (true, nil, false)
}

/// A failed reply; `badRequest` tells the server it was the request that was
/// wrong, so the caller gets a 400 rather than a bridge failure.
func failure(_ error: String, badRequest: Bool) -> [String: Any] {
    badRequest ? ["ok": false, "error": error, "code": "bad_request"] : ["ok": false, "error": error]
}

func points(from raw: Any?) -> [CGPoint]? {
    guard let pairs = raw as? [[Double]], pairs.allSatisfy({ $0.count == 2 }) else { return nil }
    return pairs.map { CGPoint(x: $0[0], y: $0[1]) }
}

// MARK: - JSON Lines Protocol

func respond(_ dict: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []) else {
        let fallback = #"{"ok":false,"error":"JSON serialization failed"}"#
        print(fallback)
        return
    }
    print(String(data: data, encoding: .utf8)!)
}

func handleCommand(_ dict: [String: Any]) {
    guard let cmd = dict["cmd"] as? String else {
        respond(["ok": false, "error": "missing 'cmd' field"])
        return
    }

    switch cmd {
    case "list":
        respond(["ok": true, "devices": listDevices()])

    case "probe-point":
        guard let udid = dict["udid"] as? String,
              let x = dict["x"] as? Double,
              let y = dict["y"] as? Double else {
            respond(["ok": false, "error": "missing 'udid', 'x', or 'y'"])
            return
        }
        let nested = dict["nested"] as? Bool ?? false
        if let result = probePoint(udid: udid, x: x, y: y, nested: nested) {
            respond(["ok": true, "tree": result])
        } else {
            respond(["ok": false, "error": "probe-point returned nil"])
        }

    case "describe-ui":
        guard let udid = dict["udid"] as? String else {
            respond(["ok": false, "error": "missing 'udid'"])
            return
        }
        let nested = dict["nested"] as? Bool ?? false
        let hitX = dict["x"] as? Double
        let hitY = dict["y"] as? Double

        if let result = describeUI(udid: udid, nested: nested, hitX: hitX, hitY: hitY) {
            if hitX != nil && hitY != nil {
                respond(["ok": true, "element": result])
            } else {
                respond(["ok": true, "tree": result])
            }
        } else {
            respond(["ok": false, "error": "Failed to query accessibility tree. Is the simulator booted with a frontmost app?"])
        }

    case "tap":
        guard let udid = dict["udid"] as? String,
              let x = dict["x"] as? Double,
              let y = dict["y"] as? Double else {
            respond(["ok": false, "error": "missing 'udid', 'x', or 'y'"])
            return
        }
        let hold = dict["hold"] as? Double ?? 0.05
        if doTap(udid: udid, x: x, y: y, hold: hold) {
            respond(["ok": true])
        } else {
            respond(["ok": false, "error": "tap failed"])
        }

    case "swipe":
        guard let udid = dict["udid"] as? String,
              let x1 = dict["x1"] as? Double,
              let y1 = dict["y1"] as? Double,
              let x2 = dict["x2"] as? Double,
              let y2 = dict["y2"] as? Double else {
            respond(["ok": false, "error": "missing swipe parameters"])
            return
        }
        let duration = dict["duration"] as? Double ?? 0.3
        let hold = dict["hold"] as? Double ?? 0
        var edge: UInt8 = 0
        if let name = dict["edge"] as? String {
            guard let bit = edgeBits[name] else {
                respond(failure("edge must be one of left, right, top, bottom, not \(name)",
                                badRequest: true))
                return
            }
            edge = bit
            // Only a swipe that starts at its edge: a real device decides from
            // the start position, so `edge` from mid-screen would be a system
            // gesture here and an ordinary swipe on a phone (#251). The margin
            // is EDGE_MARGIN in server/device/gestures.py.
            // Refused rather than skipped when the device cannot be found: an
            // unchecked edge swipe is the one this check exists to stop (review).
            guard let device = resolveDevice(udid: udid) else {
                respond(["ok": false, "error": "device not found: \(udid)"])
                return
            }
            do {
                let space = touchSpace(udid: udid, device: device)
                // Refused, not sent: no edge gesture reached an iPhone Duo in
                // any state, folded or not, by the old path or this one
                // (#536). Answering ok for a gesture that never happens is
                // the failure this bridge keeps being fixed for.
                if space.panel != nil {
                    respond(failure("edge swipes do not reach a device with several panels (iPhone Duo): none did, folded or unfolded. Navigate with taps, or press home",
                                    badRequest: true))
                    return
                }
                let size = space.size
                let w = Double(size.width), h = Double(size.height), m = 0.03
                let atEdge: Bool
                switch name {
                case "left": atEdge = x1 <= w * m
                case "right": atEdge = x1 >= w * (1 - m)
                case "top": atEdge = y1 <= h * m
                default: atEdge = y1 >= h * (1 - m)
                }
                if !atEdge {
                    let span = (name == "left" || name == "right") ? w : h
                    respond(failure("an edge swipe from the \(name) must start within \(Int((span * m).rounded())) of that edge of the \(Int(w))x\(Int(h)) screen; (\(Int(x1)), \(Int(y1))) does not",
                                    badRequest: true))
                    return
                }
            }
        }
        if doSwipe(udid: udid, x1: x1, y1: y1, x2: x2, y2: y2, duration: duration, hold: hold,
                   edge: edge) {
            respond(["ok": true])
        } else {
            respond(["ok": false, "error": "swipe failed"])
        }

    case "touch-paths":
        guard let udid = dict["udid"] as? String,
              let raw = dict["paths"] as? [Any] else {
            respond(["ok": false, "error": "missing 'udid' or 'paths'"])
            return
        }
        let paths = raw.compactMap { points(from: $0) }
        guard paths.count == raw.count else {
            respond(failure("each path must be a list of [x, y] pairs", badRequest: true))
            return
        }
        let (ok, error, bad) = doTouchPaths(udid: udid, paths: paths,
                                            duration: dict["duration"] as? Double ?? 0.6)
        respond(ok ? ["ok": true] : failure(error ?? "touch-paths failed", badRequest: bad))

    case "multi-tap":
        guard let udid = dict["udid"] as? String,
              let tapPoints = points(from: dict["points"]) else {
            respond(["ok": false, "error": "missing 'udid' or 'points'"])
            return
        }
        let (ok, error, bad) = doMultiTap(udid: udid, points: tapPoints,
                                          count: dict["count"] as? Int ?? 2,
                                          interval: dict["interval"] as? Double ?? 0.08)
        respond(ok ? ["ok": true] : failure(error ?? "multi-tap failed", badRequest: bad))

    case "type":
        guard let udid = dict["udid"] as? String,
              let text = dict["text"] as? String else {
            respond(["ok": false, "error": "missing 'udid' or 'text'"])
            return
        }
        if doTypeText(udid: udid, text: text) {
            respond(["ok": true])
        } else {
            respond(["ok": false, "error": "type failed"])
        }

    case "button":
        guard let udid = dict["udid"] as? String,
              let name = dict["name"] as? String else {
            respond(["ok": false, "error": "missing 'udid' or 'name'"])
            return
        }
        if doButton(udid: udid, name: name) {
            respond(["ok": true])
        } else {
            respond(["ok": false, "error": "button '\(name)' failed"])
        }

    case "set-hardware-keyboard":
        guard let udid = dict["udid"] as? String,
              let enabled = dict["enabled"] as? Bool else {
            respond(["ok": false, "error": "missing 'udid' or 'enabled'"])
            return
        }
        if setHardwareKeyboard(udid: udid, enabled: enabled) {
            respond(["ok": true, "enabled": enabled])
        } else {
            respond(["ok": false, "error": "set-hardware-keyboard failed"])
        }

    case "selftest-panels":
        // The panel rotations against what was measured on an iPhone Duo
        // (#536), runnable without a simulator: scripts/sim-bridge-selftest.sh,
        // which CI runs. Each failure is named; none means ok.
        let failures = panelSelfTest()
        // What this Xcode lacks of the screen interfaces, reported and not a
        // failure: an Xcode before 27.1 has no foldable to drive.
        respond(["ok": failures.isEmpty, "failures": failures,
                 "screen_interfaces_missing": screenInterfaceProblems()])

    case "panels":
        // The built-in panels and which one is lit (#536). `lit` is null on a
        // device with one panel, which every caller treats as "the screen".
        guard let udid = dict["udid"] as? String, let device = resolveDevice(udid: udid) else {
            respond(["ok": false, "error": "missing 'udid' or device not found"])
            return
        }
        // One enumeration: `panels` is asked before every screenshot of a
        // device not yet known to have one panel (review).
        let read = readPanels(udid: udid, device: device)
        let count = singlePanelDevices.contains(udid) ? 1 : read.all.count
        respond(["ok": true, "count": count, "panels": read.all.map { $0.json() },
                 "lit": read.lit.map { $0.0.json(includeLit: false) } as Any])

    case "screenshot":
        guard let udid = dict["udid"] as? String else {
            respond(["ok": false, "error": "missing 'udid'"])
            return
        }
        let quality = dict["quality"] as? Double ?? 0.8
        let scale = dict["scale"] as? Int ?? 1
        if let (data, width, height) = doScreenshot(udid: udid, quality: quality, scale: scale) {
            respond([
                "ok": true,
                "data": data.base64EncodedString(),
                "width": width,
                "height": height,
            ])
        } else {
            respond(["ok": false, "error": "screenshot failed"])
        }

    default:
        respond(["ok": false, "error": "unknown command: \(cmd)"])
    }
}

// MARK: - Main

setlinebuf(stdout)

// Load frameworks first
loadFrameworks()

// Initialize the AXP translator (triggers lazy init)
_ = sharedTranslator

// Signal ready
respond(["event": "ready"])

// Read commands from stdin
while let line = readLine() {
    guard !line.isEmpty else { continue }
    guard let data = line.data(using: .utf8),
          let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        respond(["ok": false, "error": "invalid JSON"])
        continue
    }
    // Drained per command: each now reads CoreSimulator's port and screen
    // proxies, which nothing else would release over a long session (review).
    autoreleasepool { handleCommand(dict) }
}
