// The menu-bar app's own version (#201).
//
// The Settings row labelled Version is the *server's*. The app never said what
// it was, so on a git install -- where `quern update` does not replace it -- an
// app several releases behind looked exactly like a current one.

import Foundation

enum AppVersion {
    /// From the bundle's Info.plist, which build.sh stamps.
    static func of(_ info: [String: Any]?) -> String? {
        guard let v = info?["CFBundleShortVersionString"] as? String, !v.isEmpty else {
            return nil
        }
        return v
    }

    static var current: String? { of(Bundle.main.infoDictionary) }

    /// "Menu bar v0.18.4", or a plain label when the bundle has no version --
    /// which is how an unbundled test binary runs.
    static func menuLine(_ version: String?) -> String {
        version.map { "Menu bar v\($0)" } ?? "Menu bar (version unknown)"
    }
}
