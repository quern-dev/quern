// Whether a full Xcode is installed, for the iPhone control setting (#664).
//
// WebDriverAgent is built and run with Xcode, so without it the "Through WDA"
// choice has nothing behind it. The server makes the fuller check -- Xcode,
// and WDA actually built -- and refuses with the reason; this one only decides
// whether the choice is offered.

import Foundation

enum XcodeCheck {
    /// The developer directory `xcode-select -p` names holds an `xcodebuild`.
    ///
    /// `xcode-select -p`, not `xcodebuild`: on a Mac with neither, the
    /// `/usr/bin/xcodebuild` shim opens the install dialog, and `xcode-select
    /// -p` never does. The Command Line Tools answer it too, with a directory
    /// that has no `xcodebuild`, which is what tells the two apart.
    static func isInstalled(_ completion: @escaping (Bool?) -> Void) {
        DispatchQueue.global(qos: .utility).async {
            let answer = developerDirectory().map { hasXcodebuild(developerDirectory: $0) }
            DispatchQueue.main.async { completion(answer) }
        }
    }

    /// `DEVELOPER_DIR` when set, as xcrun and the server read it (the CLI
    /// this app runs inherits it); else `xcode-select -p`. nil when that
    /// could not be run; "" when it ran and named none.
    private static func developerDirectory() -> String? {
        if let dir = ProcessInfo.processInfo.environment["DEVELOPER_DIR"], !dir.isEmpty {
            return dir
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        proc.arguments = ["-p"]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard proc.terminationStatus == 0 else { return "" }
        return String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func hasXcodebuild(developerDirectory dir: String) -> Bool {
        !dir.isEmpty && FileManager.default.isExecutableFile(atPath: dir + "/usr/bin/xcodebuild")
    }
}
