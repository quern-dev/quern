import Foundation
import ObjectiveC

/// Loads the Xcode-private frameworks the simulator capture path needs, and
/// the ObjC runtime helpers for calling into them.
///
/// Everything here is unsupported API reached through `dlopen` and
/// `NSSelectorFromString`. It works on Xcode 26 and has worked across several
/// prior versions, but it is the part of this package most likely to break on
/// a toolchain update, which is why it is quarantined in one file.
///
/// Quern already takes this dependency: `tools/sim-bridge.swift` loads the
/// same frameworks by the same route for accessibility and screenshots. This
/// is not a new category of risk, only more of it.
public enum PrivateFrameworks {
    private nonisolated(unsafe) static var loaded = false
    private static let loadLock = NSLock()

    public static func load() {
        loadLock.lock()
        defer { loadLock.unlock() }
        guard !loaded else { return }
        loaded = true

        let coreSim = "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator"
        if dlopen(coreSim, RTLD_NOW | RTLD_GLOBAL) == nil {
            MediaLog.log("[capture] CoreSimulator load failed: \(dlerrorString())")
        }
        // Not needed for the framebuffer itself — CoreSimulator owns the
        // IOSurface — but loaded so this stays a drop-in neighbour of
        // sim-bridge, which needs it for HID input.
        //
        // Loads the path that was actually found, rather than rebuilding it
        // from a constant the probe agreed with. Same reasoning as
        // `simulatorKitPath` in tools/sim-bridge.swift, which this mirrors.
        if let simKit = simulatorKitPath(at: developerDir()) {
            if dlopen(simKit, RTLD_NOW | RTLD_GLOBAL) == nil {
                MediaLog.log("[capture] SimulatorKit load failed: \(dlerrorString())")
            }
        } else {
            MediaLog.log("[capture] SimulatorKit not found under \(developerDir())")
        }
    }

    static func dlerrorString() -> String {
        guard let e = dlerror() else { return "unknown" }
        return String(cString: e)
    }

    /// The active developer directory, preferring one that actually contains
    /// SimulatorKit — `xcode-select -p` can point at a CLT-only install that
    /// has no private frameworks at all.
    public static func developerDir() -> String {
        if let dev = xcodeSelectDir(), hasSimulatorKit(at: dev) { return dev }
        let canonical = "/Applications/Xcode.app/Contents/Developer"
        if hasSimulatorKit(at: canonical) { return canonical }
        let entries = (try? FileManager.default.contentsOfDirectory(atPath: "/Applications")) ?? []
        for app in entries.sorted()
        where app.hasPrefix("Xcode") && app.hasSuffix(".app") && app != "Xcode.app" {
            let dev = "/Applications/\(app)/Contents/Developer"
            if hasSimulatorKit(at: dev) { return dev }
        }
        return xcodeSelectDir() ?? canonical
    }

    /// Where the SimulatorKit binary sits, relative to a developer directory.
    ///
    /// Xcode 27 moved it out of the developer directory altogether:
    ///
    ///   <= 26  Xcode.app/Contents/Developer/Library/PrivateFrameworks/…
    ///   27+    Xcode.app/Contents/SharedFrameworks/SimulatorKit.framework
    ///
    /// The second is a *sibling* of `Developer`, not a relocation within it,
    /// so nothing rooted at the developer directory reaches it without
    /// stepping up a level. Both are checked rather than switching on a
    /// version: one `stat`, and it survives whatever the next Xcode ships.
    ///
    /// Kept in step with `tools/sim-bridge.swift`, which resolves the same
    /// framework the same way for HID input.
    static let simulatorKitRelativePaths = [
        "Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit",
        "../SharedFrameworks/SimulatorKit.framework/SimulatorKit",
    ]

    /// The SimulatorKit binary for this developer directory, or nil if absent.
    ///
    /// Returns the path rather than a yes/no so callers load the file that was
    /// actually found; a bool plus a rebuilt constant is how the probe and the
    /// `dlopen` came to disagree when Xcode 27 moved the framework.
    public static func simulatorKitPath(at dev: String) -> String? {
        for relative in simulatorKitRelativePaths {
            let path = ((dev as NSString).appendingPathComponent(relative) as NSString)
                .standardizingPath
            if FileManager.default.fileExists(atPath: path) { return path }
        }
        return nil
    }

    static func hasSimulatorKit(at dev: String) -> Bool {
        return simulatorKitPath(at: dev) != nil
    }

    private static func xcodeSelectDir() -> String? {
        let pipe = Pipe()
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        task.arguments = ["-p"]
        task.standardOutput = pipe
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        let out = String(
            data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8
        )?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return out.isEmpty ? nil : out
    }

    // MARK: - ObjC runtime helpers

    static func invokeClassObjWithObjAndError(
        _ cls: AnyClass, _ sel: Selector, _ arg: AnyObject, _ err: inout NSError?
    ) -> NSObject? {
        guard let metaCls = object_getClass(cls),
              let imp = class_getMethodImplementation(metaCls, sel) else { return nil }
        typealias Fn = @convention(c) (
            AnyClass, Selector, AnyObject, AutoreleasingUnsafeMutablePointer<NSError?>
        ) -> AnyObject?
        return unsafeBitCast(imp, to: Fn.self)(cls, sel, arg, &err) as? NSObject
    }

