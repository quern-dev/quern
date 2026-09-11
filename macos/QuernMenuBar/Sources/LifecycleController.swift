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
        /// Re-read on-disk state, then say whether the daemon is up.
        var serverIsRunning: () -> Bool = { false }
        var start: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.start($0) }
        var stop: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.stop($0) }
        var restart: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.restart($0) }
        var log: (String) -> Void = { NSLog("%@", $0) }
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
    private(set) var hasFailed = false
    private(set) var statusText: String?

    /// Called whenever any of the three above change, so the icon can repaint.
    var onChange: (() -> Void)?
    /// Title and detail for a modal.
    var onAlert: ((String, String) -> Void)?

    init(_ deps: Dependencies = Dependencies()) {
        self.deps = deps
    }

    /// The daemon came up by some route other than us -- a terminal, another
    /// app. Whatever we were reporting is no longer true.
    func noteServerRunning() {
        guard statusText != nil || hasFailed else { return }
        statusText = nil
        hasFailed = false
        changed()
    }

    func run(_ action: Action, reporting: Report) {
        // Belt as well as braces. The menu hides these items while busy, but
        // the automatic start at launch does not go through the menu, so the
        // invariant belongs here rather than in what happens to be drawn.
        guard !isBusy else { return }
        isBusy = true
        hasFailed = false
        statusText = action.present
        changed()

        invoke(action) { [weak self] code, output in
            guard let self else { return }
            guard code != 0 else {
                self.finish(status: nil)
                return
            }
            self.deps.log("quern \(action.rawValue) failed (\(code)): \(output)")

            // Nothing ran, so nothing will change on its own -- and the reason
            // is worth naming. "See Console" is right for a server that failed
            // to come up and wrong for a setup step that was never run, and
            // the two are indistinguishable from the outside.
            if code == QuernCLI.notFoundStatus {
                self.finish(status: "quern not found — run `quern setup`", failed: true)
                self.report(reporting, action: action, detail: output)
                return
            }

            // A failed stop is a failed stop; there is nothing to wait for.
            if action == .stop {
                self.finish(status: "Could not stop the server", failed: true)
                self.report(reporting, action: action, detail: output)
                return
            }

            self.waitForTheServerToAppear(attemptsLeft: self.graceAttempts) {
                self.finish(status: "Could not start the server", failed: true)
                self.report(reporting, action: action, detail: output)
            }
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

    private func waitForTheServerToAppear(attemptsLeft: Int, giveUp: @escaping () -> Void) {
        if deps.serverIsRunning() {
            finish(status: nil)
            return
        }
        guard attemptsLeft > 0 else {
            giveUp()
            return
        }
        deps.scheduler.after(graceInterval) { [weak self] in
            self?.waitForTheServerToAppear(attemptsLeft: attemptsLeft - 1, giveUp: giveUp)
        }
    }

    private func finish(status: String?, failed: Bool = false) {
        isBusy = false
        statusText = status
        hasFailed = failed
        changed()
    }

    private func report(_ reporting: Report, action: Action, detail: String) {
        guard reporting == .alert else { return }
        onAlert?("Could not \(action.rawValue) the server", detail)
    }

    private func changed() { onChange?() }
}
