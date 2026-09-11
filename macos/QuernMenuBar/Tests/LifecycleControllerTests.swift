// The lifecycle state machine, in simulated time.
//
// Every case here is a defect review found in #136, and none was testable
// before: the logic lived in AppDelegate, which cannot be built without a
// status bar, and the grace window is fifteen seconds of real time.

import Foundation

enum LifecycleControllerTests {
    private final class Rig {
        let clock = TestScheduler()
        var running = false
        var alerts: [(String, String)] = []
        var changes = 0
        var refreshes = 0
        var invoked: [String] = []
        var controller: LifecycleController!

        /// Completions for calls that have not answered yet, so a test can
        /// decide when — or whether — the CLI comes back.
        var pending: [(Int32, String) -> Void] = []

        init(result: (Int32, String)?, serverAppearsAfter: Int? = nil) {
            var deps = LifecycleController.Dependencies()
            deps.scheduler = clock
            deps.log = { _ in }
            deps.refreshState = { [unowned self] in self.refreshes += 1 }
            var looks = 0
            deps.serverIsRunning = { [unowned self] in
                looks += 1
                if let after = serverAppearsAfter, looks > after { self.running = true }
                return self.running
            }
            let make: (String) -> (@escaping (Int32, String) -> Void) -> Void = { name in
                { [unowned self] done in
                    self.invoked.append(name)
                    if let result { done(result.0, result.1) } else { self.pending.append(done) }
                }
            }
            deps.start = make("start")
            deps.stop = make("stop")
            deps.restart = make("restart")

            controller = LifecycleController(deps)
            controller.onChange = { [unowned self] in self.changes += 1 }
            controller.onAlert = { [unowned self] in self.alerts.append(($0, $1)) }
        }
    }

