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

final class Updater {
    private var pollTimer: Timer?
    private var onStatus: ((String) -> Void)?

    /// `status` receives short human-readable progress strings for the menu.
    /// `failure` is called instead when the update could not be started at all,
    /// which is worth an alert rather than a line of menu text nobody reopens
    /// the menu to read.
    func restartToUpdate(
        status: @escaping (String) -> Void,
        failure: @escaping (String, String) -> Void
    ) {
        onStatus = status
        status("Checking version…")

        Self.installedVersion { [weak self] version in
            guard let self else { return }
            // No baseline means no way to recognise a change, and the failure
            // is not benign: every later reading compares unequal to nil, so
            // the first poll to succeed would look like the update finishing
            // and relaunch into the very bundle the update is replacing. If
            // the CLI cannot answer now it is not going to run `update`
            // either, so there is nothing lost by stopping here.
            guard let baseline = version else {
                status("Could not read the installed version")
                failure(
                    "Could not start the update",
                    "Quern could not report its installed version, so there would be "
                        + "nothing to compare against. Check that `quern --version` works."
                )
                return
            }
            status("Updating…")

            QuernCLI.update { [weak self] code, output in
                guard let self else { return }
                if code != 0 {
                    // `quern update` detaches a child and returns immediately,
                    // so a nonzero code here means it failed to even launch.
                    status("Update failed to start")
                    NSLog("quern update launch failed (\(code)): \(output)")
                    failure("Could not start the update", output)
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
        // The update runs in a detached child (~30–60s). Ask the CLI for its
        // version until it changes, then relaunch.
        var elapsed = 0.0
        var inFlight = false
        let interval = 2.0
        let timeout = 180.0

        pollTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] t in
            guard let self else { t.invalidate(); return }
            elapsed += interval
            // Each tick is a subprocess. Skip rather than queue when the last
            // one has not answered -- mid-update the venv is being rebuilt and
            // a call can hang, and piling them up would both spawn processes
            // without bound and let a stale answer arrive after the timeout.
            guard !inFlight else { return }
            inFlight = true
            Self.installedVersion { [weak self] current in
                inFlight = false
                guard let self, t.isValid else { return }
                if let current, current != baseline {
                    t.invalidate()
                    self.relaunch(into: current)
                } else if elapsed >= timeout {
                    t.invalidate()
                    self.onStatus?("Update timed out — check `quern update`")
                }
            }
        }
    }

    private func relaunch(into version: String, retriesLeft: Int = 1) {
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
                onStatus?("Update installed — restart Quern to finish")
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                self?.relaunch(into: version, retriesLeft: retriesLeft - 1)
            }
            return
        }
        onStatus?("Restarting to v\(version)…")
        let config = NSWorkspace.OpenConfiguration()
        config.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: bundleURL, configuration: config) { [weak self] _, error in
            if let error {
                NSLog("Relaunch failed: \(error.localizedDescription)")
                // Same reasoning as the missing-bundle path: without this the
                // status stays on "Restarting…" forever after a launch that
                // never happened.
                DispatchQueue.main.async {
                    self?.onStatus?("Update installed — restart Quern to finish")
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
    static func installedVersion(_ completion: @escaping (String?) -> Void) {
        QuernCLI.run(["--version"]) { code, output in
            guard code == 0 else {
                completion(nil)
                return
            }
            let line = output
                .split(separator: "\n")
                .first { $0.contains("quern ") }
                .map(String.init)?
                .trimmingCharacters(in: .whitespaces)
            guard let version = line?.split(separator: " ").last.map(String.init),
                  !version.isEmpty, version != "quern"
            else {
                completion(nil)
                return
            }
            completion(version)
        }
    }
}
