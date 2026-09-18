// A Terminal button for every failure worth recovering from (#225).

import AppKit
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
            // The whole body, not a prefix: `!hasPrefix("curl")` was satisfied
            // by `sh -c 'curl … | bash'`, ` curl …`, `eval "$(curl …)"` and
            // `bash <(curl …)` alike -- a review mutation added exactly that
            // and the suite stayed green. Nothing but `echo` may mention the
            // installer.
            let l = lines(script(.setUp, quern: nil, releaseWrapperExists: false))
            Harness.expect(l.contains { $0.contains(Recovery.installOneLiner) }, "shown")
            let executable = l.filter { line in
                let t = line.trimmingCharacters(in: .whitespaces)
                return !t.isEmpty && !t.hasPrefix("#") && !t.hasPrefix("echo ") && t != "echo"
                    && t != "clear" && !t.hasPrefix("exec ")
            }
            Harness.expect(executable, [], "nothing is run in this case: \(executable)")
            Harness.expect(!l.contains { $0.contains("install.sh") && !$0.hasPrefix("echo ") },
                           "install.sh appears only inside an echo")
        }

        Harness.test("the log path is quoted, whatever the home directory is") {
            // Every other interpolation went through shellQuote; this one sat
            // inside a double-quoted word, where " ` and $ are live.
            let hostile = "/Volumes/Home/o\"brien; touch /tmp/pwned; #/.quern/server.log"
            let text = Recovery.repair.script(quern: "/q", releaseRoot: root, log: hostile,
                                              exists: { _ in false })
            // Pinned exactly: the whole path must sit inside one quoted word,
            // so the shell sees it as text rather than as more commands.
            let logLine = lines(text).first { $0.contains("server.log") } ?? ""
            Harness.expect(logLine,
                           "echo " + TerminalScript.shellQuote("The server's log is \(hostile)"),
                           "quoting")
            for metachar in ["`id`", "$(id)", "$HOME", "\"", "\\"] {
                let path = "/h/\(metachar)/server.log"
                let t = Recovery.repair.script(quern: "/q", releaseRoot: root, log: path,
                                               exists: { _ in false })
                let line = lines(t).first { $0.contains("server.log") } ?? ""
                Harness.expect(line, "echo " + TerminalScript.shellQuote("The server's log is \(path)"),
                               "\(metachar) quoted")
            }
        }

        Harness.test("an update that never started is repaired, not finished") {
            // "Could not start the update" means the installed version could
            // not be read: nothing ran, so setup + restart would bounce a
            // server that is probably healthy.
            Harness.expect(Recovery.forUpdateFailure(started: false), .repair, "never started")
            Harness.expect(Recovery.forUpdateFailure(started: true), .finishUpdate, "stopped partway")
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
            // Just past the window: the only "too old" case was an hour, so a
            // ten-times-wider window passed the suite -- an hour of unwanted
            // login modals, which is what this function exists to prevent.
            Harness.expect(report(record(.updated,
                                         secondsAgo: FailureReporting.afterUpdateWindow + 60)),
                           .menuOnly, "just past the window")
            Harness.expect(report(record(.updated,
                                         secondsAgo: FailureReporting.afterUpdateWindow - 60)),
                           .alert, "just inside the window")
            Harness.expect(report(record(.noOp, secondsAgo: 30)), .menuOnly, "nothing was updated")
            Harness.expect(report(record(.updated, secondsAgo: nil)), .menuOnly, "no timestamp")
            Harness.expect(report(record(.updated, secondsAgo: -600)), .menuOnly, "a future timestamp")
            Harness.expect(report(nil), .menuOnly, "no record")
        }

        Harness.test("each way out is named for what it does") {
            // Two of these can be on the menu at once -- an update that
            // stopped partway, and a start that then failed. Identical
            // titles a few rows apart, running different scripts, is a coin
            // flip.
            let titles = [Recovery.finishUpdate, .repair, .setUp].map(\.menuTitle)
            Harness.expect(Set(titles).count, titles.count, "all distinct")
            Harness.expect(Recovery.finishUpdate.menuTitle, "Finish Update in Terminal…", "the update's")
        }

        Harness.test("the item carries the recovery it runs") {
            // It rides on the item because one shared property was being
            // overwritten by whichever row was built second.
            let target = NSObject()
            let items = [Recovery.finishUpdate, .repair].map {
                $0.menuItem(target: target, action: #selector(NSObject.self.description as () -> String))
            }
            Harness.expect(items[0].representedObject as? Recovery, .finishUpdate, "first")
            Harness.expect(items[1].representedObject as? Recovery, .repair, "second")
            Harness.expect(items[0].title, Recovery.finishUpdate.menuTitle, "title")
        }

        Harness.test("an update's way out is drawn whether or not the server is up") {
            // The bug: it was drawn only inside the server-is-down branch,
            // and an update that stops partway usually leaves the server
            // running -- so the only route back was absent in the common
            // case. Nothing reaches the menu builder, so this is the seam
            // that pins it.
            for running in [true, false] {
                let rows = FailureMenu.rows(
                    serverRunning: running, statusText: nil,
                    startRecovery: nil, updateRecovery: .finishUpdate,
                    hasFailed: false, logExists: false)
                Harness.expect(rows, [.recovery(.finishUpdate)], "running=\(running)")
            }
        }

        Harness.test("a failed start's way out is drawn only while the server is down") {
            let up = FailureMenu.rows(
                serverRunning: true, statusText: "Start failed",
                startRecovery: .repair, updateRecovery: nil,
                hasFailed: true, logExists: true)
            Harness.expect(up, [], "a running server has no start failure to report")
            let down = FailureMenu.rows(
                serverRunning: false, statusText: "Start failed",
                startRecovery: .repair, updateRecovery: nil,
                hasFailed: true, logExists: true)
            Harness.expect(down, [.status("Start failed"), .recovery(.repair), .serverLog], "down")
        }

        Harness.test("both ways out can be on the menu, update first") {
            let rows = FailureMenu.rows(
                serverRunning: false, statusText: "Start failed",
                startRecovery: .repair, updateRecovery: .finishUpdate,
                hasFailed: false, logExists: true)
            Harness.expect(rows, [.recovery(.finishUpdate), .status("Start failed"),
                                  .recovery(.repair)], "order, and no log without a failure")
        }

        Harness.test("nothing to report draws nothing") {
            Harness.expect(
                FailureMenu.rows(serverRunning: false, statusText: nil,
                                 startRecovery: .repair, updateRecovery: nil,
                                 hasFailed: true, logExists: true),
                [], "no status means no unresolved failure to explain")
        }

        Harness.test("the server log needs a failure and a file") {
            func rows(hasFailed: Bool, logExists: Bool) -> [FailureRow] {
                FailureMenu.rows(serverRunning: false, statusText: "Start failed",
                                 startRecovery: nil, updateRecovery: nil,
                                 hasFailed: hasFailed, logExists: logExists)
            }
            Harness.expect(rows(hasFailed: true, logExists: false).contains(.serverLog), false, "no file")
            Harness.expect(rows(hasFailed: false, logExists: true).contains(.serverLog), false, "no failure")
            Harness.expect(rows(hasFailed: true, logExists: true).contains(.serverLog), true, "both")
        }
    }
}
