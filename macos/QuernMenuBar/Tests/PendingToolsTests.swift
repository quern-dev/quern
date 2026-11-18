// Required tool upgrades an update could not finish (#725): read from
// pending-tool-upgrades.json, offered as "Finish Update in Terminal…".

import Foundation

enum PendingToolsTests {
    static let sudo: [String: Any] = [
        "checked_at": "2026-11-17T21:00:00+00:00",
        "pending": [[
            "name": "pymobiledevice3", "role": "cli", "current": "11.14.0",
            "floor": "11.55.0", "reason": "below the 11.55.0 quern requires",
            "command": ["sudo", "pipx", "upgrade", "--global", "pymobiledevice3"],
            "needs_root": true,
        ]],
    ]

    static func all() {
        Harness.test("the record is read as the updater writes it") {
            let got = StateReader.readPendingTools(contents: .some(sudo))
            Harness.expect(got, [PendingTool(
                name: "pymobiledevice3", current: "11.14.0", floor: "11.55.0",
                command: ["sudo", "pipx", "upgrade", "--global", "pymobiledevice3"],
                needsRoot: true)], "pending")
        }

        Harness.test("no record, or one that makes no sense, is nothing owed") {
            Harness.expect(StateReader.readPendingTools(contents: .some(nil)), [], "absent")
            Harness.expect(StateReader.readPendingTools(contents: .some(["pending": "x"])), [],
                           "not a list")
            Harness.expect(StateReader.readPendingTools(contents: .some(["pending": [["floor": "1"]]])),
                           [], "an entry with no name")
        }

        Harness.test("nothing owed, no menu item") {
            Harness.expect(PendingToolsMenu.title([]), nil, "title")
        }

        Harness.test("one sudo upgrade names the tool and why") {
            let pending = StateReader.readPendingTools(contents: .some(sudo))
            Harness.expect(PendingToolsMenu.title(pending),
                           "Finish Update in Terminal… (pymobiledevice3 needs sudo)", "title")
        }

        Harness.test("an upgrade that ran and is still behind says so, not sudo") {
            let stuck = PendingTool(name: "pymobiledevice3", current: "11.14.0",
                                    floor: "11.55.0", command: ["pipx", "upgrade",
                                                                "pymobiledevice3"])
            Harness.expect(PendingToolsMenu.title([stuck]),
                           "Finish Update in Terminal… (pymobiledevice3 still below quern's minimum)",
                           "title")
        }

        Harness.test("several are counted") {
            let a = PendingTool(name: "a", needsRoot: true)
            let b = PendingTool(name: "b", needsRoot: true)
            Harness.expect(PendingToolsMenu.title([a, b]),
                           "Finish Update in Terminal… (2 tools need sudo)", "title")
        }

        Harness.test("the Terminal script runs a plain quern update, not --tools") {
            // A plain update runs the required upgrades, asking sudo where it
            // needs to; --tools would also apply every merely newer one.
            let script = TerminalUpdate.script(quern: "/u/.local/bin/quern",
                                               reason: .requiredTools(["pymobiledevice3"]))
            Harness.expect(script.contains("'/u/.local/bin/quern' update\n"), true, "plain update")
            Harness.expect(script.contains("--tools"), false, "no --tools")
            Harness.expect(script.contains("pymobiledevice3 below quern's minimum"), true, "why")
        }
    }
}
