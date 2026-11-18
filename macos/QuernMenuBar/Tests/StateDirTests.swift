// The app reads state where its `quern set-...` children write it (#721).

import Foundation

enum StateDirTests {
    static let home = URL(fileURLWithPath: "/Users/u", isDirectory: true)

    static func all() {
        Harness.test("no QUERN_STATE_DIR reads ~/.quern") {
            let got = StateReader.stateDir(environment: [:], home: home)
            Harness.expect(got.path, "/Users/u/.quern", "state dir")
        }

        Harness.test("QUERN_STATE_DIR is where the app reads, as the CLI writes") {
            // The #721 shape: the CLI wrote here and the app read ~/.quern.
            let got = StateReader.stateDir(
                environment: ["QUERN_STATE_DIR": "/scratch/live"], home: home)
            Harness.expect(got.path, "/scratch/live", "state dir")
        }

        Harness.test("an empty QUERN_STATE_DIR is unset, as in server/config.py") {
            let got = StateReader.stateDir(
                environment: ["QUERN_STATE_DIR": ""], home: home)
            Harness.expect(got.path, "/Users/u/.quern", "state dir")
        }
    }
}
