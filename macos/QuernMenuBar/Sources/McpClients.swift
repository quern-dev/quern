// MCP clients that cannot start Quern's MCP server (#214).
//
// A registration outlives the node and the wrapper it names: `nvm uninstall`
// removes one, moving or reinstalling Quern the other. The client then reports
// only `CONNECTION_CLOSED`, and nothing Quern said ever mentioned it, so the one
// place a GUI user would find out is here. Plain `node` is not reported: each
// client resolves it its own way, and Claude Desktop reads the shell's PATH.
//
// The server decides what fails (server/lifecycle/mcp_clients.py) and this
// only shows it: a menu row while there is something to fix, and the dialog
// behind it. The dialog opens by itself only right after an update the user
// started -- the window `FailureReporting` already uses -- never at login.

import Foundation

enum McpClientAlertButton: Equatable {
    case fixInTerminal
    case copy
    case ok

    var title: String {
        switch self {
        case .fixInTerminal: return "Fix in Terminal"
        case .copy: return "Copy Command"
        case .ok: return "OK"
        }
    }
}

enum McpClientAlert {
    /// The menu row, or nil when there is nothing to fix.
    static func menuTitle(_ h: McpClientHealth) -> String? {
        switch h.problems.count {
        case 0: return nil
        case 1: return "\(h.problems[0].client) Can’t Start Quern…"
        default: return "\(h.problems.count) MCP Clients Can’t Start Quern…"
        }
    }

    static func title(_ h: McpClientHealth) -> String {
        h.problems.count == 1
            ? "\(h.problems[0].client) can’t start Quern’s MCP server"
            : "\(h.problems.count) MCP clients can’t start Quern’s MCP server"
    }

    /// The command that fixes what `quern mcp-install` can fix.
    static func command(_ h: McpClientHealth, quern: String = "quern") -> String? {
        h.fixClients.isEmpty ? nil : ([quern, "mcp-install"] + h.fixClients).joined(separator: " ")
    }

    /// How many reasons the dialog lists before summing up the rest. An
    /// alert's text neither scrolls nor caps its height, and a long one put
    /// the buttons off-screen once (#339).
    static let listed = 4

    static func body(_ h: McpClientHealth) -> String {
        var parts = h.problems.prefix(listed).map { $0.reason + "." }
        if h.problems.count > listed {
            parts.append("And \(h.problems.count - listed) more.")
        }
        if let command = command(h) {
            let one = h.fixClients.count == 1
            let them: String = one ? "it" : "them"
            let apps: String = one ? "that app" : "those apps"
            parts.append("Fix in Terminal runs `\(command)`, which registers \(them) with "
                         + "a Node 22 or later if it finds one, and says so if it does not. "
                         + "Then quit and reopen \(apps): a client reads its configuration "
                         + "when it starts.")
        }
        // What `mcp-install` cannot fix still needs saying -- once, however
        // many project entries there are.
        let projects = h.problems.filter { !$0.fixable }.map(\.client)
        if !projects.isEmpty {
            parts.append("\(projects.joined(separator: ", ")): `quern mcp-install` does not "
                         + "write project entries. Edit the command of quern-debug under that "
                         + "project in ~/.claude.json, or remove it so the user-wide one applies.")
        }
        parts.append("`quern doctor` shows which node each client runs.")
        return parts.joined(separator: "\n\n")
    }

    /// Fix first, so Return does it. Without anything `mcp-install` can fix,
    /// there is nothing to run or copy.
    static func buttons(_ h: McpClientHealth) -> [McpClientAlertButton] {
        h.fixClients.isEmpty ? [.ok] : [.fixInTerminal, .copy, .ok]
    }

    /// The Terminal script: register, then say what to do next.
    static func script(_ h: McpClientHealth, quern: String?) -> String {
        var body = ["echo \"Registering MCP clients with an absolute Node 22 or later.\"", "echo",
                    "echo \"\\$ quern mcp-install \(h.fixClients.joined(separator: " "))\""]
        body.append(([TerminalScript.shellQuote(quern ?? "quern"), "mcp-install"]
                     + h.fixClients.map(TerminalScript.shellQuote)).joined(separator: " "))
        body.append("echo")
        body.append("echo \"Quit and reopen those apps: a client reads its configuration when it starts.\"")
        body.append("echo")
        return TerminalScript.wrap(title: "Quern MCP clients", body: body)
    }

    /// A marker older than this is from some other attempt: an update does
    /// not take half an hour.
    static let markerLifetime: TimeInterval = 30 * 60

    /// Whether to open the dialog without being asked.
    ///
    /// Only right after an update started from this menu, and only once: at
    /// login a modal stealing focus is worse than a menu row, and the menu row
    /// is always there. `menuUpdateStartedAt` is when that update started --
    /// kept in this process, and left in `MenuUpdateMarker` for the app it
    /// relaunches -- so an update run in a terminal does not count. And only
    /// on a problem found since that update: one from before, or one carried
    /// through a pass that could not look, may describe what it replaced.
    static func shouldInterrupt(_ h: McpClientHealth, lastUpdate: UpdateResult?,
                                menuUpdateStartedAt: Date?, now: Date,
                                alreadyShown: Bool) -> Bool {
        guard !alreadyShown, h.problems.contains(where: { !$0.carried }),
              let started = menuUpdateStartedAt,
              let lastUpdate, lastUpdate.outcome == .updated,
              let finished = lastUpdate.finishedAt, finished >= started,
              finished.timeIntervalSince(started) <= markerLifetime,
              now.timeIntervalSince(finished) >= 0,
              now.timeIntervalSince(finished) <= FailureReporting.afterUpdateWindow,
              let checked = h.checkedAt, checked >= finished
        else { return false }
        return true
    }

    /// Whether this process should go on holding `menuUpdateStartedAt`.
    ///
    /// Held, it lets a later update -- one run in a terminal -- count as the
    /// menu's, which is what the rule above exists to prevent. So it is let go
    /// once its update has had its answer: a no-op or a failure, an answer
    /// written since a real update (whether or not it had problems), or the
    /// marker's lifetime passing.
    static func markerStillNeeded(_ h: McpClientHealth, lastUpdate: UpdateResult?,
                                  menuUpdateStartedAt started: Date, now: Date) -> Bool {
        if now.timeIntervalSince(started) > markerLifetime { return false }
        guard let lastUpdate, lastUpdate.describes(runStartedAt: started) else {
            return true                   // still running: no record of it yet
        }
        guard lastUpdate.outcome == .updated, let finished = lastUpdate.finishedAt else {
            return false
        }
        if let checked = h.checkedAt, checked >= finished { return false }
        return true
    }
}

/// The marker an update started from the menu leaves for the app it relaunches.
///
/// UserDefaults, because the relaunch is a new process. Consumed at launch:
/// read once and removed, so quitting and reopening the app, or a login after
/// a reboot, finds nothing.
enum MenuUpdateMarker {
    static let key = "QuernMenuUpdateStartedAt"

    static func record(_ now: Date = Date(), defaults: UserDefaults = .standard) {
        defaults.set(now.timeIntervalSince1970, forKey: key)
    }

    /// Removed when an update ends without relaunching the app -- a no-op, a
    /// failure, a version that did not change -- so it cannot reach a later
    /// launch and interrupt after an update run somewhere else.
    static func clear(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: key)
    }

    static func consume(defaults: UserDefaults = .standard) -> Date? {
        guard let seconds = defaults.object(forKey: key) as? Double else { return nil }
        defaults.removeObject(forKey: key)
        return Date(timeIntervalSince1970: seconds)
    }
}
