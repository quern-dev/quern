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
            var progress: [UpdateProgress] = []
            var failures: [(String, String)] = []
            var relaunchedInto: String?
            var versionCalls = 0
            var parked: [(String?, String) -> Void] = []
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
            if versionHangs && record.versionCalls > 1 {
                // Parked, not dropped: a test can answer it later, which is how
                // a completion arriving after the deadline gets reproduced. In
                // production `QuernCLI.run` hops to the main queue, so that
                // ordering is real; under a fake that answers inline it cannot
                // happen, and the guard against it was untestable.
                record.parked.append(done)
                return
            }
            let next = queue.isEmpty ? queue.last ?? nil : queue.removeFirst()
            done(next, next == nil ? "no answer" : "quern \(next!)")
        }
        deps.runUpdate = { done in done(updateResult.0, updateResult.1) }
        deps.relaunch = { record.relaunchedInto = $0 }

        let updater = Updater(deps)
        record.updater = updater
        updater.restartToUpdate(
            status: { record.progress.append($0); record.statuses.append($0.text) },
            failure: { record.failures.append(($0, $1)) }
        )
        return (updater, clock, record)
    }

    /// A failure must leave the updater usable. `inProgress` is a busy marker,
    /// and a marker that survives a failure makes "Restart to Update" a dead
    /// menu item for the life of the process -- the menu keeps offering it,
    /// because it is gated on `update_available`, which nothing rewrites.
    private static func expectRetryable(_ updater: Updater, _ r: Rig.Record, _ after: String) {
        let before = r.versionCalls
        updater.restartToUpdate(status: { _ in }, failure: { _, _ in })
        Harness.expect(r.versionCalls > before,
                       "after \(after), a second attempt was refused — the busy flag "
                           + "survived the failure")
    }

    static func all() {
        Harness.test("a changed version relaunches into it") {
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.17.0"])
            clock.advance(by: 4)
            Harness.expect(r.relaunchedInto, "0.17.0", "relaunch target")
            Harness.expect(clock.liveTimerCount, 0, "timers left running")
        }

        Harness.test("an unreadable baseline does not start the update") {
            let (u, _, r) = run(baseline: nil, thenVersions: ["0.17.0"])
            Harness.expect(r.relaunchedInto == nil, "must not relaunch without a baseline")
            Harness.expect(r.failures.count, 1, "failure reports")
            expectRetryable(u, r, "an unreadable baseline")
        }

        Harness.test("a nil baseline cannot be mistaken for a version change") {
            // The #136 finding: nil baseline compares unequal to every later
            // reading, so the first successful poll looked like the update
            // completing and relaunched into the bundle being replaced.
            let (_, clock, r) = run(baseline: nil, thenVersions: ["0.16.1", "0.16.1"])
            clock.advance(by: 10)
            Harness.expect(r.relaunchedInto == nil, "relaunched off a nil baseline")
        }

        Harness.test("the deadline is short, because the update has already finished") {
            // `quern update` is synchronous, so the poll is only waiting for a
            // version read to start working again after the daemon restart.
            // Sizing it for a background update left "Updating…" on screen for
            // three minutes after the work was done.
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.16.1", "0.16.1"])
            clock.advance(by: 25)
            Harness.expect(r.progress.last?.isWorking == true,
                           "25s in, it should still be waiting")
            clock.advance(by: 10)
            Harness.expect(r.progress.last?.isWorking == false,
                           "by 35s it should have given up and said so")
        }

        Harness.test("a hung version check still reaches the deadline") {
            // The in-flight guard used to skip the tick that held the only
            // deadline check, so a call that never answered meant "Updating…"
            // for the life of the process.
            let (u, clock, r) = run(baseline: "0.16.1", thenVersions: [], versionHangs: true)
            clock.advance(by: 200)
            Harness.expect(clock.liveTimerCount, 0, "timer still running past the deadline")
            Harness.expect(r.statuses.last?.contains("did not change") == true,
                           "expected a give-up status, got \(r.statuses.last ?? "none")")
            expectRetryable(u, r, "reaching the deadline")

            // The parked read now answers, after the timer was cancelled. In
            // production that is a `quern --version` started just before the
            // deadline returning just after it. Without the liveness check the
            // updater relaunches seconds after saying nothing had changed.
            let parked = r.parked
            r.parked = []
            for answer in parked { answer("0.17.0", "quern 0.17.0") }
            Harness.expect(r.relaunchedInto == nil,
                           "a read that landed after the deadline must not relaunch")
        }

        Harness.test("one outstanding version check at a time") {
            // Without the skip every 2s tick spawns another subprocess for the
            // whole 180s, against a venv being rebuilt -- 90 of them.
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: [], versionHangs: true)
            clock.advance(by: 20)
            Harness.expect(r.versionCalls, 2,
                           "expected the baseline read plus one outstanding poll, "
                               + "got \(r.versionCalls)")
        }

        Harness.test("an unchanged version is not reported as a timeout") {
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.16.1", "0.16.1"])
            clock.advance(by: 60)
            Harness.expect(r.relaunchedInto == nil, "nothing changed, so nothing to relaunch into")
            Harness.expect(r.statuses.last?.contains("timed out") == false,
                           "`quern update` exited 0; calling that a timeout reports a "
                               + "failure for something that worked")
        }

        Harness.test("a failed update stops and reports") {
            let (u, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.17.0"],
                                    updateResult: (1, "git pull failed"))
            clock.advance(by: 10)
            Harness.expect(r.relaunchedInto == nil, "must not relaunch after a failed update")
            Harness.expect(r.failures.count, 1, "failure reports")
            Harness.expect(clock.liveTimerCount, 0, "no poll should have started")
            expectRetryable(u, r, "a failed update")
        }

        Harness.test("progress is reported as working until there is an answer") {
            // The menu closes the instant you click an item, so a status that
            // only reaches a menu line is invisible for the whole run. The
            // status item shows `working`, and stops the moment there is an
            // answer so the menu bar is not permanently wider.
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.16.1", "0.16.1"])
            Harness.expect(r.progress.first?.isWorking == true,
                           "the first thing reported must be that work started")
            clock.advance(by: 60)
            Harness.expect(r.progress.last?.isWorking == false,
                           "running out of time is an answer, not work in progress")
        }

        Harness.test("a failure is an answer, not continuing work") {
            let (_, clock, r) = run(baseline: "0.16.1", thenVersions: ["0.17.0"],
                                    updateResult: (1, "git pull failed"))
            clock.advance(by: 10)
            Harness.expect(r.progress.last?.isWorking == false,
                           "a failed update must not leave the icon saying it is working")
        }

        Harness.test("an unreadable baseline stops reporting work immediately") {
            let (_, _, r) = run(baseline: nil, thenVersions: [])
            Harness.expect(r.progress.last?.isWorking == false, "reading")
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
            let go = { updater.restartToUpdate(status: {
                record.progress.append($0); record.statuses.append($0.text) },
                                               failure: { record.failures.append(($0, $1)) }) }
            go()
            let afterFirst = record.versionCalls
            go()
            Harness.expect(record.versionCalls, afterFirst,
                           "a second click started a second update")
        }
    }
}
