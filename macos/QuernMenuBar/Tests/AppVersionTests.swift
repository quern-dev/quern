// The app reports its own version, separately from the server's (#201).

import Foundation

enum AppVersionTests {
    static func all() {
        Harness.test("the version comes from the bundle's short version string") {
            Harness.expect(AppVersion.of(["CFBundleShortVersionString": "0.18.4"]), "0.18.4", "read")
            Harness.expect(AppVersion.of(["CFBundleShortVersionString": ""]), nil, "empty")
            Harness.expect(AppVersion.of(nil), nil, "no info")
        }

        Harness.test("the menu line names the app, not the server") {
            Harness.expect(AppVersion.menuLine("0.18.4"), "Quern app v0.18.4", "known")
            Harness.expect(AppVersion.menuLine(nil), "Quern app (version unknown)", "unknown")
        }

        Harness.test("Settings shows the app version in its own rows") {
            Harness.expect(SettingsModel.appRows(version: "0.18.4").first?.1, "0.18.4", "row")
            Harness.expect(SettingsModel.appRows(version: nil).first?.1, "unknown", "unknown")
        }
    }
}
