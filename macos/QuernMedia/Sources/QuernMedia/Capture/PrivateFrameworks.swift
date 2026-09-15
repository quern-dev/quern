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
        let simKit = (developerDir() as NSString)
            .appendingPathComponent("Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit")
        if dlopen(simKit, RTLD_NOW | RTLD_GLOBAL) == nil {
            MediaLog.log("[capture] SimulatorKit load failed: \(dlerrorString())")
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

    static func hasSimulatorKit(at dev: String) -> Bool {
        let path = (dev as NSString)
            .appendingPathComponent("Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit")
        return FileManager.default.fileExists(atPath: path)
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
