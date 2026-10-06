import Foundation

/// What a refused stream means, in words a person reads under OFF AIR.
///
/// The preview app's stream client reads the body of a response other than
/// 200, because quern-media puts the reason there. In this package for the
/// reason `JPEGFraming` is: the app (tools/ios-preview) compiles this file
/// into its single `swiftc` build, and here it can have tests.
public enum StreamRefusal {
    public static func describe(code: Int, body: Data) -> String {
        let said = String(decoding: body.prefix(1024), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        switch code {
        case 503 where !said.isEmpty:
            // quern-media is up and its source is not: "the simulator is
            // shutdown", or whichever state it is in.
            return said
        case 409:
            return "this port is streaming another simulator now"
                + (said.isEmpty ? "" : " (\(said))")
        default:
            // Including an empty 503: that is all that is known.
            return "quern-media answered HTTP \(code)" + (said.isEmpty ? "" : ": \(said)")
        }
    }

    /// The part of a reason to show in a window: up to any parenthesis. A
    /// 409's names two UDIDs, which wrap past two lines in a phone-shaped
    /// window and pushed the reason out of view; the status keeps it whole.
    public static func short(_ reason: String) -> String {
        guard let paren = reason.range(of: " (") else { return reason }
        return String(reason[..<paren.lowerBound])
    }
}
