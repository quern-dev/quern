import Foundation

/// Runs a closure at most once, however many ways the process can end, and
/// hands every caller the status it produced.
///
/// A second caller arriving while the body is still running **waits** for it.
/// Marking the guard done on entry and returning the not-yet-written status
/// reported a failure as 0, and let that caller carry on -- so an exit racing
/// a recording still being finalised produced the moov-less file this exists
/// to prevent. The same-thread re-entrant case returns instead of waiting,
/// because that is `atexit` firing inside `exit()` and there is nobody left
/// to wait for.
public final class ShutdownGuard {
    private let body: () -> Int32
    private let condition = NSCondition()
    private var running = false
    private var owner: Thread?
    private var completed: Int32?

    public init(_ body: @escaping () -> Int32) { self.body = body }

    @discardableResult
    public func run() -> Int32 {
        condition.lock()
        if owner == Thread.current {
            condition.unlock()
            return completed ?? 0
        }
        while running { condition.wait() }
        if let completed {
            condition.unlock()
            return completed
        }
        running = true
        owner = Thread.current
        condition.unlock()

        let result = body()

        condition.lock()
        completed = result
        running = false
        owner = nil
        condition.broadcast()
        condition.unlock()
        return result
    }
}
