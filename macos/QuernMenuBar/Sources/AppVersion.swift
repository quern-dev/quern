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

    /// "Quern app v0.18.4", or a plain label when the bundle has no version --
    /// which is how an unbundled test binary runs.
    ///
    /// "Quern app", not "Menu bar" or "Helper": it distinguishes this from the
    /// *server*, which is the version the row above it reports, without
    /// claiming to be a background helper -- on macOS a "Helper" is launched
    /// by a main app, which here would be the daemon, not this.
    static func menuLine(_ version: String?) -> String {
        version.map { "Quern app v\($0)" } ?? "Quern app (version unknown)"
    }
}
