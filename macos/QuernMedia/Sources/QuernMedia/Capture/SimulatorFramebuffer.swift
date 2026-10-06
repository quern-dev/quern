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
///
/// The simulator is watched for as long as this runs, because it can shut
/// down underneath it -- quitting Simulator.app shuts down every simulator,
/// headless ones included. Nothing in the framebuffer says so: the callbacks
/// simply stop, which looks exactly like a still screen, and a viewer was
/// left showing the last frame as if it were live. Now a shutdown detaches
/// and reports itself through `onAvailability`, and a reboot reattaches to
/// the new framebuffer.
public final class SimulatorFramebuffer: FrameSource {
    private let udid: String
    private let queue = DispatchQueue(label: "quern.media.simulator", qos: .userInteractive)
    private let onFrame: (CapturedFrame) -> Void

    private let stateLock = NSLock()
    private var stopped = false
    private var ioClient: NSObject?
    private var descriptors: [NSObject] = []
    private var callbackUUIDs: [ObjectIdentifier: NSUUID] = [:]

    /// The resolved `SimDevice`, kept to read its state. Capture queue only.
    private var device: NSObject?
    /// Whether the framebuffer callbacks are registered. Capture queue only.
    private var attached = false
    private var monitor: DispatchSourceTimer?

    /// How often the simulator's state is read while streaming.
    public var stateCheckInterval: TimeInterval = 1

    /// Called on the capture queue when the simulator goes away (false, with
    /// the reason) and when it is back and attached again (true). Set before
    /// `start()`.
    public var onAvailability: ((Bool, String) -> Void)?

    /// Marks `queue` with *this instance's* identity.
    ///
    /// The value carries the identity, not merely the key's presence. A
    /// shared key with a `Void` value answered "yes, you are on my queue"
    /// while standing on any *other* instance's queue — so stopping source A
    /// from inside source B's frame callback ran A's cleanup inline on B's
    /// queue, mutating A's descriptors while A's own queue was iterating
    /// them. That is the race the queue hop exists to prevent, reintroduced
    /// by the check meant to avoid deadlocking on it.
    private static let queueKey = DispatchSpecificKey<ObjectIdentifier>()

    public init(udid: String, onFrame: @escaping (CapturedFrame) -> Void) {
        self.udid = udid
        self.onFrame = onFrame
        queue.setSpecific(key: Self.queueKey, value: ObjectIdentifier(self))
    }

    public func start() throws {
        PrivateFrameworks.load()

        guard let device = PrivateFrameworks.resolveDevice(udid: udid) else {
            throw SimulatorFramebufferError.deviceNotFound(udid)
        }
        // A shut-down device still resolves and still hands back an `io`
        // client; it simply never composites. Failing here beats a window that
        // stays black with no explanation.
        let raw = Self.state(of: device)
        guard raw == SimDeviceState.booted.rawValue else {
            let name = SimDeviceState(rawValue: raw)?.name ?? "unknown(\(raw))"
            throw SimulatorFramebufferError.notBooted(name)
        }

        try queue.sync {
            self.device = device
            try self.attach(device)
        }
        startMonitor()
    }

    private static func state(of device: NSObject) -> Int {
        (device.value(forKey: "state") as? NSNumber)?.intValue ?? -1
    }

    /// Finds the display descriptors and registers for their frames. Capture
    /// queue only. Throws with nothing registered.
    private func attach(_ device: NSObject) throws {
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
        guard !descriptors.isEmpty else {
            ioClient = nil
            throw SimulatorFramebufferError.noFramebuffer
        }
        MediaLog.log("[capture] framebuffer descriptors: \(descriptors.count)")

        do {
            for desc in descriptors { try register(on: desc) }
        } catch {
            detach()
            throw error
        }
        attached = true

        // Nothing composites on an idle screen, so the callback alone can
        // leave a consumer with no frames at all until the user touches
        // something. Prime it with whatever is on screen now.
        queue.async { [weak self] in self?.captureLatest() }
    }

    /// Unregisters every callback and drops the framebuffer. Capture queue
    /// only, and safe to call when nothing is attached.
    private func detach() {
        let unregSel = NSSelectorFromString("unregisterScreenCallbacksWithUUID:")
        for desc in descriptors {
            if let uuid = callbackUUIDs[ObjectIdentifier(desc)], desc.responds(to: unregSel) {
                desc.perform(unregSel, with: uuid)
            }
        }
        descriptors.removeAll()
        callbackUUIDs.removeAll()
        ioClient = nil
        attached = false
    }

    private func startMonitor() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + stateCheckInterval, repeating: stateCheckInterval)
        timer.setEventHandler { [weak self] in self?.checkState() }
        stateLock.lock()
        monitor = timer
        stateLock.unlock()
        timer.resume()
    }

    /// Detaches when the simulator stops being booted, and reattaches when it
    /// is booted again. A reattach that fails -- the framebuffer can lag the
    /// state by a moment during boot -- is simply tried again next tick.
    private func checkState() {
        stateLock.lock()
        let done = stopped
        stateLock.unlock()
        guard !done, let device else { return }

        let raw = Self.state(of: device)
        let booted = raw == SimDeviceState.booted.rawValue
        if attached && !booted {
            detach()
            let name = SimDeviceState(rawValue: raw)?.name ?? "unknown(\(raw))"
            MediaLog.log("[capture] simulator \(udid) is no longer booted (\(name))")
            onAvailability?(false, "the simulator is \(name.lowercased())")
        } else if !attached && booted {
            do {
                try attach(device)
                MediaLog.log("[capture] simulator \(udid) is booted again; streaming")
                onAvailability?(true, "the simulator is booted")
            } catch {
                // Not yet. The next tick tries again.
            }
        }
    }

    /// Re-delivers the current framebuffer surface, exactly as `start()`
    /// primes with. Safe from any thread, and a no-op once stopped —
    /// `captureLatest` re-checks that on the queue as well.
    public func requestCurrentFrame() {
        stateLock.lock()
        let done = stopped
        stateLock.unlock()
        guard !done else { return }
        queue.async { [weak self] in self?.captureLatest() }
    }

    /// Whether the caller is standing on *this instance's* capture queue.
    ///
    /// The distinction `stop()` turns on, and the reason the specific key
    /// carries an identity rather than being merely present: with a shared
    /// key and a `Void` value this answered yes on any instance's queue.
    var isOnCaptureQueue: Bool {
        DispatchQueue.getSpecific(key: Self.queueKey) == ObjectIdentifier(self)
    }

    /// Runs `body` on this instance's capture queue. Test seam for the above.
    func onCaptureQueue<T>(_ body: () -> T) -> T {
        queue.sync(execute: body)
    }

    /// Safe from any thread, including from inside `onFrame`.
    public func stop() {
        // The flag goes up first, so a `captureLatest` already sitting in the
        // queue behind this returns without touching anything.
        stateLock.lock()
        let already = stopped
        stopped = true
        let timer = monitor
        monitor = nil
        stateLock.unlock()
        guard !already else { return }
        timer?.cancel()

        // The rest runs on the capture queue, because `captureLatest` reads
        // `descriptors` there. Clearing it from the caller's thread raced a
        // callback that had already been enqueued: a torn read at best, a
        // frame delivered after stop() returned at worst.
        let cleanup = {
            self.detach()
            self.device = nil
        }

        // `onFrame` runs on this queue, so a consumer that stops the source
        // from inside its own frame callback is already here -- and
        // `queue.sync` onto the serial queue you are standing on deadlocks.
        if isOnCaptureQueue {
            cleanup()
        } else {
            queue.sync(execute: cleanup)
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
