// Drives the "Restart to Update" action.
//
// Flow: trigger `quern update` (which downloads + replaces the install tree,
// including this Quern.app, and restarts the daemon), watch the install
// dir's pyproject.toml version flip, then relaunch from the freshly-installed
// bundle so the running menu-bar binary is replaced too.

import AppKit

final class Updater {
    private var pollTimer: Timer?
    private var startVersion: String?
    private var onStatus: ((String) -> Void)?

    /// `status` receives short human-readable progress strings for the menu.
    func restartToUpdate(status: @escaping (String) -> Void) {
        onStatus = status
        startVersion = Self.installedVersion()
        status("Updating…")

        QuernCLI.update { [weak self] code, output in
            guard let self else { return }
            if code != 0 {
                // `quern update` detaches a child and returns immediately, so a
                // nonzero code here means it failed to even launch.
                status("Update failed to start")
                NSLog("quern update launch failed (\(code)): \(output)")
                return
            }
            self.waitForNewVersionThenRelaunch()
        }
    }

    private func waitForNewVersionThenRelaunch() {
        // The update runs in a detached child (~30–60s). Poll pyproject.toml in
        // the install dir until the version changes, then relaunch.
        var elapsed = 0.0
        let interval = 2.0
        let timeout = 180.0

        pollTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] t in
            guard let self else { t.invalidate(); return }
            elapsed += interval
            let current = Self.installedVersion()
            if let current, current != self.startVersion {
                t.invalidate()
                self.relaunch(into: current)
            } else if elapsed >= timeout {
                t.invalidate()
                self.onStatus?("Update timed out — check `quern update`")
            }
        }
    }

    private func relaunch(into version: String) {
        let bundleURL = Bundle.main.bundleURL
        // The bundle was replaced on disk during the update; if it's briefly
        // missing (delete-then-move window) wait a beat and retry once.
        guard FileManager.default.fileExists(atPath: bundleURL.path) else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                self?.relaunch(into: version)
            }
            return
        }
        onStatus?("Restarting to v\(version)…")
        let config = NSWorkspace.OpenConfiguration()
        config.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: bundleURL, configuration: config) { _, error in
            if let error {
                NSLog("Relaunch failed: \(error.localizedDescription)")
                return
            }
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }

    /// Parse `version = "x.y.z"` from the install dir's pyproject.toml — the
    /// same single source of truth the server uses (server/__init__.py).
    static func installedVersion() -> String? {
        let pyproject = QuernCLI.installDir.appendingPathComponent("pyproject.toml")
        guard let text = try? String(contentsOf: pyproject, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("version") {
                let parts = trimmed.components(separatedBy: "\"")
                if parts.count >= 2 { return parts[1] }
            }
        }
        return nil
    }
}
