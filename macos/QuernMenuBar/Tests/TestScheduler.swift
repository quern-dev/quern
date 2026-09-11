// A clock that only moves when a test moves it.
//
// The whole point of the Scheduler seam. Every timing defect review found in
// the update poll needed three minutes of real time to reproduce, which is to
// say none of them was ever going to be covered by a test that waits.

import Foundation

final class FakeWork: ScheduledWork {
    private(set) var isActive = true
    func cancel() { isActive = false }
}

final class TestScheduler: Scheduler {
    private(set) var now = Date(timeIntervalSince1970: 1_000_000)

    private struct Repeating {
        let interval: TimeInterval
        var nextFire: Date
        let body: (ScheduledWork) -> Void
        let work: FakeWork
    }

    private struct Once {
        let due: Date
        let body: () -> Void
    }

    private var repeatingWork: [Repeating] = []
    private var onceWork: [Once] = []

    func repeating(every interval: TimeInterval,
                   _ body: @escaping (ScheduledWork) -> Void) -> ScheduledWork {
        let work = FakeWork()
        repeatingWork.append(
            Repeating(interval: interval, nextFire: now.addingTimeInterval(interval),
                      body: body, work: work)
        )
        return work
    }

    func after(_ delay: TimeInterval, _ body: @escaping () -> Void) {
        // A zero delay would let a body that reschedules itself spin `advance`
        // forever. Nothing in Sources does, but a test that hung with no output
        // would be a poor way to find that out.
        precondition(delay > 0, "a zero delay would make advance(by:) non-terminating")
        onceWork.append(Once(due: now.addingTimeInterval(delay), body: body))
    }

    /// Move the clock, firing everything due along the way.
    ///
    /// Steps to each due time rather than jumping to the end, so a body that
    /// reads `now` sees the time its tick actually represents. Jumping would
    /// make a deadline check pass on the first tick of a long advance.
    func advance(by seconds: TimeInterval) {
        let target = now.addingTimeInterval(seconds)
        while true {
            let due = nextDue()
            guard let due, due <= target else { break }
            now = due
            fireDue()
        }
        // No fireDue() here: the loop above only exits once nothing is due at
        // or before `target`, so anything this could fire has already fired.
        // Verified by deleting it -- the suite stayed green either way, which
        // is what dead code looks like.
        now = target
    }

    private func nextDue() -> Date? {
        let times = repeatingWork.filter { $0.work.isActive }.map(\.nextFire)
            + onceWork.map(\.due)
        return times.min()
    }

    private func fireDue() {
        let ready = onceWork.filter { $0.due <= now }
        onceWork.removeAll { $0.due <= now }
        for item in ready { item.body() }

        for index in repeatingWork.indices {
            guard repeatingWork[index].work.isActive else { continue }
            guard repeatingWork[index].nextFire <= now else { continue }
            repeatingWork[index].nextFire = now.addingTimeInterval(repeatingWork[index].interval)
            repeatingWork[index].body(repeatingWork[index].work)
        }
        repeatingWork.removeAll { !$0.work.isActive }
    }

    /// Timers still running. The leak check: an update that finished must not
    /// leave one behind.
    var liveTimerCount: Int { repeatingWork.filter { $0.work.isActive }.count }
}