    static func invokeObjWithError(
        _ target: NSObject, _ sel: Selector, _ err: inout NSError?
    ) -> NSObject? {
        guard let imp = class_getMethodImplementation(type(of: target), sel) else { return nil }
        typealias Fn = @convention(c) (
            AnyObject, Selector, AutoreleasingUnsafeMutablePointer<NSError?>
        ) -> AnyObject?
        return unsafeBitCast(imp, to: Fn.self)(target, sel, &err) as? NSObject
    }

    /// Every available `SimDevice` in the default device set.
    public static func availableDevices() -> [NSObject] {
        load()
        guard let cls = NSClassFromString("SimServiceContext") else {
            MediaLog.log("[capture] SimServiceContext unavailable — frameworks did not load")
            return []
        }
        var err: NSError?
        guard let ctx = invokeClassObjWithObjAndError(
            cls, NSSelectorFromString("sharedServiceContextForDeveloperDir:error:"),
            developerDir() as NSString, &err
        ) else {
            MediaLog.log("[capture] sharedServiceContext failed: \(err?.description ?? "nil")")
            return []
        }
        let sel = NSSelectorFromString("defaultDeviceSetWithError:")
        guard ctx.responds(to: sel),
              let set = invokeObjWithError(ctx, sel, &err) else {
            MediaLog.log("[capture] defaultDeviceSet failed: \(err?.description ?? "nil")")
            return []
        }
        return (set.value(forKey: "availableDevices") as? [NSObject]) ?? []
    }

    // MARK: - Screen properties (#538)

    /// Only a yes is kept; a no is cheap to ask again.
    private nonisolated(unsafe) static var screenInterfacesConfirmed = false

    /// Every declaration `panel(of:)` calls, with the return type it reads it
    /// as: what is missing or typed otherwise, by name. Empty means all are
    /// there. The same list as sim-bridge's `screenInterfaceProblems`.
    ///
    /// A descriptor and its properties are XPC proxies that claim every
    /// selector, so neither `responds(to:)` nor `class_getMethodImplementation`
    /// can say whether one has a method, and an unrecognised selector raises
    /// an exception Swift cannot catch -- here, inside a capture that a
    /// recording depends on. So what is called is checked against what
    /// CoreSimulator declares first. Xcode 26.6 has no `deviceName`.
    static func screenInterfaceProblems() -> [String] {
        let wanted: [(String, String, String)] = [
            ("SimScreen", "screenProperties", "@"),
            ("SimScreenProperties", "screenID", "I"),
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
            if !String(cString: types).hasPrefix(type) {
                problems.append("\(protoName).\(method) is not typed \(type)")
            }
        }
        return problems
    }

    static func screenInterfacesAvailable() -> Bool {
        if screenInterfacesConfirmed { return true }
        screenInterfacesConfirmed = screenInterfaceProblems().isEmpty
        return screenInterfacesConfirmed
    }

    /// The built-in panel a display descriptor is (`screenType` 0), or nil:
    /// an overlay or secondary plane, or an Xcode without the interfaces.
    static func panel(of descriptor: NSObject) -> Panel? {
        guard screenInterfacesAvailable(),
              let screen = objc_getProtocol("SimScreen"), descriptor.conforms(to: screen),
              let props = descriptor.perform(NSSelectorFromString("screenProperties"))?
                .takeUnretainedValue() as? NSObject,
              let cls = object_getClass(props) else { return nil }
        typealias U32Fn = @convention(c) (AnyObject, Selector) -> UInt32
        typealias U64Fn = @convention(c) (AnyObject, Selector) -> UInt64
        typealias ObjFn = @convention(c) (AnyObject, Selector) -> AnyObject?
        let idSel = NSSelectorFromString("screenID"), typeSel = NSSelectorFromString("screenType")
        let nameSel = NSSelectorFromString("deviceName")
        guard let idImp = class_getMethodImplementation(cls, idSel),
              let typeImp = class_getMethodImplementation(cls, typeSel),
              let nameImp = class_getMethodImplementation(cls, nameSel) else { return nil }
        guard unsafeBitCast(typeImp, to: U64Fn.self)(props, typeSel) == 0 else { return nil }
        return Panel(
            screenID: unsafeBitCast(idImp, to: U32Fn.self)(props, idSel),
            deviceName: unsafeBitCast(nameImp, to: ObjFn.self)(props, nameSel) as? String ?? ""
        )
    }

    public static func resolveDevice(udid: String) -> NSObject? {
        for device in availableDevices()
        where (device.value(forKey: "UDID") as? NSUUID)?.uuidString.lowercased()
            == udid.lowercased() {
            return device
        }
        return nil
    }
}

/// `SimDevice` state values, which are integers with no public meaning.
public enum SimDeviceState: Int {
    case creating = 0, shutdown = 1, booting = 2, booted = 3, shuttingDown = 4

    public var name: String {
        switch self {
        case .creating: return "creating"
        case .shutdown: return "shutdown"
        case .booting: return "booting"
        case .booted: return "booted"
        case .shuttingDown: return "shutting down"
        }
    }
}
