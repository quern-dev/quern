// MCP clients that cannot start Quern's MCP server (#214).
//
// A client registered with plain `node` resolves it on its own PATH, and an
// app opened from the Dock gets launchd's -- no fnm, nvm, Volta, asdf or mise,
// since those are set up in shell startup files it never reads. A registration
// can also outlive the node it names. Either way the client reports only
// `CONNECTION_CLOSED`, and nothing Quern said ever mentioned it, so the one
// place a GUI user would find out is here.
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

    static func body(_ h: McpClientHealth) -> String {
        var parts = h.problems.map { $0.reason + "." }
        if let command = command(h) {
            let one = h.fixClients.count == 1
            let them: String = one ? "it" : "them"
            let apps: String = one ? "that app" : "those apps"
            parts.append("Fix in Terminal runs `\(command)`, which registers \(them) with "
                         + "a Node 22 or later that apps opened from the Dock can run. Then "
                         + "quit and reopen \(apps): a client reads its configuration when it "
                         + "starts.")
        }
        // What `mcp-install` cannot fix still needs saying: a project entry.
        for p in h.problems where !p.fixable && !p.fix.isEmpty {
            parts.append("\(p.client): \(p.fix).")
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

    /// Whether to open the dialog without being asked.
    ///
    /// Only right after an update the user started, and only once: at login a
    /// modal stealing focus is worse than a menu row, and the menu row is
    /// always there. Only on an answer written since that update, since the
    /// one from before it may describe a registration the update replaced.
    static func shouldInterrupt(_ h: McpClientHealth, lastUpdate: UpdateResult?,
                                now: Date, alreadyShown: Bool) -> Bool {
        guard !alreadyShown, !h.problems.isEmpty,
              FailureReporting.forLaunchStart(lastUpdate: lastUpdate, now: now) == .alert,
              let finished = lastUpdate?.finishedAt, let checked = h.checkedAt,
              checked >= finished
        else { return false }
        return true
    }
}
