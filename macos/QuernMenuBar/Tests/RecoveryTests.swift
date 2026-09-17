// A Terminal button for every failure worth recovering from (#225).

import Foundation

enum RecoveryTests {
    static let root = URL(fileURLWithPath: "/Users/u/.local/share/quern", isDirectory: true)

    static func lines(_ script: String) -> [String] {
        script.split(separator: "\n").map(String.init)
    }

    static func script(_ r: Recovery, quern: String?, releaseWrapperExists: Bool = false) -> String {
        r.script(quern: quern, releaseRoot: root, log: "/Users/u/.quern/server.log",
                 exists: { _ in releaseWrapperExists })
    }

    static func all() {
        Harness.test("repair runs doctor --fix, then start, with the wrapper by path") {
            let l = lines(script(.repair, quern: "/Users/u/.local/bin/quern"))
            let doctor = l.firstIndex(of: "'/Users/u/.local/bin/quern' doctor --fix")
            let start = l.firstIndex(of: "'/Users/u/.local/bin/quern' start")
            Harness.expect(doctor != nil && start != nil, "both commands: \(l)")
            Harness.expect((doctor ?? 0) < (start ?? 0), "doctor before start")
        }

        Harness.test("finishing an update runs setup, then restart") {
            let l = lines(script(.finishUpdate, quern: "/q"))
            let setup = l.firstIndex(of: "'/q' setup")
            let restart = l.firstIndex(of: "'/q' restart")
            Harness.expect(setup != nil && restart != nil, "both commands: \(l)")
            Harness.expect((setup ?? 0) < (restart ?? 0), "setup before restart")
        }

        Harness.test("every recovery leaves the window open and names the log") {
            for r in [Recovery.repair, .finishUpdate, .setUp] {
                let text = script(r, quern: "/q")
                Harness.expect(lines(text).last, "exec \"${SHELL:-/bin/zsh}\" -l", "\(r) ends in a shell")
                Harness.expect(text.contains("/Users/u/.quern/server.log"), "\(r) names the log")
                Harness.expect(text.hasPrefix("#!/bin/sh\n"), "\(r) shebang")
            }
        }

        Harness.test("set-up runs the release install's own wrapper when there is one") {
            let l = lines(script(.setUp, quern: nil, releaseWrapperExists: true))
            Harness.expect(l.contains("'/Users/u/.local/share/quern/quern' setup"), "runs it: \(l)")
        }

        Harness.test("with no install at all, the installer is shown and not run") {
            let l = lines(script(.setUp, quern: nil, releaseWrapperExists: false))
            Harness.expect(l.contains { $0.contains(Recovery.installOneLiner) }, "shown")
            Harness.expect(!l.contains { $0.hasPrefix("curl") }, "never executed")
        }

        Harness.test("a missing wrapper falls back to quern on PATH") {
            Harness.expect(lines(script(.repair, quern: nil)).contains("'quern' start"), "fallback")
        }

        Harness.test("every failure alert with a recovery offers it second") {
            let with = FailureAlert.buttons(detail: "boom", hasLog: true, recovery: .repair)
            Harness.expect(with, [.ok, .fixInTerminal(.repair), .copy, .openLog], "full set")
            Harness.expect(FailureAlert.buttons(detail: "", hasLog: false, recovery: .setUp),
                           [.ok, .fixInTerminal(.setUp)], "minimal")
            Harness.expect(FailureAlert.buttons(detail: "boom", hasLog: false, recovery: nil),
                           [.ok, .copy], "no recovery, no button")
            Harness.expect(FailureAlertButton.fixInTerminal(.repair).title, "Fix in Terminal", "title")
        }

        Harness.test("a failed start right after an update interrupts; a login start does not") {
            let now = Date(timeIntervalSince1970: 1_000_000)
            func record(_ outcome: UpdateResult.Outcome, secondsAgo: TimeInterval?) -> UpdateResult {
                UpdateResult(outcome: outcome, detail: "", version: "0.18.5",
                             finishedAt: secondsAgo.map { now.addingTimeInterval(-$0) })
            }
            func report(_ r: UpdateResult?) -> LifecycleController.Report {
                FailureReporting.forLaunchStart(lastUpdate: r, now: now)
            }
            Harness.expect(report(record(.updated, secondsAgo: 30)), .alert, "just updated")
            Harness.expect(report(record(.failed, secondsAgo: 30)), .alert, "update just failed")
            Harness.expect(report(record(.updated, secondsAgo: 3600)), .menuOnly, "an old update")
            Harness.expect(report(record(.noOp, secondsAgo: 30)), .menuOnly, "nothing was updated")
            Harness.expect(report(record(.updated, secondsAgo: nil)), .menuOnly, "no timestamp")
            Harness.expect(report(record(.updated, secondsAgo: -600)), .menuOnly, "a future timestamp")
            Harness.expect(report(nil), .menuOnly, "no record")
        }
    }
}
