// A one-click way into recovery when the server does not come back (#225).
//
// Always a button, never Terminal opening by itself: a window appearing
// unprompted -- at login, where start-on-launch also runs -- is worse than the
// failure it would be answering. Each recovery is a short, fixed script run in
// a Terminal window the user asked for and can watch.

import AppKit
import Foundation

/// What the menu draws between the header and the lifecycle actions.
///
/// A plain value so it can be tested. The bug this exists to prevent lived in
/// the menu builder -- the update's way out was drawn only inside the
/// server-is-down branch, and an update that stops partway usually leaves the
/// server *up*, so the only route back was absent in the common case. Nothing
/// reaches that builder: `AppDelegate` cannot be constructed without a status
/// item, so every mutation of it shipped green.
enum FailureRow: Equatable {
    case status(String)
    case recovery(Recovery)
    case serverLog
}

enum FailureMenu {
    /// - Parameters:
    ///   - serverRunning: whether the daemon is up *right now*.
    ///   - statusText: the last lifecycle message, if one is unresolved.
    ///   - startRecovery: the way out of a failed start.
    ///   - updateRecovery: the way out of an update that stopped partway.
    ///   - hasFailed: whether a lifecycle action failed, which is what makes
    ///     the server log worth offering.
    ///   - logExists: whether there is a log file to open.
    static func rows(serverRunning: Bool, statusText: String?,
                     startRecovery: Recovery?, updateRecovery: Recovery?,
                     hasFailed: Bool, logExists: Bool) -> [FailureRow]
    {
        var rows: [FailureRow] = []
        // Deliberately not gated on `serverRunning`. See the type's note.
        if let updateRecovery { rows.append(.recovery(updateRecovery)) }
        guard !serverRunning, let statusText else { return rows }
        rows.append(.status(statusText))
        if let startRecovery { rows.append(.recovery(startRecovery)) }
        if hasFailed, logExists { rows.append(.serverLog) }
        return rows
    }
}

enum Recovery: Equatable {
    /// `quern update` stopped partway: setup, then restart (#212).
    case finishUpdate
    /// The server would not start: `doctor --fix`, then start.
    case repair
    /// There is no `quern` to run.
    case setUp

    var buttonTitle: String { "Fix in Terminal" }

    /// One line saying what the button will do, for the alert's own text.
    ///
    /// The alert used to put the whole command output where this goes, which
    /// is how its buttons ended up off-screen (#339). The output is worth
    /// keeping, but it is not what the reader needs first -- the next action
    /// is.
    var explanation: String {
        switch self {
        case .finishUpdate:
            return "The update stopped partway. Opening Terminal will run setup and restart the server."
        case .repair:
            return "The server would not start. Opening Terminal will run doctor and try again."
        case .setUp:
            return "There is no quern command to run. Opening Terminal will install it."
        }
    }

    /// Distinct per case, because two of these can be on the menu at once: an
    /// update that stopped partway leaves the server up or down, and a start
    /// that then fails records its own. Two rows reading "Troubleshoot in
    /// Terminal…" a few lines apart, running different scripts, is a coin
    /// flip for the user.
    var menuTitle: String {
        switch self {
        case .finishUpdate: return "Finish Update in Terminal…"
        case .repair: return "Troubleshoot in Terminal…"
        case .setUp: return "Set Up in Terminal…"
        }
    }

