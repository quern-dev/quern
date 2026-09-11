// Thin wrapper around the installed `quern` CLI.
//
// All lifecycle actions shell out to `quern` rather than hitting the REST
// API: `quern stop`/`restart` already reuse the daemon's proxy-restore and
// daemonization logic, and shelling out means no API-key/HTTP handling.

import Foundation

enum QuernCLI {
    /// Where the installer puts a release install. Not derived from this
    /// bundle's location: the app is installed to ~/Applications (so Spotlight
    /// and Launchpad find it), which is nowhere near the install tree. It used
    /// to sit inside the install root, and the old `parent of Quern.app` rule
    /// silently kept resolving -- to ~/Applications -- so every path built on
    /// it pointed at a directory that will never contain what it wanted.
    static let releaseInstallDir = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent(".local/share/quern", isDirectory: true)

    /// Resolve the executable to run, most-preferred first:
    ///   1. ~/.local/bin/quern        (the wrapper `quern setup` installs)
    ///   2. ~/.local/share/quern/.venv/bin/quern-debug-server (release installs)
    ///   3. `quern` on PATH
    ///
    /// Returns nil when none of those exist, rather than handing back
    /// `/usr/bin/env quern` and letting it fail with 127. A GUI app does not
    /// inherit your shell's PATH, so "on PATH" here means only the few
    /// directories `run` adds below -- a clone on your own PATH is not among
    /// them. The 127 that results is indistinguishable from the command
    /// existing and failing, and it is the wrong thing to show a user whose
    /// actual problem is that setup has not run.
    /// Exit status used when nothing could be run at all, as distinct from a
    /// command that ran and failed. Callers key their wording off this.
    static let notFoundStatus: Int32 = 127

    static func resolve() -> (path: String, leadingArgs: [String])? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let wrapper = home.appendingPathComponent(".local/bin/quern").path
        if FileManager.default.isExecutableFile(atPath: wrapper) {
            return (wrapper, [])
        }
        let venvBin = releaseInstallDir.appendingPathComponent(".venv/bin/quern-debug-server").path
        if FileManager.default.isExecutableFile(atPath: venvBin) {
            return (venvBin, [])
        }
        if let onPath = which("quern") {
            return (onPath, [])
        }
        return nil
    }

    /// First executable named `name` in the same PATH `run` gives the child.
    private static func which(_ name: String) -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        for dir in searchPath(home: home) {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    /// The PATH handed to the child, so it can find python/git/etc. even when
    /// launched from a GUI context (which has a minimal PATH).
    private static func searchPath(home: String) -> [String] {
        let extra = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        let current = ProcessInfo.processInfo.environment["PATH"] ?? ""
        return extra + current.split(separator: ":").map(String.init)
    }

    /// Run a quern subcommand off the main thread. `completion` receives the
    /// exit status and combined output, dispatched back to the main thread.
    static func run(_ args: [String], completion: ((Int32, String) -> Void)? = nil) {
        guard let resolved = resolve() else {
            // Dispatched like every other completion. It used to be called
            // synchronously here, which made this one path re-enter the
            // caller before its own call returned -- harmless for today's
            // callers, all on the main thread, and a trap for the next one.
            let home = FileManager.default.homeDirectoryForCurrentUser
            let wrapper = home.appendingPathComponent(".local/bin/quern").path
            // Distinguish "not there" from "there but not runnable". A copy
            // that lost its mode bit reads as missing otherwise, and the
            // advice to run setup sends the reader looking for a file that is
            // sitting in front of them.
            let detail = FileManager.default.fileExists(atPath: wrapper)
                ? "\(wrapper) exists but is not executable. Fix it with:\n\n"
                    + "    chmod +x \(wrapper)"
                : "The menu bar app looks for \(wrapper), which `quern setup` "
                    + "writes. Run setup once from your install, then try again."
            DispatchQueue.main.async {
                completion?(notFoundStatus, "Could not find the quern command.\n\n" + detail)
            }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: resolved.path)
            proc.arguments = resolved.leadingArgs + args

            var env = ProcessInfo.processInfo.environment
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            env["PATH"] = searchPath(home: home).joined(separator: ":")
            proc.environment = env

            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe

            var status: Int32 = -1
            var output = ""
            do {
                try proc.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                proc.waitUntilExit()
                status = proc.terminationStatus
                output = String(data: data, encoding: .utf8) ?? ""
            } catch {
                output = "Failed to launch quern: \(error.localizedDescription)"
            }
            if let completion {
                DispatchQueue.main.async { completion(status, output) }
            }
        }
    }

    // Convenience actions ---------------------------------------------------

    static func start(_ completion: ((Int32, String) -> Void)? = nil) { run(["start"], completion: completion) }
    static func stop(_ completion: ((Int32, String) -> Void)? = nil) { run(["stop"], completion: completion) }
    static func restart(_ completion: ((Int32, String) -> Void)? = nil) { run(["restart"], completion: completion) }
    static func update(_ completion: ((Int32, String) -> Void)? = nil) { run(["update"], completion: completion) }
    static func setChannel(_ channel: String, completion: ((Int32, String) -> Void)? = nil) {
        run(["set-channel", channel], completion: completion)
    }

    static func setAutoInstallCert(_ enabled: Bool, completion: ((Int32, String) -> Void)? = nil) {
        run(["set-auto-install-cert", enabled ? "on" : "off"], completion: completion)
    }
}
