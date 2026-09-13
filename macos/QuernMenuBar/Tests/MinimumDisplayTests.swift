// The floor under the "Checking…" indicator, in simulated time.
//
// Real time would mean a suite that sits for the better part of a second per
// case to observe something whose entire purpose is to be brief.

import Foundation

enum MinimumDisplayTests {
    private static func rig(_ duration: TimeInterval = 1.0)
        -> (TestScheduler, MinimumDisplay) {
        let clock = TestScheduler()
        return (clock, MinimumDisplay(duration: duration, scheduler: clock))
    }

    static func all() {
        Harness.test("a fast answer is held until the floor is reached") {
            let (clock, display) = rig()
            var finished = false
            display.begin()
            clock.advance(by: 0.1)
            display.end { finished = true }
            Harness.expect(finished, false, "held at 0.1s")
            clock.advance(by: 0.8)
            Harness.expect(finished, false, "held at 0.9s")
            clock.advance(by: 0.2)
            Harness.expect(finished, true, "released at 1.1s")
        }

        Harness.test("a slow answer is not delayed any further") {
            // The floor is a minimum, not a delay. A check that already took
            // three seconds must not then sit for another one.
            let (clock, display) = rig()
            var finished = false
            display.begin()
            clock.advance(by: 3.0)
            display.end { finished = true }
            Harness.expect(finished, true, "released immediately")
        }

        Harness.test("an answer at exactly the floor is not delayed") {
            // The boundary, because `remaining > 0` and `remaining >= 0` differ
            // only here and only one of them schedules a zero-delay timer.
            let (clock, display) = rig()
            var finished = false
            display.begin()
            clock.advance(by: 1.0)
            display.end { finished = true }
            Harness.expect(finished, true, "released at exactly the floor")
        }

        Harness.test("ending without beginning still runs the completion") {
            // A caller that lost track of its own start must still get its
            // completion: the alternative is an indicator that never comes down
            // and a reentrancy flag that stays set, which disables the menu
            // item permanently.
            let (_, display) = rig()
            var finished = false
            display.end { finished = true }
            Harness.expect(finished, true, "released with no begin()")
        }

        Harness.test("a second use starts its own floor") {
            // The instance outlives one check -- AppDelegate holds one for the
            // life of the app -- so a stale start time from the previous run
            // would let the next fast check through with no hold at all.
            let (clock, display) = rig()
            display.begin()
            clock.advance(by: 5.0)
            display.end {}

            var finished = false
            display.begin()
            clock.advance(by: 0.2)
            display.end { finished = true }
            Harness.expect(finished, false, "second check held")
            clock.advance(by: 1.0)
            Harness.expect(finished, true, "second check released")
        }

        Harness.test("the completion runs exactly once") {
            let (clock, display) = rig()
            var calls = 0
            display.begin()
            display.end { calls += 1 }
            clock.advance(by: 10.0)
            Harness.expect(calls, 1, "completion calls")
        }

        Harness.test("the default duration is the shipped floor") {
            // The gap a review found: every other case passes an explicit
            // duration, so the default argument -- the only construction
            // anywhere in Sources -- was never exercised. Changing it to zero
            // left all 41 tests green while shipping the exact flicker this
            // file exists to remove.
            let clock = TestScheduler()
            let display = MinimumDisplay(scheduler: clock)
            var finished = false
            display.begin()
            clock.advance(by: MinimumDisplay.standard / 2)
            display.end { finished = true }
            Harness.expect(finished, false, "default floor held")
            clock.advance(by: MinimumDisplay.standard)
            Harness.expect(finished, true, "default floor released")
        }

        Harness.test("the shipped floor is long enough to be seen") {
            // A real check measures 0.47-0.94s, so a floor at or below the fast
            // end would leave the flicker this exists to remove. Asserted
            // because the constant is the whole feature: set it to 0.05 and
            // every other case here still passes.
            Harness.expect(MinimumDisplay.standard >= 0.5,
                           "floor must exceed the flicker threshold")
            Harness.expect(MinimumDisplay.standard <= 1.0,
                           "floor must not make a fast check feel slow")
        }
    }
}

// The flag QuernCLI's watchdog hands back to the thread waiting on the
// process. Tested here rather than in its own file because it is four lines
// and exists for one call site.
enum FlagTests {
    static func all() {
        Harness.test("a flag starts clear and latches when set") {
            let flag = Flag()
            Harness.expect(flag.isSet, false, "initial")
            flag.set()
            Harness.expect(flag.isSet, true, "after set")
        }

        Harness.test("concurrent setters and readers agree at the end") {
            // Not a proof -- a data race need not show itself. It runs under
            // the thread sanitiser in CI, which is what actually detects one,
            // and it fails outright if `set` and `isSet` ever disagree.
            let flag = Flag()
            let group = DispatchGroup()
            for _ in 0..<200 {
                DispatchQueue.global().async(group: group) { flag.set() }
                DispatchQueue.global().async(group: group) { _ = flag.isSet }
            }
            // Bounded. An unbalanced lock in `set` or `isSet` would deadlock
            // here, and an untimed wait turns that into a CI job that hangs
            // until the runner kills it -- with no failing test to point at.
            let finished = group.wait(timeout: .now() + 10) == .success
            Harness.expect(finished, "the flag deadlocked")
            Harness.expect(flag.isSet, true, "settled")
        }
    }
}
