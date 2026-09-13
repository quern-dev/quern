// Drives the "Restart to Update" action.
//
// Flow: trigger `quern update` (which downloads + replaces the install tree,
// including this Quern.app, and restarts the daemon), watch the version
// reported by the CLI flip, then relaunch from the freshly-installed bundle so
// the running menu-bar binary is replaced too.
//
// The version comes from `quern --version` rather than from a pyproject.toml
// this app locates itself. It used to read one next to the bundle, which was
// right while Quern.app lived inside the install tree and became permanently
// wrong when it moved to ~/Applications: the read failed, the poll never saw a
// change, and every update ran the full three minutes before reporting a
// timeout. Asking the CLI has no path to get wrong, and it is the same answer
// the server gives.

import AppKit

/// Where an update has got to.
///
/// Split into working and finished because the two belong in different places.
/// A menu line is only read when someone opens the menu, and the menu closes
/// the instant you click an item -- so an update reported only there ran for
/// three minutes with nothing on screen at all. Work in progress has to reach
/// the status item itself; a final answer can wait for the menu.
enum UpdateProgress: Equatable {
    case working(String)
    case finished(String)

    var text: String {
        switch self {
        case .working(let t), .finished(let t): return t
        }
    }

    var isWorking: Bool {
        if case .working = self { return true }
        return false
    }
}

final class Updater {
    /// What the update does with the outside world, so a test can stand in for
    /// all of it. Defaults are the real thing, so nothing but a test passes any
    /// of these.
    ///
    /// Closures rather than a protocol: there are four, they have nothing in
    /// common, and a protocol would only give them a shared name they do not
    /// need. `relaunch` is here because the alternative is an NSWorkspace call
    /// that quits the app mid-test.
    struct Dependencies {
        var scheduler: Scheduler = SystemScheduler()
        var readVersion: (@escaping (String?, String) -> Void) -> Void = Updater.installedVersion
        var runUpdate: (@escaping (Int32, String) -> Void) -> Void = { QuernCLI.update($0) }
        var relaunch: ((String) -> Void)?
        /// What the update recorded about itself. See UpdateResult.
        var readResult: () -> UpdateResult? = { UpdateResult.read() }
        /// Where the account goes. Injected for the same reason
        /// LifecycleController injects its own: without it the test binary
        /// writes into the real system log, and those lines are
        /// indistinguishable from the app's when read back.
        var log: (String) -> Void = { Log.updater.notice("\($0, privacy: .public)") }
        var logError: (String) -> Void = { Log.updater.error("\($0, privacy: .public)") }
    }

    private let deps: Dependencies
    private var pollWork: ScheduledWork?
    private var onStatus: ((UpdateProgress) -> Void)?

    init(_ deps: Dependencies = Dependencies()) {
        self.deps = deps
    }
    /// Guards against a second "Restart to Update" while one is in progress.
    ///
    /// The menu item stays enabled throughout -- it is gated on
    /// `update_available`, which nothing rewrites until the update lands -- so
    /// a second click was always possible. It used to be harmless only because
    /// the update never appeared to finish; now that it works, two clicks mean
    /// two `quern update` processes against one install tree, each doing a git
    /// pull and a pip install, and two live poll timers either of which can
    /// relaunch the app.
    private var inProgress = false

