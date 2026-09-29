import Foundation
import Testing
@testable import QuernMedia

/// Lock-backed, so the body and the assertions can share it across threads.
private final class Box: @unchecked Sendable {
    private let lock = NSLock()
    private var runs = 0
    private var observed: Int32?

    func recordRun() { lock.lock(); runs += 1; lock.unlock() }
    var runCount: Int { lock.lock(); defer { lock.unlock() }; return runs }

    func observe(_ status: Int32) { lock.lock(); observed = status; lock.unlock() }
    var seen: Int32? { lock.lock(); defer { lock.unlock() }; return observed }
}

@Test("the body runs once however many callers arrive")
func bodyRunsOnce() {
    let box = Box()
    let guardian = ShutdownGuard { box.recordRun(); return 0 }
    #expect(guardian.run() == 0)
    #expect(guardian.run() == 0)
    #expect(guardian.run() == 0)
    #expect(box.runCount == 1)
}

@Test("every caller sees the status the body produced")
func statusReachesEveryCaller() {
    let guardian = ShutdownGuard { 1 }
    #expect(guardian.run() == 1)
    #expect(guardian.run() == 1, "a later caller lost the failure")
}

@Test("a caller arriving mid-run waits for the real status", .timeLimit(.minutes(1)))
func concurrentCallerWaitsRatherThanGuessing() throws {
    // The bug this pins: marking the guard done on entry and returning the
    // not-yet-written status reported a failure as 0, and let that caller
    // carry on while the recording was still being finalised -- which is the
    // moov-less file the guard exists to prevent.
    let bodyStarted = DispatchSemaphore(value: 0)
    let letBodyFinish = DispatchSemaphore(value: 0)
    let box = Box()

    let guardian = ShutdownGuard {
        bodyStarted.signal()
        letBodyFinish.wait()
        return 1
    }

    // Caller one starts the body and blocks inside it.
    DispatchQueue.global().async { _ = guardian.run() }
    #expect(bodyStarted.wait(timeout: .now() + 5) == .success, "the body never started")

    // Caller two arrives while the body is still running.
    DispatchQueue.global().async { box.observe(guardian.run()) }

    // It must not have answered yet: the status does not exist.
    Thread.sleep(forTimeInterval: 0.3)
    #expect(box.seen == nil, "a mid-run caller answered before the body finished")

    letBodyFinish.signal()

    let deadline = Date().addingTimeInterval(5)
    while box.seen == nil, Date() < deadline { usleep(5_000) }
    #expect(box.seen == 1, "the concurrent caller read a failure as success")
}
