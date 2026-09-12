// What the Settings window reports about the server's version.
//
// The row used to fall back to `current_version` from update-info.json when a
// live read failed. That file is only rewritten by the server, and only when it
// runs an update check, so on a machine where the CLI could not be found it
// showed a version from days earlier under a heading that says "Server". It
// read 0.15.0 for an 0.16.1 install and was believed.

import Foundation

enum SettingsModelTests {
    private static func row(_ label: String, in rows: [(String, String)]) -> String {
        rows.first { $0.0 == label }?.1 ?? "<no \(label) row>"
    }

    /// Drives the real decision. The first version of this file rebuilt the
    /// logic inside the rig, so every mutation of `apply(version:)` passed.
    private static func model(answering versions: [String?]) -> (SettingsModel, () -> Void) {
        var queue = versions
        let model = SettingsModel()
        let refresh = {
            let next = queue.isEmpty ? nil : queue.removeFirst()
            model.apply(version: next)
        }
        return (model, refresh)
    }

    static func all() {
        Harness.test("before anything is read, it says so rather than guessing") {
            let model = SettingsModel()
            Harness.expect(model.version, .pending, "initial reading")
            Harness.expect(model.version.display, "checking…", "initial display")
        }

        Harness.test("a live read is shown as itself") {
            let (model, refresh) = self.model(answering: ["0.16.1"])
            refresh()
            Harness.expect(model.version, .live("0.16.1"), "reading")
            Harness.expect(model.version.display, "0.16.1", "display")
        }

        Harness.test("a failed first read is unavailable, never a cached value") {
            // The defect: this showed update-info.json's current_version, which
            // is a different question answered at a different time.
            let (model, refresh) = self.model(answering: [nil])
            refresh()
            Harness.expect(model.version, .unavailable, "reading")
            Harness.expect(model.version.display, "unavailable", "display")
            Harness.expect(model.version.display != "0.15.0",
                           "must not present a stale cache as the server's version")
        }

        Harness.test("a transient failure holds the last live reading") {
            // Mid-update the CLI is briefly unrunnable. Blanking the field then
            // is a worse reading than a slightly old one -- but only because
            // this app read that value itself.
            let (model, refresh) = self.model(answering: ["0.16.1", nil])
            refresh()
            refresh()
            Harness.expect(model.version, .live("0.16.1"), "should hold the last live value")
        }

        Harness.test("a later live read replaces an earlier one") {
            let (model, refresh) = self.model(answering: ["0.16.1", "0.17.0"])
            refresh()
            refresh()
            Harness.expect(model.version, .live("0.17.0"), "reading")
        }

        Harness.test("the version row never shows anything but a live reading") {
            // The defect lived in the view, not the model: the row fell back to
            // update-info.json's current_version. Testing the model alone still
            // passed with that fallback reinstated, measured -- so the row
            // itself has to be what is asserted.
            let model = SettingsModel()
            var snap = QuernSnapshot()
            snap.update.currentVersion = "0.15.0"   // the stale cache
            snap.server.running = true
            model.apply(snap)

            model.apply(version: nil)               // live read fails
            let unavailable = row("Version", in: model.serverRows(now: Date()))
            Harness.expect(unavailable, "unavailable", "with no live reading")
            Harness.expect(unavailable != "0.15.0",
                           "the cache must never reach the Server version row")

            model.apply(version: "0.16.1")
            Harness.expect(row("Version", in: model.serverRows(now: Date())), "0.16.1",
                           "with a live reading")
        }

        Harness.test("uptime is formatted from the snapshot, not the wall clock") {
            let start = Date(timeIntervalSince1970: 1_000_000)
            let cases: [(TimeInterval, String)] = [
                (5, "5s"), (90, "1m"), (3700, "1h 1m"), (90000, "1d 1h"),
            ]
            for (elapsed, expected) in cases {
                let got = SettingsModel.uptime(since: start,
                                               now: start.addingTimeInterval(elapsed))
                Harness.expect(got, expected, "after \(Int(elapsed))s")
            }
            Harness.expect(SettingsModel.uptime(since: nil, now: Date()), "—", "no start time")
        }

        Harness.test("a read that cannot answer reaches the row as unavailable") {
            // Crosses the seam the other tests skip. `apply(version:)` was
            // tested directly, so its only production caller was not -- and a
            // `guard let version else { return }` reinstated inside
            // refreshInstalledVersion passed every test while parking the row
            // on "checking…" forever.
            let model = SettingsModel()
            model.readVersion = { done in done(nil, "could not find quern") }

            model.refreshInstalledVersion()

            Harness.expect(model.version, .unavailable, "reading after a failed read")
            Harness.expect(row("Version", in: model.serverRows(now: Date())), "unavailable",
                           "the row must not sit on checking…")
        }

        Harness.test("a read that answers reaches the row as the version") {
            let model = SettingsModel()
            model.readVersion = { done in done("0.16.1", "quern 0.16.1") }

            model.refreshInstalledVersion()

            Harness.expect(row("Version", in: model.serverRows(now: Date())), "0.16.1", "row")
        }

        Harness.test("a transient failure holds the last live reading, through the seam") {
            // The other seam tests only reach `apply`'s trivial branches, so a
            // refresh that bypassed `apply` entirely --
            //   self?.version = version.map(VersionReading.live) ?? .unavailable
            // -- passed all 29 while destroying the one behaviour the
            // three-state enum exists for.
            let model = SettingsModel()
            var answers: [String?] = ["0.16.1", nil]
            model.readVersion = { done in
                let next = answers.isEmpty ? nil : answers.removeFirst()
                done(next, "")
            }

            model.refreshInstalledVersion()
            model.refreshInstalledVersion()

            Harness.expect(model.version, .live("0.16.1"),
                           "a failure after a good reading must not blank the row")
            Harness.expect(row("Version", in: model.serverRows(now: Date())), "0.16.1", "row")
        }

        Harness.test("pending and unavailable are not the same state") {
            Harness.expect(SettingsModel.VersionReading.pending
                             != SettingsModel.VersionReading.unavailable,
                           "not asked yet and asked-but-could-not-tell must differ")
        }
    }
}
