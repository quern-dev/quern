// The update poll, in simulated time.
//
// Every case here is a defect review found in #136. They were all found by
// reading, because reproducing one meant sitting through a three-minute
// deadline; that is the gap this file closes.

import Foundation

enum UpdaterTests {
    /// An updater wired to a test clock, with the CLI and the relaunch replaced.
    private struct Rig {
        let clock = TestScheduler()
        var versionAnswers: [String?] = []
        var versionHangs = false
        var updateResult: (Int32, String) = (0, "")

        final class Record {
            var statuses: [String] = []
            var failures: [(String, String)] = []
            var relaunchedInto: String?
            var versionCalls = 0
            /// Holds the updater alive for the length of the test.
            ///
            /// Not bookkeeping. The poll body captures `[weak self]` and cancels
            /// itself when that is nil, so an updater nobody retains stops on
            /// its first tick -- which is exactly what the real app must do,
            /// and exactly what made these two tests fail until the rig kept
            /// one the way `AppDelegate` does.
            var updater: Updater?
        }
    }

    private static func run(
        baseline: String?,
        thenVersions: [String?],
        updateResult: (Int32, String) = (0, ""),
        versionHangs: Bool = false
    ) -> (Updater, TestScheduler, Rig.Record) {
        let clock = TestScheduler()
        let record = Rig.Record()
        var queue: [String?] = [baseline] + thenVersions

        var deps = Updater.Dependencies()
        deps.scheduler = clock
        deps.readVersion = { done in
            record.versionCalls += 1
            if versionHangs && record.versionCalls > 1 { return }  // never answers
            let next = queue.isEmpty ? queue.last ?? nil : queue.removeFirst()
            done(next, next == nil ? "no answer" : "quern \(next!)")
        }
        deps.runUpdate = { done in done(updateResult.0, updateResult.1) }
        deps.relaunch = { record.relaunchedInto = $0 }

        let updater = Updater(deps)
        record.updater = updater
        updater.restartToUpdate(
            status: { record.statuses.append($0) },
            failure: { record.failures.append(($0, $1)) }
        )
        return (updater, clock, record)
    }

    static func all() {
        Harness.test("a changed version relaunches into it") {
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.17.0"])
            clock.advance(by: 4)
            Harness.expect(r.relaunchedInto, "0.17.0", "relaunch target")
            Harness.expect(clock.liveTimerCount, 0, "timers left running")
        }

        Harness.test("an unreadable baseline does not start the update") {
            let (_, _, r) = run(baseline: nil, thenVersions: ["0.17.0"])
            Harness.expect(r.relaunchedInto == nil, "must not relaunch without a baseline")
            Harness.expect(r.failures.count, 1, "failure reports")
        }

        Harness.test("a nil baseline cannot be mistaken for a version change") {
            // The #136 finding: nil baseline compares unequal to every later
            // reading, so the first successful poll looked like the update
            // completing and relaunched into the bundle being replaced.
            let (_, clock, r) = run(baseline: nil, thenVersions: ["0.16.1", "0.16.1"])
            clock.advance(by: 10)
            Harness.expect(r.relaunchedInto == nil, "relaunched off a nil baseline")
        }

        Harness.test("a hung version check still reaches the deadline") {
            // The in-flight guard used to skip the tick that held the only
            // deadline check, so a call that never answered meant "Updating…"
            // for the life of the process.
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: [], versionHangs: true)
            clock.advance(by: 200)
            Harness.expect(clock.liveTimerCount, 0, "timer still running past the deadline")
            Harness.expect(r.statuses.last?.contains("did not change") == true,
                           "expected a give-up status, got \(r.statuses.last ?? "none")")
        }

        Harness.test("an unchanged version is not reported as a timeout") {
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.16.1", "0.16.1"])
            clock.advance(by: 200)
            Harness.expect(r.relaunchedInto == nil, "nothing changed, so nothing to relaunch into")
            Harness.expect(r.statuses.last?.contains("timed out") == false,
                           "`quern update` exited 0; calling that a timeout reports a "
                               + "failure for something that worked")
        }

        Harness.test("a failed update stops and reports") {
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.17.0"],
                                    updateResult: (1, "git pull failed"))
            clock.advance(by: 10)
            Harness.expect(r.relaunchedInto == nil, "must not relaunch after a failed update")
            Harness.expect(r.failures.count, 1, "failure reports")
            Harness.expect(clock.liveTimerCount, 0, "no poll should have started")
        }

        Harness.test("a second click while one is running is ignored") {
            let clock = TestScheduler()
            let record = Rig.Record()
            var deps = Updater.Dependencies()
            deps.scheduler = clock
            deps.readVersion = { done in
                record.versionCalls += 1
                done("0.16.1", "quern 0.16.1")
            }
            deps.runUpdate = { _ in }  // never completes: the update is in flight
            deps.relaunch = { record.relaunchedInto = $0 }

            let updater = Updater(deps)
            record.updater = updater
            let go = { updater.restartToUpdate(status: { record.statuses.append($0) },
                                               failure: { record.failures.append(($0, $1)) }) }
            go()
            let afterFirst = record.versionCalls
            go()
            Harness.expect(record.versionCalls, afterFirst,
                           "a second click started a second update")
        }
    }
}