    static func all() {
        Harness.test("a clean start clears the status and the busy flag") {
            let rig = Rig(result: (0, ""))
            rig.controller.run(.start, reporting: .alert)
            Harness.expect(rig.controller.isBusy, false, "busy after success")
            Harness.expect(rig.controller.statusText == nil, "status should be cleared")
            Harness.expect(rig.controller.hasFailed, false, "failed after success")
            // onChange is the only thing that repaints the icon. Without this
            // assertion every `changed()` call could be deleted and the suite
            // stayed green -- measured.
            Harness.expect(rig.changes >= 2, "expected a change on start and on finish, "
                + "got \(rig.changes)")
            Harness.expect(rig.refreshes >= 1, "on-disk state must be re-read when a "
                + "lifecycle action returns")
        }

        Harness.test("the menu is told something is happening before the CLI returns") {
            // The click used to produce no visible effect for thirty seconds,
            // so the item read as dead and clicking again spawned a second
            // `quern start` racing the first for the port.
            let rig = Rig(result: nil)  // never answers
            rig.controller.run(.start, reporting: .alert)
            Harness.expect(rig.controller.isBusy, true, "busy while the CLI runs")
            Harness.expect(rig.controller.statusText, "Starting…", "status while running")
        }

        Harness.test("busy is held through the grace window, not dropped at the exit") {
            // The #136 finding: cleared when the CLI returned, so for fifteen
            // seconds the menu showed "Starting…" beside an enabled "Start
            // Server" — offering the second click the flag exists to prevent.
            let rig = Rig(result: (1, "health check timed out"))
            rig.controller.run(.start, reporting: .alert)
            Harness.expect(rig.controller.isBusy, true, "must stay busy while confirming")
            rig.clock.advance(by: 3)
            Harness.expect(rig.controller.isBusy, true, "still confirming")
            Harness.expect(rig.alerts.isEmpty, "must not report before the window closes")
        }

        Harness.test("a server that turns up during the window is not a failure") {
            // `quern start` gives up after 30s and leaves its child running, so
            // a nonzero exit does not mean no server. Alerting immediately put
            // "Could not start" in front of a menu that said it was running.
            let rig = Rig(result: (1, "health check timed out"), serverAppearsAfter: 2)
            rig.controller.run(.start, reporting: .alert)
            rig.clock.advance(by: 20)
            Harness.expect(rig.alerts.isEmpty, "the server came up; nothing failed")
            Harness.expect(rig.controller.hasFailed, false, "must not mark failed")
            Harness.expect(rig.controller.isBusy, false, "must not stay busy")
            Harness.expect(rig.changes >= 2, "the icon must be repainted when the wait ends")
            Harness.expect(rig.controller.statusText == nil, "status should be cleared")
        }

        Harness.test("a server that never turns up is reported once") {
            let rig = Rig(result: (1, "health check timed out"))
            rig.controller.run(.start, reporting: .alert)
            rig.clock.advance(by: 60)
            Harness.expect(rig.alerts.count, 1, "alerts raised")
            Harness.expect(rig.controller.hasFailed, true, "failed flag drives the red icon")
            Harness.expect(rig.controller.isBusy, false, "busy must clear at the outcome")
            Harness.expect(rig.controller.statusText, "Could not start the server", "status")
        }

        Harness.test("a login start reports to the menu and never to a modal") {
            let rig = Rig(result: (1, "health check timed out"))
            rig.controller.run(.start, reporting: .menuOnly)
            rig.clock.advance(by: 60)
            Harness.expect(rig.alerts.isEmpty, "a modal must not steal focus at login")
            Harness.expect(rig.controller.hasFailed, true, "but the menu must still say so")
        }

        Harness.test("a missing CLI skips the wait and names itself") {
            // Nothing ran, so nothing will change on its own — and "see
            // Console" is the wrong answer for a setup step never run.
            let rig = Rig(result: (QuernCLI.notFoundStatus, "no wrapper"))
            rig.controller.run(.start, reporting: .alert)
            Harness.expect(rig.controller.isBusy, false, "must not wait for a CLI that is absent")
            Harness.expect(rig.controller.statusText, "quern not found — run `quern setup`", "status")
            // Nothing ran, so there is nothing in the server log about it and
            // no reason to turn the icon red. hasFailed drives both.
            Harness.expect(rig.controller.hasFailed, false,
                           "a command that never ran must not offer the server log")
        }

        Harness.test("a failed stop is not waited on") {
            let rig = Rig(result: (1, "nope"))
            rig.controller.run(.stop, reporting: .alert)
            Harness.expect(rig.controller.isBusy, false, "nothing to wait for")
            Harness.expect(rig.alerts.count, 1, "reported immediately")
            // The daemon is still up, so the icon must not go red: the status
            // line is only rendered when it is down, which left the tooltip
            // pointing at a menu with nothing in it.
            Harness.expect(rig.controller.hasFailed, false,
                           "a failed stop must not redden the icon of a running server")
        }

        Harness.test("a second action while one is running is ignored") {
            let rig = Rig(result: nil)  // first call never answers
            rig.controller.run(.start, reporting: .alert)
            rig.controller.run(.restart, reporting: .alert)
            Harness.expect(rig.invoked, ["start"], "a second CLI call was made")
        }

        Harness.test("a restart that never brings the daemon back is reported") {
            // The only action with no failure coverage before, and the one
            // where "the server is up" and "the action worked" can differ.
            let rig = Rig(result: (1, "restart failed"))
            rig.controller.run(.restart, reporting: .alert)
            rig.clock.advance(by: 60)
            Harness.expect(rig.invoked, ["restart"], "the restart CLI call")
            Harness.expect(rig.alerts.count, 1, "alerts raised")
            Harness.expect(rig.controller.hasFailed, true, "a start that did not happen is a failure")
        }

        Harness.test("the daemon coming up elsewhere clears a stale failure") {
            let rig = Rig(result: (1, "health check timed out"))
            rig.controller.run(.start, reporting: .menuOnly)
            rig.clock.advance(by: 60)
            Harness.expect(rig.controller.hasFailed, true, "precondition")
            let before = rig.changes
            rig.controller.noteServerRunning()
            Harness.expect(rig.controller.hasFailed, false, "stale failure survived")
            Harness.expect(rig.controller.statusText == nil, "stale status survived")
            Harness.expect(rig.changes >= before + 1, "clearing a stale failure must repaint")
        }
    }
}
