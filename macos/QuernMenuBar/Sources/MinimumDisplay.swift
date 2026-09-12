// Keeps a transient indicator on screen long enough to be read.
//
// "Checking…" appears beside the menu-bar icon when a check starts and is
// cleared when it answers. A real check measures 0.47-0.94s, so the indicator
// can come and go inside half a second -- and a status that appears and
// disappears that fast does not read as "it checked", it reads as nothing
// having happened, which is the complaint this whole path exists to fix. It is
// also indistinguishable from a menu item that is simply broken, so the faster
// the check gets the worse the button looks.
//
// So the indicator has a floor. Not the check: nothing here delays the network
// request or the answer, only how soon the app is allowed to stop admitting it
// did the work.
//
// Separate from AppDelegate because AppDelegate cannot be built without a
// status bar, which is the same reason LifecycleController is its own type. A
// floor whose only test is a person watching a menu bar with a stopwatch is a
// floor nobody checks again.

import Foundation

final class MinimumDisplay {
    /// Long enough to register as a distinct beat rather than a flicker, and
    /// short enough to be invisible in the common case: a check that already
    /// takes 0.47-0.94s usually outlasts this on its own, so the floor only
    /// engages on the fast answers -- a cached reply, a hostname that fails to
    /// resolve immediately -- which are exactly the ones that flashed.
    static let standard: TimeInterval = 0.8

    private let duration: TimeInterval
    private let scheduler: Scheduler
    private var startedAt: Date?

    init(duration: TimeInterval = MinimumDisplay.standard,
         scheduler: Scheduler = SystemScheduler()) {
        self.duration = duration
        self.scheduler = scheduler
    }

    /// Marks the moment the indicator went up.
    func begin() {
        startedAt = scheduler.now
    }

    /// Runs `body` once the indicator has been up for the full duration.
    ///
    /// Immediately if it already has -- the floor is a minimum, not a delay,
    /// and a check that took three seconds must not then sit for another one.
    /// Immediately too if `begin()` was never called: a caller that lost track
    /// of its own start should still get its completion, because the
    /// alternative is an indicator that never comes down.
    func end(_ body: @escaping () -> Void) {
        guard let started = startedAt else {
            body()
            return
        }
        // No reset of `startedAt` here, deliberately. It looks like it belongs
        // -- one begin, one end -- but nothing can observe it: begin() is the
        // only writer and sets it afresh each time, and QuernCLI.run delivers
        // exactly one completion, so a second end() against one begin() never
        // happens. Deleting the line left the suite green, which is this
        // project's definition of dead code; see the same note in
        // TestScheduler.advance(by:).
        let remaining = duration - scheduler.now.timeIntervalSince(started)
        guard remaining > 0 else {
            body()
            return
        }
        scheduler.after(remaining, body)
    }
}