    /// `status` receives short human-readable progress strings for the menu.
    /// `failure` is called instead when the update could not be started at all,
    /// which is worth an alert rather than a line of menu text nobody reopens
    /// the menu to read.
    func restartToUpdate(
        status: @escaping (UpdateProgress) -> Void,
        failure: @escaping (String, String) -> Void
    ) {
        guard !inProgress else { return }
        inProgress = true
        onStatus = status
        status(.working("Checking version…"))

        deps.readVersion { [weak self] version, detail in
            guard let self else { return }
            // No baseline means no way to recognise a change, and the failure
            // is not benign: every later reading compares unequal to nil, so
            // the first poll to succeed would look like the update finishing
            // and relaunch into the very bundle the update is replacing. If
            // the CLI cannot answer now it is not going to run `update`
            // either, so there is nothing lost by stopping here.
            guard let baseline = version else {
                self.inProgress = false
                status(.finished("Could not read the installed version"))
                failure("Could not start the update", detail)
                return
            }
            status(.working("Updating…"))

            // Before the CLI starts, so a record left by an earlier run cannot
            // be read as this one's answer. That is not hypothetical: the CLI
            // that upgrades *to* the first version writing the record is the
            // old one, which writes nothing.
            let startedAt = self.deps.scheduler.now

            self.deps.runUpdate { [weak self] code, output in
                guard let self else { return }
                // Every run, not only the failures. This output was the only
                // account of what the update actually did -- which branch it
                // was on, what it skipped, which external tools are behind --
                // and throwing it away on success left no record anywhere of a
                // successful or no-op update.
                self.deps.log("quern update exited \(code): \(output)")
                if code != 0 {
                    // `quern update` runs the whole update synchronously, so
                    // this completion does not arrive for a minute or more and
                    // a nonzero code means the update itself failed -- not, as
                    // this once said, that it failed to launch a detached
                    // child. `output` is the CLI's own reason; pass it through
                    // rather than paraphrasing it.
                    self.inProgress = false
                    status(.finished("Update failed"))
                    self.deps.logError("quern update failed (\(code)): \(output)")
                    failure("The update did not complete", output)
                    return
                }
                // Exit 0 does not mean something happened. "Already up to
                // date" has to exit 0 too, or every script treating nonzero as
                // failure breaks -- so the code alone cannot tell an update
                // from a no-op, and reading it as an update meant polling
                // thirty seconds for a version that was never going to move,
                // then reporting that an update had finished when none was
                // attempted. The CLI writes down which it was.
                let recorded = self.deps.readResult()
                if recorded?.describes(runStartedAt: startedAt) == true,
                   recorded?.isNoOp == true {
                    self.inProgress = false
                    status(.finished("Already up to date"))
                    return
                }
                self.waitForNewVersionThenRelaunch(baseline: baseline)
            }
        }
    }

    /// `baseline` is non-optional deliberately: the comparison below is only
    /// meaningful against a version we actually read, so the caller has to
    /// have one rather than this having to defend against not having one.
    private func waitForNewVersionThenRelaunch(baseline: String) {
        // The version the CLI reports should already have changed by the time
        // we get here, since `quern update` is synchronous. Poll anyway: the
        // restart it performs is not instant, and a version read taken during
        // it can fail.
        //
        // Running out of time is therefore not "the update timed out" -- the
        // update already exited 0. The likeliest cause is that the version
        // genuinely did not move, which happens on a git install whenever
        // commits land without a bump in pyproject.toml. Saying it timed out
        // reported a failure for something that worked.
        var inFlight = false
        let interval = 2.0
        // A wall-clock deadline, not a tick count. Counting ticks assumes every
        // tick happens, and the default run-loop mode does not fire while a
        // menu is tracking or a modal is up. So a menu left open paused the
        // countdown, and the deadline that exists to stop "Updating…" lasting
        // forever could itself be stopped by looking at it. `SystemScheduler`
        // uses `.common` for the same reason.
        //
        // Thirty seconds, not the three minutes this started with. That figure
        // was sized for an update running in the background, which it does not:
        // `quern update` is synchronous, so by the time this poll begins the
        // pull, the reinstall and the daemon restart have all finished. What
        // remains is a version read that can fail while the restart settles --
        // seconds of work. Three minutes was therefore three minutes of
        // "Updating…" on screen for an update that had already finished and
        // simply not moved the version, which is the common case on a git
        // checkout ahead of its release branch.
        let deadline = deps.scheduler.now.addingTimeInterval(30)

        pollWork?.cancel()
        pollWork = deps.scheduler.repeating(every: interval) { [weak self] t in
            guard let self else { t.cancel(); return }

            // The deadline is checked here, in the timer body, and not inside
            // the completion below. It used to live there, which made it
            // unreachable in the one case it exists for: a `quern --version`
            // that never returns leaves `inFlight` true forever, every later
            // tick stops at the guard, and the completion holding the only
            // timeout check never runs. The menu then reads "Updating…" for
            // the life of the process, which is the state that tells the user
            // nothing is wrong. `QuernCLI.run` has no timeout of its own, so
            // there is no other way out.
            if self.deps.scheduler.now >= deadline {
                t.cancel()
                self.inProgress = false
                self.onStatus?(.finished("Update finished, but the version did not change"))
                return
            }

            // Each tick is a subprocess. Skip rather than queue when the last
            // one has not answered -- mid-update the venv is being rebuilt and
            // a call can hang, and piling them up would both spawn processes
            // without bound and let a stale answer arrive after the deadline.
            guard !inFlight else { return }
            inFlight = true
            self.deps.readVersion { [weak self] current, _ in
                inFlight = false
                guard let self, t.isActive else { return }
                guard let current, current != baseline else { return }
                t.cancel()
                self.relaunch(into: current)
            }
        }
    }

