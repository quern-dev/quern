import Foundation

/// A booted simulator, as `simctl list devices booted -j` reports it.
///
/// In this package for the reason `JPEGFraming` is: the preview app
/// (tools/ios-preview) compiles this file into its single `swiftc` build, and
/// here it can have tests. Nothing in `quern-media` itself uses it.
public struct BootedSimulator: Equatable {
    public let udid: String
    public let name: String
    /// "iOS 26.5", from the runtime identifier.
    public let runtime: String

    public init(udid: String, name: String, runtime: String) {
        self.udid = udid
        self.name = name
        self.runtime = runtime
    }

    public var menuTitle: String { "\(name) (\(runtime))" }
}

public enum SimulatorList {
    /// The booted simulators in `simctl list devices booted -j` output,
    /// sorted by name. Nil when the output is not that shape -- "could not
    /// read the answer" is not "nothing is booted".
    ///
    /// `{"devices": {"<runtime id>": [{"udid", "name", "state"}, ...]}}`.
    /// Filtered on `state` as well, because `booted` is a filter `simctl`
    /// applies, and a list read some other way should still mean the same.
    public static func parse(_ data: Data) -> [BootedSimulator]? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let runtimes = json["devices"] as? [String: Any] else { return nil }
        var found: [BootedSimulator] = []
        for (runtimeID, devices) in runtimes {
            for device in devices as? [[String: Any]] ?? [] {
                guard device["state"] as? String == "Booted",
                      let udid = device["udid"] as? String,
                      let name = device["name"] as? String else { continue }
                found.append(BootedSimulator(
                    udid: udid, name: name, runtime: runtimeLabel(runtimeID)
                ))
            }
        }
        return found.sorted { ($0.name, $0.udid) < ($1.name, $1.udid) }
    }

    /// "com.apple.CoreSimulator.SimRuntime.iOS-26-5" -> "iOS 26.5". An
    /// identifier of any other shape comes back as its last component.
    public static func runtimeLabel(_ id: String) -> String {
        let tail = id.split(separator: ".").last.map(String.init) ?? id
        guard let dash = tail.firstIndex(of: "-") else { return tail }
        let platform = tail[..<dash]
        let version = tail[tail.index(after: dash)...].replacingOccurrences(of: "-", with: ".")
        return "\(platform) \(version)"
    }
}