    /// The menu item, carrying the recovery itself.
    ///
    /// It rides on the item rather than in one property on the delegate
    /// because both rows can be present at once and the second writer was
    /// overwriting the first. Captured as the menu is built: reading the
    /// controller again on click let the three-second state poll clear it
    /// while the menu was open, and the click then did nothing at all.
    func menuItem(target: AnyObject, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: menuTitle, action: action, keyEquivalent: "")
        item.target = target
        item.representedObject = self
        return item
    }

    /// The `.command` body. `quern` is the resolved wrapper, if there is one.
    static let installOneLiner = "curl -fsSL https://quern.dev/install.sh | bash"

    func script(quern: String?, releaseRoot: URL = QuernCLI.releaseInstallDir,
                log: String = StateReader.quernDir.appendingPathComponent("server.log").path,
                exists: (String) -> Bool = { FileManager.default.isExecutableFile(atPath: $0) })
        -> String
    {
        var body: [String] = []
        switch self {
        case .finishUpdate:
            body.append("echo \"The update did not finish. Running setup, then restarting the server.\"")
            body.append("echo")
            body += Self.runQuern(quern, "setup")
            body += Self.runQuern(quern, "restart")
        case .repair:
            body.append("echo \"The server did not start. Checking the install and repairing what can be.\"")
            body.append("echo")
            body += Self.runQuern(quern, "doctor", "--fix")
            body += Self.runQuern(quern, "start")
        case .setUp:
            let releaseWrapper = releaseRoot.appendingPathComponent("quern").path
            if exists(releaseWrapper) {
                body.append("echo \"The quern command is missing. Running setup from the install.\"")
                body.append("echo")
                body.append("\(TerminalScript.shellQuote(releaseWrapper)) setup")
            } else {
                // Printed, not run: fetching and running an installer is a
                // bigger step than a click on an alert should take.
                body.append("echo \"The quern command was not found. To install or repair Quern, run:\"")
                body.append("echo")
                body.append("echo \"    \(Self.installOneLiner)\"")
            }
        }
        body.append("echo")
        // Quoted like every other interpolation. This lands inside a
        // double-quoted word, where " ` $ and \ are all live, and a home
        // directory can legitimately contain them -- a network home under
        // /Volumes/..., or an apostrophe in a name.
        body.append("echo " + TerminalScript.shellQuote("The server's log is \(log)"))
        return TerminalScript.wrap(title: "Quern recovery", body: body)
    }

    private static func runQuern(_ quern: String?, _ args: String...) -> [String] {
        let command = ([TerminalScript.shellQuote(quern ?? "quern")] + args).joined(separator: " ")
        return [
            "echo \"\\$ quern \(args.joined(separator: " "))\"",
            command,
            "echo",
        ]
    }

    /// Write the script and open it in Terminal.
    func open(completion: @escaping (String?) -> Void) {
        TerminalScript.open(name: "quern-recovery.command",
                            contents: script(quern: QuernCLI.resolve()?.path),
                            completion: completion)
    }
}

/// Running a short script in a new Terminal window.
///
/// A `.command` file opened with Terminal rather than AppleScript: scripting
/// Terminal from a hardened-runtime app needs the apple-events entitlement, a
/// usage string, and a permission prompt the user has to accept, and until
/// they do it fails with -1743. Opening a document needs none of that.
enum TerminalScript {
    static let terminalApp = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")

    static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// A complete script: header, `body`, then the window becomes the user's
    /// own login shell. Terminal's default profile closes a window whose shell
    /// exits cleanly, which hid a successful run's output before anyone could
    /// read it; a shell keeps it on screen and leaves somewhere to type next.
    static func wrap(title: String, body: [String]) -> String {
        // The title lands in a comment, so anything that could end that line
        // could add a command. Constants today; cheap to make it not matter.
        let safe = title.replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
        return (["#!/bin/sh",
          "# Written by the Quern app (\(safe)). Safe to delete.",
          "clear"]
         + body
         + ["echo \"This window is now an ordinary shell; close it when you're finished.\"",
            "exec \"${SHELL:-/bin/zsh}\" -l",
            ""])
            .joined(separator: "\n")
    }

    static func open(name: String, contents: String,
                     completion: @escaping (String?) -> Void) {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        do {
            try contents.write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700],
                                                  ofItemAtPath: file.path)
        } catch {
            completion("Could not write \(file.path): \(error.localizedDescription)")
            return
        }
        NSWorkspace.shared.open([file], withApplicationAt: terminalApp,
                                configuration: NSWorkspace.OpenConfiguration()) { _, error in
            DispatchQueue.main.async {
                completion(error.map { "Terminal did not open: \($0.localizedDescription)" })
            }
        }
    }
}

