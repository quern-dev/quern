// MCP clients that cannot start Quern's MCP server (#214).

import Foundation

enum McpClientsTests {
    static let finished = Date(timeIntervalSince1970: 1_790_000_000)

    static func health(_ problems: [McpClientProblem], fix: [String],
                       checkedAt: Date? = nil) -> McpClientHealth {
        McpClientHealth(problems: problems, fixClients: fix, checkedAt: checkedAt ?? finished)
    }

    static let desktop = McpClientProblem(
        client: "Claude Desktop",
        reason: "Claude Desktop runs plain `node`, and apps opened from the Dock find none",
        fix: "`quern mcp-install claude-desktop` registers an absolute Node 22+")
    static let project = McpClientProblem(
        client: "Claude Code (/src/app)",
        reason: "Claude Code (/src/app) is set to run /gone/node, which no longer exists",
        fix: "edit the command of `quern-debug` under projects[\"/src/app\"].mcpServers "
            + "in ~/.claude.json; `quern mcp-install` does not write project entries",
        fixable: false)

    static func update(_ outcome: UpdateResult.Outcome, at when: Date) -> UpdateResult {
        UpdateResult(outcome: outcome, detail: "", version: "0.23.0", finishedAt: when)
    }

    static func all() {
        Harness.test("the file is read, nested problems included") {
            let h = StateReader.readMcpClients(contents: [
                "checked_at": "2026-09-30T09:00:00.123456+00:00",
                "fix_clients": ["claude-desktop"],
                "problems": [["client": "Claude Desktop", "reason": "r", "fix": "f",
                              "fixable": true],
                             ["client": "no reason"]],
            ])
            Harness.expect(h.problems, [McpClientProblem(client: "Claude Desktop", reason: "r",
                                                         fix: "f")], "the well-formed one")
            Harness.expect(h.fixClients, ["claude-desktop"], "fix clients")
            Harness.expect(h.checkedAt != nil, "fractional seconds parse")
        }

        Harness.test("no file, or nonsense, is no problems") {
            Harness.expect(StateReader.readMcpClients(contents: .some(nil)), McpClientHealth(),
                           "absent")
            let junk = StateReader.readMcpClients(contents: ["problems": "nope"])
            Harness.expect(junk.problems.isEmpty, "a string where a list goes")
        }

        Harness.test("the menu row appears only while there is something to fix") {
            Harness.expect(McpClientAlert.menuTitle(health([], fix: [])), nil, "none")
            Harness.expect(McpClientAlert.menuTitle(health([desktop], fix: ["claude-desktop"])),
                           "Claude Desktop Can’t Start Quern…", "one")
            Harness.expect(McpClientAlert.menuTitle(health([desktop, project], fix: [])),
                           "2 MCP Clients Can’t Start Quern…", "two")
        }

        Harness.test("the command fixes what mcp-install can, and nothing else") {
            let h = health([desktop, project], fix: ["claude-desktop", "cursor"])
            Harness.expect(McpClientAlert.command(h), "quern mcp-install claude-desktop cursor",
                           "command")
            Harness.expect(McpClientAlert.command(health([project], fix: [])), nil,
                           "a project entry alone has no command")
        }

        Harness.test("the body says why, what the button runs, to reopen the app, and the rest") {
            let body = McpClientAlert.body(health([desktop, project], fix: ["claude-desktop"]))
            Harness.expect(body.contains(desktop.reason), "the reason")
            Harness.expect(body.contains("`quern mcp-install claude-desktop`"), "the command")
            Harness.expect(body.contains("quit and reopen that app"), "restart the client")
            Harness.expect(body.contains("Claude Code (/src/app): edit the command"),
                           "the fix mcp-install cannot make")
            Harness.expect(!body.contains("Claude Desktop: `quern mcp-install"),
                           "a fixable one's fix is not repeated")
            Harness.expect(body.contains("quern doctor"), "where to look")
        }

        Harness.test("Fix first, so Return runs it; nothing to run, only OK") {
            Harness.expect(McpClientAlert.buttons(health([desktop], fix: ["claude-desktop"])),
                           [.fixInTerminal, .copy, .ok], "fixable")
            Harness.expect(McpClientAlert.buttons(health([project], fix: [])), [.ok], "not")
        }

        Harness.test("the script runs mcp-install by path, quoted, and stays open") {
            let text = McpClientAlert.script(health([desktop], fix: ["claude-desktop", "o'dd"]),
                                             quern: "/Users/u/.local/bin/quern")
            let l = text.split(separator: "\n").map(String.init)
            Harness.expect(l.contains("'/Users/u/.local/bin/quern' mcp-install 'claude-desktop' 'o'\\''dd'"),
                           "the command: \(l)")
            Harness.expect(l.last, "exec \"${SHELL:-/bin/zsh}\" -l", "ends in a shell")
            Harness.expect(text.contains("Quit and reopen"), "what to do next")
        }

        Harness.test("it interrupts right after an update, once, on a fresh answer") {
            let h = health([desktop], fix: ["claude-desktop"],
                           checkedAt: finished.addingTimeInterval(20))
            let now = finished.addingTimeInterval(60)
            Harness.expect(McpClientAlert.shouldInterrupt(
                h, lastUpdate: update(.updated, at: finished), now: now, alreadyShown: false),
                "right after an update")
            Harness.expect(!McpClientAlert.shouldInterrupt(
                h, lastUpdate: update(.updated, at: finished), now: now, alreadyShown: true),
                "only once")
        }

        Harness.test("it does not interrupt at login, after a no-op, or on a stale answer") {
            let h = health([desktop], fix: ["claude-desktop"],
                           checkedAt: finished.addingTimeInterval(20))
            Harness.expect(!McpClientAlert.shouldInterrupt(
                h, lastUpdate: nil, now: finished, alreadyShown: false), "no update: login")
            Harness.expect(!McpClientAlert.shouldInterrupt(
                h, lastUpdate: update(.updated, at: finished),
                now: finished.addingTimeInterval(3600), alreadyShown: false), "an hour later")
            Harness.expect(!McpClientAlert.shouldInterrupt(
                h, lastUpdate: update(.noOp, at: finished),
                now: finished.addingTimeInterval(60), alreadyShown: false), "nothing updated")
            let stale = health([desktop], fix: ["claude-desktop"],
                               checkedAt: finished.addingTimeInterval(-60))
            Harness.expect(!McpClientAlert.shouldInterrupt(
                stale, lastUpdate: update(.updated, at: finished),
                now: finished.addingTimeInterval(60), alreadyShown: false),
                "checked before the update: may describe what it replaced")
            Harness.expect(!McpClientAlert.shouldInterrupt(
                health([], fix: [], checkedAt: finished.addingTimeInterval(20)),
                lastUpdate: update(.updated, at: finished),
                now: finished.addingTimeInterval(60), alreadyShown: false), "nothing wrong")
        }
    }
}
