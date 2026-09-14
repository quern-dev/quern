import CoreMedia
import Foundation
import IOSurface
import ObjectiveC

public enum SimulatorFramebufferError: Error, CustomStringConvertible {
    case deviceNotFound(String)
    case notBooted(String)
    case ioUnavailable
    case noFramebuffer
    case callbackUnavailable

    public var description: String {
        switch self {
        case .deviceNotFound(let u): return "simulator not found: \(u)"
        case .notBooted(let s): return "simulator is not booted (state: \(s))"
        case .ioUnavailable: return "device.io unavailable"
        case .noFramebuffer: return "no com.apple.framebuffer.display descriptor"
        case .callbackUnavailable: return "registerScreenCallbacks selector unavailable"
        }
    }
}

/// Frames from a booted simulator, straight off CoreSimulator's framebuffer.
///
/// A simulator never appears as a capture device, which is the whole reason
/// the AVFoundation path cannot see one. This subscribes to the framebuffer
/// instead — the same IOSurface `sim-bridge` reads one-shot for screenshots,
/// but via a continuous callback rather than a poll.
///
/// No Simulator.app involved: `simctl boot` is enough, and the framebuffer
/// composites regardless. Event-driven, so an idle screen produces nothing
/// and costs nothing.
public final class SimulatorFramebuffer: FrameSource {
    private let udid: String
    private let queue = DispatchQueue(label: "quern.media.simulator", qos: .userInteractive)
    private let onFrame: (CapturedFrame) -> Void

    private let stateLock = NSLock()
    private var stopped = false
    private var ioClient: NSObject?
    private var descriptors: [NSObject] = []
    private var callbackUUIDs: [ObjectIdentifier: NSUUID] = [:]

    public init(udid: String, onFrame: @escaping (CapturedFrame) -> Void) {
        self.udid = udid
        self.onFrame = onFrame
    }

    public func start() throws {
        PrivateFrameworks.load()

        guard let device = PrivateFrameworks.resolveDevice(udid: udid) else {
            throw SimulatorFramebufferError.deviceNotFound(udid)
        }
        // A shut-down device still resolves and still hands back an `io`
        // client; it simply never composites. Failing here beats a window that
        // stays black with no explanation.
        let raw = (device.value(forKey: "state") as? NSNumber)?.intValue ?? -1
        guard raw == SimDeviceState.booted.rawValue else {
            let name = SimDeviceState(rawValue: raw)?.name ?? "unknown(\(raw))"
            throw SimulatorFramebufferError.notBooted(name)
        }

        guard let io = device.perform(NSSelectorFromString("io"))?
            .takeUnretainedValue() as? NSObject else {
            throw SimulatorFramebufferError.ioUnavailable
        }
        ioClient = io

        io.perform(NSSelectorFromString("updateIOPorts"))
        guard let ports = io.value(forKey: "deviceIOPorts") as? [NSObject] else {
            throw SimulatorFramebufferError.noFramebuffer
        }

        let pidSel = NSSelectorFromString("portIdentifier")
        let descSel = NSSelectorFromString("descriptor")
        let surfSel = NSSelectorFromString("framebufferSurface")

        // Every display descriptor, not the first.
        //
        // A simulator exposes secondary planes and overlays — `simctl io
        // screenshot` says as much when it reports defaulting to a display —
        // and the main screen is simply whichever live surface is largest at
        // that moment. A booted iPhone reports two.
        for port in ports where port.responds(to: pidSel) {
            guard let pid = port.perform(pidSel)?.takeUnretainedValue(),
                  "\(pid)" == "com.apple.framebuffer.display",
                  port.responds(to: descSel),
                  let desc = port.perform(descSel)?.takeUnretainedValue() as? NSObject,
                  desc.responds(to: surfSel) else { continue }
            descriptors.append(desc)
        }
        guard !descriptors.isEmpty else { throw SimulatorFramebufferError.noFramebuffer }
        MediaLog.log("[capture] framebuffer descriptors: \(descriptors.count)")

        for desc in descriptors { try register(on: desc) }

        // Nothing composites on an idle screen, so the callback alone can
        // leave a consumer with no frames at all until the user touches
        // something. Prime it with whatever is on screen now.
        queue.async { [weak self] in self?.captureLatest() }
    }

    /// Not safe to call from the capture queue -- it waits on it.
    public func stop() {
        // The flag goes up first, so a `captureLatest` already sitting in the
        // queue behind this returns without touching anything.
        stateLock.lock()
        let already = stopped
        stopped = true
        stateLock.unlock()
        guard !already else { return }

        // The rest runs on the capture queue, because `captureLatest` reads
        // `descriptors` there. Clearing it from the caller's thread raced a
        // callback that had already been enqueued: a torn read at best, a
        // frame delivered after stop() returned at worst.
        queue.sync {
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
    }

    private func register(on desc: NSObject) throws {
        let regSel = NSSelectorFromString(
            "registerScreenCallbacksWithUUID:callbackQueue:frameCallback:"
                + "surfacesChangedCallback:propertiesChangedCallback:"
        )
        guard desc.responds(to: regSel),
              let imp = class_getMethodImplementation(type(of: desc), regSel) else {
            throw SimulatorFramebufferError.callbackUnavailable
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
            desc, regSel, uuid, queue as AnyObject,
            frame as AnyObject, surfaces as AnyObject, props as AnyObject
        )
    }

    private func captureLatest() {
        stateLock.lock()
        let done = stopped
        stateLock.unlock()
        guard !done else { return }

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
        guard let best else { return }
        // The callback carries no timestamp — it says only that a frame
        // happened — so this is arrival time, later and noisier than the true
        // composite by an unmeasured amount. Marked accordingly so a consumer
        // aligning against logs knows what it is holding.
        onFrame(CapturedFrame(
            surface: best,
            time: CMClockGetTime(CMClockGetHostTimeClock()),
            timeAccuracy: .arrival
        ))
    }
}
