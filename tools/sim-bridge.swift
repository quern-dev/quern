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
    let simKit = (dev as NSString)
        .appendingPathComponent("Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit")
    if dlopen(simKit, RTLD_NOW | RTLD_GLOBAL) == nil {
        logErr("SimulatorKit load failed: \(dlerrorString())")
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

private func hasSimulatorKit(at dev: String) -> Bool {
    let path = (dev as NSString)
        .appendingPathComponent("Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit")
    return FileManager.default.fileExists(atPath: path)
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

    func map(_ macFrame: CGRect) -> CGRect {
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
    let projected = transform.map(macFrame)

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
    let transform = AXFrameTransform(rootFrame: rootFrame, pointSize: pointSize)

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
    let pointSize = devicePointSize(for: device)
    let rootFrame = axFrame(of: frontmostRoot)
    let transform = AXFrameTransform(rootFrame: rootFrame, pointSize: pointSize)

    let macPoint = transform.unmap(CGPoint(x: x, y: y))

    guard let hitTranslation = objectAtPointOnTranslator(
        translator: translator, point: macPoint, displayId: 0, token: token
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
    let kitPath = (dev as NSString).appendingPathComponent(
        "Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit"
    )
    guard let kit = dlopen(kitPath, RTLD_NOW) else { return false }
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

func wrapAndPatch(event: CFTypeRef, edgeBit: UInt8 = 0) -> UnsafeMutableRawPointer? {
    guard let wrapFn = trackpadWrapFn else { return nil }
    let raw = Unmanaged.passUnretained(event as AnyObject).toOpaque()
    guard let msg = wrapFn(raw) else { return nil }
    let target: UInt32 = 0x32
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
                        edgeBit: UInt8 = 0, client: AnyObject) -> Bool {
    guard let event = makeDigitizerEvent(point: point, identifier: identifier, isDown: isDown) else {
        return false
    }
    let msg: UnsafeMutableRawPointer? = withExtendedLifetime(event) {
        wrapAndPatch(event: event, edgeBit: edgeBit)
    }
    guard let msg else { return false }
    sendHIDMessage(msg, to: client)
    return true
}

func clamp01(_ v: Double) -> Double { v < 0 ? 0 : (v > 1 ? 1 : v) }

func doTap(udid: String, x: Double, y: Double, hold: Double = 0.05) -> Bool {
    guard let client = ensureHIDClient(udid: udid) else { return false }
    let size = devicePointSize(for: resolveDevice(udid: udid)!)
    let nx = CGFloat(clamp01(x / Double(size.width)))
    let ny = CGFloat(clamp01(y / Double(size.height)))
    let point = CGPoint(x: nx, y: ny)
    let id = nextTouchId()
    guard sendDigitizerEvent(point: point, identifier: id, isDown: true, client: client) else { return false }
    let holdUs = UInt32(max(0.02, hold) * 1_000_000)
    usleep(holdUs)
    return sendDigitizerEvent(point: point, identifier: id, isDown: false, client: client)
}

func doSwipe(udid: String, x1: Double, y1: Double, x2: Double, y2: Double, duration: Double = 0.3) -> Bool {
    guard let client = ensureHIDClient(udid: udid) else { return false }
    let size = devicePointSize(for: resolveDevice(udid: udid)!)
    let start = CGPoint(x: CGFloat(clamp01(x1 / Double(size.width))),
                        y: CGFloat(clamp01(y1 / Double(size.height))))
    let end = CGPoint(x: CGFloat(clamp01(x2 / Double(size.width))),
                      y: CGFloat(clamp01(y2 / Double(size.height))))
    let steps = 10
    let stepMs = UInt32(max(8, (duration * 1000) / Double(steps + 2)))
    let id = nextTouchId()

    guard sendDigitizerEvent(point: start, identifier: id, isDown: true, client: client) else { return false }
    var ok = 0
    for i in 1...steps {
        usleep(stepMs * 1000)
        let t = Double(i) / Double(steps)
        let p = CGPoint(x: start.x + (end.x - start.x) * CGFloat(t),
                        y: start.y + (end.y - start.y) * CGFloat(t))
        // Move events: use isDown=true for sustained touch (mask 0x07)
        if sendDigitizerEvent(point: p, identifier: id, isDown: true, client: client) { ok += 1 }
    }
    usleep(stepMs * 1000)
    return sendDigitizerEvent(point: end, identifier: id, isDown: false, client: client) && ok >= steps / 2
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

    guard let cgImage = ctx.makeImage() else { return nil }

    // Encode JPEG
    let data = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(data, "public.jpeg" as CFString, 1, nil) else { return nil }
    let options: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: quality]
    CGImageDestinationAddImage(dest, cgImage, options as CFDictionary)
    guard CGImageDestinationFinalize(dest) else { return nil }

    return (data as Data, width, height)
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
        if doSwipe(udid: udid, x1: x1, y1: y1, x2: x2, y2: y2, duration: duration) {
            respond(["ok": true])
        } else {
            respond(["ok": false, "error": "swipe failed"])
        }

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
    handleCommand(dict)
}
