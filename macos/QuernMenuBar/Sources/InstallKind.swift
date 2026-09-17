// Which kind of install the app is driving, and how a git install updates.
//
// A release install updates cleanly from here: the tarball ships the MCP
// wrapper prebuilt, so nothing needs Node. A git install does not. Its update
// rebuilds the wrapper with npm, and a GUI app gets launchd's PATH, where a
// Node from fnm or nvm does not exist -- measured on the maintainer's machine,
// which has Node 22 in every shell and none at all for GUI apps (#214). `git
// pull` can also stop at a credential prompt a GUI app has no way to show. So
// a git install is updated in Terminal, where the user's own environment is.

import AppKit
import Foundation

enum InstallKind: Equatable {
    case git(URL)
    case release(URL)
    case unknown

    /// The line `quern setup` writes into the wrapper, naming the install.
    static let wrapperRootPrefix = "# Points to: "

    /// Decide from the executable the app runs. Pure, so tests can feed it.
    ///
    /// The wrapper names its install root; a release venv binary lives under
    /// the release install directory. Either way, a `.git` entry in the root
    /// makes it a checkout -- `exists` rather than a directory test, because a
    /// git worktree's `.git` is a file.
    static func of(
        resolved: String?,
        releaseRoot: URL = QuernCLI.releaseInstallDir,
        readFile: (String) -> String? = { try? String(contentsOfFile: $0, encoding: .utf8) },
        exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) -> InstallKind {
        guard let resolved else { return .unknown }
        var root: URL?
        if let text = readFile(resolved),
           let line = text.split(separator: "\n").first(where: { $0.hasPrefix(wrapperRootPrefix) }) {
            let path = line.dropFirst(wrapperRootPrefix.count)
                .trimmingCharacters(in: .whitespaces)
            if !path.isEmpty { root = URL(fileURLWithPath: path, isDirectory: true) }
        } else if resolved.hasPrefix(releaseRoot.path + "/") {
            root = releaseRoot
        }
        guard let root else { return .unknown }
        return exists(root.appendingPathComponent(".git").path) ? .git(root) : .release(root)
    }

    /// The install the app would run right now.
    static var current: InstallKind { of(resolved: QuernCLI.resolve()?.path) }

    var isGit: Bool {
        if case .git = self { return true }
        return false
    }

    /// For the Settings row.
    var display: String {
        switch self {
        case .git(let root): return "Git checkout — \(Self.abbreviate(root.path))"
        case .release(let root): return "Release — \(Self.abbreviate(root.path))"
        case .unknown: return "—"
        }
    }

    static func abbreviate(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
    }
}

enum TerminalUpdate {
    /// Where the guide explains git versus release installs, and how to switch.
    static let docs = URL(string: "https://quern.dev/getting-started/menu-bar-app/#updating")!

    static let terminalApp = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")

    /// A `.command` file Terminal runs in a new window.
    ///
    /// A file rather than AppleScript: scripting Terminal from a
    /// hardened-runtime app needs the apple-events entitlement, a usage
    /// string, and a permission prompt the user has to accept, and until they
    /// do it fails with -1743. Opening a document needs none of that.
    ///
    /// The wrapper's absolute path, quoted: Terminal's shell probably has
    /// `quern` on PATH, but "probably" is the thing this whole feature exists
    /// to stop relying on.
    ///
    /// It ends by becoming the user's own login shell. Terminal's default
    /// profile closes a window whose shell exits cleanly, so a successful
    /// update vanished before anyone could read it. Handing the window to a
    /// shell keeps the output on screen and leaves somewhere to run
    /// `quern doctor` from; a "press Return" pause would only do the first.
    static func script(quern: String) -> String {
        """
        #!/bin/sh
        # Written by the Quern menu-bar app to update a git install. Safe to delete.
        clear
        echo "Updating Quern (git install)..."
        echo
        \(shellQuote(quern)) update
        status=$?
        echo
        if [ "$status" -eq 0 ]; then
          echo "Done."
        else
          echo "quern update exited $status. The output above says why."
        fi
        echo "This window is now an ordinary shell; close it when you're finished."
        exec "${SHELL:-/bin/zsh}" -l

        """
    }

    static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Write the script and open it in Terminal. `completion` receives an
    /// error description, or nil once Terminal has it.
    static func open(completion: @escaping (String?) -> Void) {
        guard let quern = QuernCLI.resolve()?.path else {
            completion("Could not find the quern command. Run `quern setup` in a terminal.")
            return
        }
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("quern-update.command")
        do {
            try script(quern: quern).write(to: file, atomically: true, encoding: .utf8)
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

/// What the menu offers for a staged update. Separate from the menu so a test
/// can drive the decision rather than rebuilding it.
enum UpdateMenuItem: Equatable {
    case restartToUpdate(String)
    case updateInTerminal(String)

    static func forStaged(latestVersion: String?, install: InstallKind) -> UpdateMenuItem {
        let suffix = latestVersion.map { " — v\($0)" } ?? ""
        return install.isGit
            ? .updateInTerminal("Update in Terminal…" + suffix)
            : .restartToUpdate("Restart to Update" + suffix)
    }
}
