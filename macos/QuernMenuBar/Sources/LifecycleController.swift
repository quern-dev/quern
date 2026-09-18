// What is happening to the daemon, as distinct from how it is drawn.
//
// Extracted from AppDelegate, which cannot be constructed without an
// NSStatusItem -- which needs a window server, which a CI runner may not have.
// That put this state machine permanently out of reach of a test, and review
// found three separate defects in it: a flag cleared before the outcome was
// known, so the menu offered a second `quern start` while the first was still
// being waited on; a failure reported before the daemon had finished coming up,
// contradicted moments later by a menu saying it was running; and a "see
// Console" for a case where the real answer was that setup had never run.
//
// AppDelegate now renders this and owns none of it.

import Foundation

final class LifecycleController {
    enum Action: String {
        case start, stop, restart

        /// What the menu says while it runs.
        var present: String { self == .stop ? "Stopping…" : "Starting…" }
    }

    /// How a failure reaches the user.
    ///
    /// A click someone is waiting on gets a modal. The automatic start at login
    /// gets a menu line only: that can fire as you open your laptop, and a
    /// modal taking focus then is worse than the failure it reports.
    enum Report { case alert, menuOnly }

    struct Dependencies {
        var scheduler: Scheduler = SystemScheduler()
        /// Re-read on-disk state. Called the moment any action returns, whether
        /// it worked or not: a `quern stop` that succeeded has already deleted
        /// state.json, and without this the menu kept offering Stop until the
        /// next poll came round.
        var refreshState: () -> Void = {}
        /// Whether the daemon is up, as of the last refresh.
        var serverIsRunning: () -> Bool = { false }
        var start: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.start($0) }
        var stop: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.stop($0) }
        var restart: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.restart($0) }
        var log: (String) -> Void = { Log.lifecycle.notice("\($0, privacy: .public)") }
    }

    /// How long to keep waiting after a nonzero exit, and how often to look.
    ///
    /// `quern start` gives up after 30s and leaves its child running on
    /// purpose, so a nonzero exit does not mean no server -- the daemon can
    /// appear moments later. This covers the tail of a slow startup rather
    /// than repeating the whole of it.
    private let graceAttempts = 10
    private let graceInterval = 1.5

    private let deps: Dependencies

    /// True from the moment an action is invoked until its outcome is known,
    /// which is not the same as until the CLI returns. Clearing it at the
    /// return put an enabled "Start Server" in the same menu as "Starting…"
    /// for the whole grace window.
    private(set) var isBusy = false
    /// A start was attempted and the daemon did not come up.
    ///
    /// Narrower than "something went wrong", and the narrowness is the point:
    /// this is what turns the icon red, sets the tooltip to "Quern could not
    /// start", and offers **Open Server Log**. Setting it for a failed *stop*
    /// reddened the icon of a server that was still running and pointed the
    /// tooltip at a menu that had nothing to show, because the status line is
    /// only rendered when the daemon is down. Setting it for a missing CLI
    /// offered a server log for a command that never ran, which is the dead
    /// end this file exists to remove.
    private(set) var hasFailed = false
    private(set) var statusText: String?
    /// What to offer when the last *start* failed in a way the user can act on
    /// from Terminal (#225). Cleared with the failure it describes.
    private(set) var recovery: Recovery?

    /// The same, for an update that stopped partway -- kept apart because it
    /// outlives the condition `recovery` is tied to. An update can fail with
    /// the server still running happily, and then the menu's start-failure
    /// section is not drawn at all and `noteServerRunning()` would clear it.
    /// The way out has to survive both.
    private(set) var updateRecovery: Recovery?

    /// The newest version the server has reported, and the one it was
    /// reporting when the update failed. A version that has moved since is
    /// proof the update landed -- by the Terminal recovery, by `quern update`
    /// in the user's own shell, by any route.
    ///
    /// Something has to retire `updateRecovery`, and the two obvious
    /// candidates do not: it deliberately survives `noteServerRunning()`, and
    /// the retry items that call `clearUpdateRecovery()` are drawn only while
    /// an update is still staged. Without this, finishing the update left a
    /// bare "Finish Update in Terminal…" on the menu of a healthy, current
    /// server for the life of the process.
    ///
    /// The last *known* version is kept rather than the latest reading,
    /// because the reading goes nil while the install is being replaced --
    /// exactly when an update fails.
    ///
    /// The baseline is written with `updateRecovery` and read only while it is
    /// set, so there is nothing to clear alongside it.
    private var lastKnownVersion: String?
    private var versionAtUpdateFailure: String?

    /// Called whenever any of the three above change, so the icon can repaint.
    var onChange: (() -> Void)?
    /// Title, detail, and the recovery to offer, for a modal.
    var onAlert: ((String, String, Recovery?) -> Void)?

    init(_ deps: Dependencies = Dependencies()) {
        self.deps = deps
    }

    /// A failure that did not come from `run` -- an update that stopped partway
    /// -- so the menu keeps offering its recovery after the alert is gone.
    ///
    /// Without this the update dead-end survived the alert: OK left `recovery`
    /// nil and the menu had nothing, in the case this feature exists for.
    func noteFailure(status: String, recovery: Recovery) {
        statusText = status
        updateRecovery = recovery
        versionAtUpdateFailure = lastKnownVersion
        changed()
    }

    /// The version the server reports, on every state poll. See
    /// `versionAtUpdateFailure`.
    func noteServerVersion(_ version: String?) {
        guard let version else { return }
        defer { lastKnownVersion = version }
        guard updateRecovery != nil else { return }
        guard let before = versionAtUpdateFailure else {
            // No version was known when the update failed, which a git install
            // can genuinely be: the update check writes no current version
            // when there is a head_sha but nothing parses a version out of the
            // tree. Without a baseline nothing could ever retire the item, so
            // the first reading afterwards becomes one. It cannot retire
            // anything by itself -- there is no change yet -- and if a version
            // never becomes readable the way out stays, which is the honest
            // answer to "could not ask".
            versionAtUpdateFailure = version
            return
        }
        guard version != before else { return }
        clearUpdateRecovery()
    }

    /// The user is trying again, so the last update's way out is stale.
    func clearUpdateRecovery() {
        guard updateRecovery != nil else { return }
        updateRecovery = nil
        changed()
    }

    /// The daemon came up by some route other than us -- a terminal, another
    /// app. Whatever we were reporting is no longer true.
    func noteServerRunning() {
        // Only an update's way out survives a healthy server, and it is
        // retired by a version change instead. Anything else recorded here is
        // about the server being down -- `Recovery.forUpdateFailure` hands
        // back `.repair` when the update failed before the restart -- and a
        // server that is up has answered it.
        let staleUpdateRecovery = updateRecovery != nil && updateRecovery != .finishUpdate
        guard statusText != nil || hasFailed || recovery != nil
                || staleUpdateRecovery else { return }
        statusText = nil
        hasFailed = false
        recovery = nil
        if staleUpdateRecovery {
            updateRecovery = nil
            versionAtUpdateFailure = nil
        }
        changed()
    }

    func run(_ action: Action, reporting: Report,
             recoveryOnFailure: Recovery = .repair) {
        // Belt as well as braces. The menu hides these items while busy, but
        // the automatic start at launch does not go through the menu, so the
        // invariant belongs here rather than in what happens to be drawn.
        guard !isBusy else { return }
        isBusy = true
        hasFailed = false
        // Cleared with the failure it belonged to: through the retry window
        // the menu would otherwise offer the *previous* failure's recovery
        // beside "Starting…", after Open Server Log had already gone.
        recovery = nil
        statusText = action.present
        changed()

        invoke(action) { [weak self] code, output in
            guard let self else { return }
            self.deps.refreshState()
            guard code != 0 else {
                self.finish(status: nil)
                return
            }
            // Named by route as well as by verb. The extraction collapsed
            // "Start on launch failed" into this, making an automatic start
            // indistinguishable from a clicked one in Console -- which is the
            // one distinction someone reading Console after a login is after.
            let route = reporting == .menuOnly ? " on launch" : ""
            self.deps.log("quern \(action.rawValue)\(route) failed (\(code)): \(output)")

            // Nothing ran, so nothing will change on its own -- and the reason
            // is worth naming. "See Console" is right for a server that failed
            // to come up and wrong for a setup step that was never run, and
            // the two are indistinguishable from the outside.
            if code == QuernCLI.notFoundStatus {
                // Not `failed`: nothing ran, so the server log has nothing to
                // say about it. The status line carries the real answer.
                self.finish(status: "quern not found — run `quern setup`", recovery: .setUp)
                self.report(reporting, action: action, detail: output)
                return
            }

            // A failed stop is a failed stop; there is nothing to wait for.
            // Not `failed` either: the daemon is still up, so the icon must not
            // go red and the log is not where the answer is. The alert is the
            // channel here -- and it is always an alert, since nothing stops
            // the server automatically.
            if action == .stop {
                self.finish(status: nil)
                self.report(reporting, action: action, detail: output)
                return
            }

            self.waitForTheServerToAppear(attemptsLeft: self.graceAttempts,
                                          action: action, reporting: reporting, detail: output,
                                          recovery: recoveryOnFailure)
        }
    }

    // MARK: - Internals

    private func invoke(_ action: Action, _ done: @escaping (Int32, String) -> Void) {
        switch action {
        case .start: deps.start(done)
        case .stop: deps.stop(done)
        case .restart: deps.restart(done)
        }
    }

    /// The give-up case is passed as values rather than a closure.
    ///
    /// A closure built at the call site captures `self` strongly, and the
    /// scheduled block then holds the controller through it however the block
    /// itself captures -- which made the `[weak self]` below decorative. It is
    /// bounded at fifteen seconds and the delegate owns this for the life of
    /// the process, so nothing leaked; it just was not doing what it said.
    private func waitForTheServerToAppear(
        attemptsLeft: Int, action: Action, reporting: Report, detail: String,
        recovery: Recovery = .repair
    ) {
        if deps.serverIsRunning() {
            finish(status: nil)
            return
        }
        guard attemptsLeft > 0 else {
            finish(status: "Could not start the server", failed: true, recovery: recovery)
            report(reporting, action: action, detail: detail)
            return
        }
        deps.scheduler.after(graceInterval) { [weak self] in
            self?.waitForTheServerToAppear(attemptsLeft: attemptsLeft - 1,
                                           action: action, reporting: reporting, detail: detail,
                                           recovery: recovery)
        }
    }

    private func finish(status: String?, failed: Bool = false, recovery: Recovery? = nil) {
        isBusy = false
        statusText = status
        hasFailed = failed
        self.recovery = recovery
        changed()
    }

    private func report(_ reporting: Report, action: Action, detail: String) {
        guard reporting == .alert else { return }
        onAlert?("Could not \(action.rawValue) the server", detail, recovery)
    }

    private func changed() { onChange?() }
}
