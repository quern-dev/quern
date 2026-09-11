// Where the app gets the time, and where it asks for work to happen later.
//
// Exists so the things that depend on both can be tested without waiting for
// them. Every defect review found in the update poll was a timing defect: a
// deadline that could not be reached, a deadline that counted ticks instead of
// seconds, a flag left set because a call never came back. None of those is
// visible by reading, and none is reachable by a test that has to sit through
// three minutes of real time to see one.
//
// Deliberately small. Two ways to schedule and one way to read the clock is
// everything the app does with time, and a wider protocol would be surface
// nothing implements twice.

import Foundation

/// A cancellable piece of repeating work.
protocol ScheduledWork: AnyObject {
    /// False once cancelled. Checked after an async hop to find out whether the
    /// loop was stopped while a call was in flight.
    var isActive: Bool { get }
    func cancel()
}

protocol Scheduler {
    var now: Date { get }

    /// Run `body` every `interval` until cancelled. `body` receives its own
    /// handle so it can stop itself, which is the common case.
    func repeating(every interval: TimeInterval,
                   _ body: @escaping (ScheduledWork) -> Void) -> ScheduledWork

    /// Run `body` once, after `delay`.
    func after(_ delay: TimeInterval, _ body: @escaping () -> Void)
}

// MARK: - The real one

final class TimerWork: ScheduledWork {
    fileprivate var timer: Timer?
    var isActive: Bool { timer?.isValid ?? false }
    func cancel() {
        timer?.invalidate()
        timer = nil
    }
}

struct SystemScheduler: Scheduler {
    var now: Date { Date() }

    func repeating(every interval: TimeInterval,
                   _ body: @escaping (ScheduledWork) -> Void) -> ScheduledWork {
        let work = TimerWork()
        let timer = Timer(timeInterval: interval, repeats: true) { _ in body(work) }
        work.timer = timer
        // `.common` rather than `.default`: the default mode does not fire while
        // a menu is tracking or a modal is up, both of which this app puts on
        // screen itself. A deadline that stops running when the user opens the
        // menu is not a deadline.
        RunLoop.main.add(timer, forMode: .common)
        return work
    }

    func after(_ delay: TimeInterval, _ body: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: body)
    }
}
