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
        reason: "Claude Desktop is set to run /Users/u/.nvm/versions/node/v22.1.0/bin/node, "
            + "which no longer exists",
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
                             ["client": "Claude Code (/p)", "reason": "r2", "fix": "f2",
                              "fixable": false],
                             ["client": "no reason"]],
            ])
            Harness.expect(h.problems, [
                McpClientProblem(client: "Claude Desktop", reason: "r", fix: "f"),
                McpClientProblem(client: "Claude Code (/p)", reason: "r2", fix: "f2",
                                 fixable: false),
            ], "the well-formed ones, fixable read")
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
            Harness.expect(body.contains("Claude Code (/src/app): `quern mcp-install` does not "
                                         + "write project entries"),
                           "the fix mcp-install cannot make")
            Harness.expect(body.contains("says so if it does not"),
                           "no promise it cannot keep when there is no Node 22")
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

        Harness.test("a long list is cut short, and project fixes are said once") {
            let many = (0..<7).map { McpClientProblem(client: "Claude Code (/p\($0))",
                                                      reason: "reason \($0)", fix: "f",
                                                      fixable: false) }
            let body = McpClientAlert.body(health(many, fix: []))
            Harness.expect(body.contains("reason 3") && !body.contains("reason 4"), "four listed")
            Harness.expect(body.contains("And 3 more."), "the rest summed")
            Harness.expect(body.components(separatedBy: "does not write project entries").count,
                           2, "one project line, however many")
        }

        let h = health([desktop], fix: ["claude-desktop"], checkedAt: finished.addingTimeInterval(20))
        let started = finished.addingTimeInterval(-90)
        func interrupts(_ h: McpClientHealth, _ update: UpdateResult?, started: Date?,
                        now: Date, shown: Bool = false) -> Bool {
            McpClientAlert.shouldInterrupt(h, lastUpdate: update, menuUpdateStartedAt: started,
                                           now: now, alreadyShown: shown)
        }

        Harness.test("it interrupts right after a menu update, once, on a fresh answer") {
            let now = finished.addingTimeInterval(60)
            Harness.expect(interrupts(h, update(.updated, at: finished), started: started, now: now),
                           "right after an update from the menu")
            Harness.expect(!interrupts(h, update(.updated, at: finished), started: started,
                                       now: now, shown: true), "only once")
        }

        Harness.test("it does not interrupt without the menu's marker, or late, or on a stale answer") {
            let now = finished.addingTimeInterval(60)
            Harness.expect(!interrupts(h, update(.updated, at: finished), started: nil, now: now),
                           "an update run in a terminal: no marker")
            Harness.expect(!interrupts(h, update(.updated, at: finished),
                                       started: finished.addingTimeInterval(30), now: now),
                           "a marker from after that update: some other attempt")
            Harness.expect(!interrupts(h, update(.updated, at: finished),
                                       started: finished.addingTimeInterval(-3600), now: now),
                           "a marker an hour older than the update: left from another attempt")
            Harness.expect(!interrupts(h, update(.updated, at: finished), started: started,
                                       now: finished.addingTimeInterval(3600)), "an hour later")
            Harness.expect(!interrupts(h, update(.noOp, at: finished), started: started, now: now),
                           "nothing updated")
            Harness.expect(!interrupts(h, update(.failed, at: finished), started: started, now: now),
                           "a failed update has its own alert")
            let stale = health([desktop], fix: ["claude-desktop"],
                               checkedAt: finished.addingTimeInterval(-60))
            Harness.expect(!interrupts(stale, update(.updated, at: finished), started: started,
                                       now: now), "checked before the update finished")
            Harness.expect(!interrupts(health([], fix: [], checkedAt: finished.addingTimeInterval(20)),
                                       update(.updated, at: finished), started: started, now: now),
                           "nothing wrong")
        }

        Harness.test("the marker is read once, then gone") {
            let defaults = UserDefaults(suiteName: "quern-tests-\(UUID().uuidString)")!
            Harness.expect(MenuUpdateMarker.consume(defaults: defaults), nil, "none yet")
            MenuUpdateMarker.record(finished, defaults: defaults)
            Harness.expect(MenuUpdateMarker.consume(defaults: defaults), finished, "the time")
            Harness.expect(MenuUpdateMarker.consume(defaults: defaults), nil,
                           "consumed: a reopen or a login finds nothing")
            MenuUpdateMarker.record(finished, defaults: defaults)
            MenuUpdateMarker.clear(defaults: defaults)
            Harness.expect(MenuUpdateMarker.consume(defaults: defaults), nil,
                           "cleared when the update ended without a relaunch")
        }
    }
}