    private func relaunch(into version: String, retriesLeft: Int = 1) {
        if let hook = deps.relaunch {
            inProgress = false
            hook(version)
            return
        }
        let bundleURL = Bundle.main.bundleURL
        // The bundle was replaced on disk during the update; if it's briefly
        // missing (delete-then-move window) wait a beat and retry. The retry
        // is counted rather than open-ended: the earlier version recursed on
        // the same condition forever, so an update that left no bundle behind
        // rescheduled every 1.5s for the life of the process while the menu
        // still read "Restarting to v…", which is the one state that tells
        // the user nothing is wrong.
        guard FileManager.default.fileExists(atPath: bundleURL.path) else {
            guard retriesLeft > 0 else {
                inProgress = false
                onStatus?(.finished("Update installed — restart Quern to finish"))
                return
            }
            deps.scheduler.after(1.5) { [weak self] in
                self?.relaunch(into: version, retriesLeft: retriesLeft - 1)
            }
            return
        }
        onStatus?(.working("Restarting to v\(version)…"))
        let config = NSWorkspace.OpenConfiguration()
        config.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: bundleURL, configuration: config) { [weak self] _, error in
            if let error {
                self?.deps.logError("Relaunch failed: \(error.localizedDescription)")
                // Same reasoning as the missing-bundle path: without this the
                // status stays on "Restarting…" forever after a launch that
                // never happened.
                DispatchQueue.main.async {
                    self?.inProgress = false
                    self?.onStatus?(.finished("Update installed — restart Quern to finish"))
                }
                return
            }
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }

    /// The installed version, from `quern --version` ("quern 0.16.1").
    ///
    /// nil means "could not tell", never "unchanged": mid-update the CLI is
    /// briefly unrunnable, and callers must keep waiting rather than treat a
    /// failed read as an answer.
    static func installedVersion(_ completion: @escaping (String?, String) -> Void) {
        // Short: this runs on a 2s poll during an update, and a version read
        // that has not answered in half a minute is not going to.
        QuernCLI.run(["--version"], timeout: 30) { code, output in
            guard code == 0 else {
                // `output` carries the CLI's own reason -- including the one
                // that names the missing wrapper. Flattening it to nil here
                // is how a user with no ~/.local/bin/quern ended up being told
                // to check `quern --version`, which works fine in their shell.
                completion(nil, output)
                return
            }
            // stdout and stderr share one pipe, so "a line mentioning quern"
            // is not a tight enough net: any stderr line containing that word
            // would win, and a junk token compares unequal to the baseline,
            // which the poll reads as the update having finished. Require the
            // shape `quern <digit>…` that the CLI actually prints.
            let version = output
                .split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .first { $0.hasPrefix("quern ") }?
                .dropFirst("quern ".count)
                .trimmingCharacters(in: .whitespaces)
            guard let version, let first = version.first, first.isNumber else {
                completion(nil, output)
                return
            }
            completion(version, output)
        }
    }
}
