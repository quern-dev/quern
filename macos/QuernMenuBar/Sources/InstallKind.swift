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

    /// The update script. The wrapper's absolute path, quoted: Terminal's
    /// shell probably has `quern` on PATH, but "probably" is the thing this
    /// whole feature exists to stop relying on. `TerminalScript.wrap` leaves
    /// the window open afterwards.
    /// `reason` only changes the words. `quern update` handles both install
    /// kinds itself, so the script is the same either way -- but a release
    /// install sent here because its node is hidden was being told it was a git
    /// checkout, which is a confusing thing to read while following advice.
    static func script(quern: String, reason: TerminalReason = .gitInstall) -> String {
        let why: String
        switch reason {
        case .gitInstall: why = "git install"
        case .nodeManagedElsewhere(let manager): why = "Node is managed by \(manager)"
        }
        return TerminalScript.wrap(title: "update quern (\(why))", body: [
            "echo \"Updating Quern (\(why))...\"",
            "echo",
            "\(shellQuote(quern)) update",
            "status=$?",
            "echo",
            "if [ \"$status\" -eq 0 ]; then",
            "  echo \"Done.\"",
            "else",
            "  echo \"quern update exited $status. The output above says why.\"",
            "fi",
        ])
    }

    static func shellQuote(_ s: String) -> String { TerminalScript.shellQuote(s) }

    /// Write the script and open it in Terminal. `completion` receives an
    /// error description, or nil once Terminal has it.
    static func open(reason: TerminalReason = .gitInstall,
                     completion: @escaping (String?) -> Void)
    {
        guard let quern = QuernCLI.resolve()?.path else {
            completion("Could not find the quern command. Run `quern setup` in a terminal.")
            return
        }
        TerminalScript.open(name: "quern-update.command",
                            contents: script(quern: quern, reason: reason),
                            completion: completion)
    }
}

/// Whether this process can see a `node`, and what to say if it cannot.
///
/// A release install was assumed not to need Node at all, because the tarball
/// ships `mcp/dist` prebuilt. True of the *build* and false of the *check*:
/// `quern setup` runs `check_node()`, which reports MISSING, and the update
/// fails naming Node on a machine where Node is installed and working (#339).
///
/// A shell's fnm node lives in a directory containing the pid of the shell
/// that asked for it -- `~/.local/state/fnm_multishells/800_1789402185835/bin/
/// node` on the machine this was found on. That one no static PATH can name,
/// but fnm's `default` alias can be, and `QuernCLI.searchPath` now includes it
/// (#447), so an fnm user with a default set reads `.visible`. What is left
/// here is a manager with no stable directory -- nvm's default is a version
/// string in a file -- or fnm with no default. Terminal is not a workaround
/// for those, it is the answer: that is where the user's own environment is.
enum NodeVisibility: Equatable {
    case visible
    /// Not on our PATH, and a version manager is installed that would explain
    /// why. Named so the menu can say which one.
    case managedElsewhere(String)
    /// Not on our PATH and no manager found, so Terminal probably will not help
    /// either. Left alone: the ordinary "Node is not installed" case is real,
    /// and routing it to Terminal would only move the same failure.
    case absent

    /// Directories a manager keeps in `$HOME`.
    ///
    /// Related to `node_env._MANAGER_PATHS` but deliberately not the same list,
    /// and the difference is not drift: that one classifies a path `probe`
    /// already found, this one guesses from a directory existing. So brew,
    /// MacPorts, nix and `n` are absent here on purpose -- they install into
    /// `/opt/homebrew/bin` or `/usr/local/bin`, which are on
    /// `QuernCLI.searchPath`, so a node from them reads `.visible` and never
    /// reaches this list.
    ///
    /// Ordering only picks the label when two are installed; a stale `~/.nvm`
    /// beside a live fnm says "nvm", which is one word in a menu title.
    static let managerMarkers: [(String, String)] = [
        (".local/share/mise", "mise"),
        (".asdf", "asdf"),
        (".nodenv", "nodenv"),
        (".volta", "volta"),
        (".nvm", "nvm"),
        (".local/state/fnm_multishells", "fnm"),
        ("Library/Application Support/fnm", "fnm"),
        (".fnm", "fnm"),
        ("Library/pnpm", "pnpm"),
        (".local/share/pnpm", "pnpm"),
    ]

    static func check(
        home: String = FileManager.default.homeDirectoryForCurrentUser.path,
        searchPath: [String]? = nil,
        isExecutable: (String) -> Bool = { FileManager.default.isExecutableFile(atPath: $0) },
        exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) -> NodeVisibility {
        let dirs = searchPath ?? QuernCLI.searchPath(home: home)
        if dirs.contains(where: { isExecutable("\($0)/node") }) { return .visible }
        for (suffix, name) in managerMarkers where exists("\(home)/\(suffix)") {
            return .managedElsewhere(name)
        }
        return .absent
    }

    /// Whether the GUI should decline to update and hand over to Terminal.
    var needsTerminal: Bool {
        if case .managedElsewhere = self { return true }
        return false
    }
}

/// Why an update is being sent to Terminal, so the menu can say so. The git
/// case has said "Why Terminal? (git install)" for a while; a reason the user
/// cannot guess is worse, not better, so the Node case says which manager.
enum TerminalReason: Equatable {
    case gitInstall
    case nodeManagedElsewhere(String)

    var menuTitle: String {
        switch self {
        case .gitInstall: return "Why Terminal? (git install)"
        case .nodeManagedElsewhere(let manager): return "Why Terminal? (Node is managed by \(manager))"
        }
    }
}

/// What the menu offers for a staged update. Separate from the menu so a test
/// can drive the decision rather than rebuilding it.
enum UpdateMenuItem: Equatable {
    case restartToUpdate(String)
    case updateInTerminal(String, TerminalReason)

    /// A git install is checked first. When both apply the git reason is the
    /// one to show: it is the property of the install, true on every machine,
    /// where the Node one is a property of this launch.
    ///
    /// No version in the title (#352). The one available is `latest_version`
    /// from `update-info.json`, a cached answer refreshed at most once a day,
    /// while the update itself installs the newest release on the channel -- so
    /// the number could be older than what the click installs. The item
    /// promises what it does: update to the latest.
    static func forStaged(install: InstallKind,
                          node: NodeVisibility = .check()) -> UpdateMenuItem
    {
        if install.isGit {
            return .updateInTerminal("Update in Terminal…", .gitInstall)
        }
        if case .managedElsewhere(let manager) = node {
            return .updateInTerminal("Update in Terminal…", .nodeManagedElsewhere(manager))
        }
        return .restartToUpdate("Restart to Update")
    }

    /// The Settings window's update line. Without a version, for the reason
    /// above -- and decided by `updateAvailable` alone: it used to need a
    /// `latest_version` too, so an update the server reported without one
    /// (older quern.dev deployments did not send it) read "Up to date" while
    /// the menu offered Restart to Update.
    static func settingsLine(updateAvailable: Bool) -> String {
        updateAvailable ? "Update available" : "Up to date"
    }

    /// The status item's tooltip. Without a version, for the reason above.
    static func tooltip(failed: Bool, updateAvailable: Bool, running: Bool) -> String {
        if failed { return "Quern could not start — open the menu" }
        if updateAvailable { return "Quern — update available" }
        return running ? "Quern is running" : "Quern is stopped"
    }
}