extension Recovery {
    /// Which recovery an update failure deserves.
    ///
    /// `Updater` reports two different things through one channel: an update
    /// that ran and stopped partway, and one that never started because the
    /// installed version could not be read. Only the first is finished by
    /// `setup` + `restart`; offering that for the second restarts a server
    /// that is probably healthy.
    static func forUpdateFailure(started: Bool) -> Recovery {
        started ? .finishUpdate : .repair
    }
}

/// Which failures interrupt with an alert.
enum FailureReporting {
    /// How long after a recorded update a failed start still counts as the
    /// update's failure, and so deserves an alert rather than only a red icon.
    static let afterUpdateWindow: TimeInterval = 5 * 60

    /// Start-on-launch is quiet by default -- it runs at login, and a modal as
    /// the laptop opens is worse than the failure. Right after an update the
    /// user is watching, having just clicked Update, so it is loud.
    static func forLaunchStart(lastUpdate: UpdateResult?, now: Date) -> LifecycleController.Report {
        guard let lastUpdate, lastUpdate.outcome != .noOp,
              let finished = lastUpdate.finishedAt,
              now.timeIntervalSince(finished) >= 0,
              now.timeIntervalSince(finished) <= afterUpdateWindow
        else { return .menuOnly }
        return .alert
    }
}

/// The buttons on a failure alert, in order. Separate from `NSAlert` so the
/// choice can be tested.
enum FailureAlertButton: Equatable {
    case ok
    case fixInTerminal(Recovery)
    case copy
    case openLog

    var title: String {
        switch self {
        case .ok: return "OK"
        case .fixInTerminal(let recovery): return recovery.buttonTitle
        case .copy: return "Copy"
        case .openLog: return "Open Log"
        }
    }
}

enum FailureAlert {
    /// Fix in Terminal comes **first** when there is one, because NSAlert makes
    /// the first button the default and Return triggers it.
    ///
    /// It used to come second, after OK. That was defensible while the alert
    /// was readable; it was not once the alert could grow past the screen. A
    /// user met a window whose buttons were all below the bottom edge, and the
    /// only thing reachable was Return -- which discarded the error, because OK
    /// was the default. The remedy was on screen in spirit and unreachable in
    /// fact (#339). Whichever way the height bug is fixed, the key that always
    /// works should do the useful thing.
    ///
    /// Running a command on Return is safe here: the scripts are fixed quern
    /// commands, they open a Terminal window the user watches, and any password
    /// prompt appears there in front of them.
    static func buttons(detail: String, hasLog: Bool, recovery: Recovery?) -> [FailureAlertButton] {
        var buttons: [FailureAlertButton] = []
        if let recovery { buttons.append(.fixInTerminal(recovery)) }
        buttons.append(.ok)
        if !detail.isEmpty { buttons.append(.copy) }
        if hasLog { buttons.append(.openLog) }
        return buttons
    }

    /// What the alert says in its own body -- never the command output.
    ///
    /// `NSAlert.informativeText` does not scroll and cannot be bounded, so it
    /// sets the window's height. Given an install log it produced a window
    /// taller than the display, with its buttons off the bottom edge and no way
    /// to resize or scroll (#339). The output now goes in a scrollable
    /// accessory view; this is the short part that must always be visible, so
    /// it leads with the next action rather than ending with it.
    static func text(guidance: String?, hasDetail: Bool, recovery: Recovery?) -> String {
        var parts: [String] = []
        let trimmed = guidance?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !trimmed.isEmpty {
            parts.append(trimmed)
        } else if let recovery {
            parts.append(recovery.explanation)
        } else if !hasDetail {
            // Nothing to show and nothing to offer: say what would tell them.
            parts.append("Run `quern status` to see what state it is in.")
        }
        if hasDetail { parts.append("The full output is below.") }
        return parts.joined(separator: "\n\n")
    }
}
