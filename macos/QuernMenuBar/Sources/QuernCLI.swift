// Thin wrapper around the installed `quern` CLI.
//
// All lifecycle actions shell out to `quern` rather than hitting the REST
// API: `quern stop`/`restart` already reuse the daemon's proxy-restore and
// daemonization logic, and shelling out means no API-key/HTTP handling.

import Foundation

enum QuernCLI {
    /// Install directory = the parent of Quern.app (…/quern/Quern.app → …/quern).
    static var installDir: URL {
        Bundle.main.bundleURL.deletingLastPathComponent()
    }

    /// Resolve the executable to run, most-preferred first:
    ///   1. ~/.local/bin/quern        (the wrapper `quern setup` installs)
    ///   2. <install>/.venv/bin/quern-debug-server (PATH-independent fallback)
    ///   3. `quern` on PATH
    static func resolve() -> (path: String, leadingArgs: [String])? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let wrapper = home.appendingPathComponent(".local/bin/quern").path
        if FileManager.default.isExecutableFile(atPath: wrapper) {
            return (wrapper, [])
        }
        let venvBin = installDir.appendingPathComponent(".venv/bin/quern-debug-server").path
        if FileManager.default.isExecutableFile(atPath: venvBin) {
            return (venvBin, [])
        }
        // Last resort: rely on PATH via /usr/bin/env.
        return ("/usr/bin/env", ["quern"])
    }

    /// Run a quern subcommand off the main thread. `completion` receives the
    /// exit status and combined output, dispatched back to the main thread.
    static func run(_ args: [String], completion: ((Int32, String) -> Void)? = nil) {
        guard let resolved = resolve() else {
            completion?(127, "Could not locate the quern executable.")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: resolved.path)
            proc.arguments = resolved.leadingArgs + args

            // Give the child a sane PATH so it can find python/git/etc. even
            // when launched from a GUI context (which has a minimal PATH).
            var env = ProcessInfo.processInfo.environment
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            let extra = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
            let current = env["PATH"] ?? ""
            env["PATH"] = (extra + [current]).joined(separator: ":")
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
}
