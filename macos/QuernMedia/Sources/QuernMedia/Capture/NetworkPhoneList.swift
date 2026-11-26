import Foundation

/// An iPhone paired with this Mac and reachable over the network only, as
/// `devicectl list devices --json-output` reports it.
///
/// In this package for the reason `SimulatorList` is: the preview app compiles
/// this file into its single `swiftc` build, and here it can have tests. The
/// server lists the same phones for a window it opens (`network_phones` in
/// server/device/media/preview.py); this is the standalone app's own reading,
/// and the two apply the same rules.
public struct NetworkPhone: Equatable {
    /// The CoreDevice identifier: quern's id for the phone.
    public let key: String
    /// The hardware UDID, which the phone bridge names it by.
    public let hardwareUDID: String
    public let name: String
    /// "iOS 27.2.1", or "" when devicectl gives none.
    public let osVersion: String
    /// Why its screen cannot be mirrored, or nil when it can.
    public let unavailable: String?

    public init(key: String, hardwareUDID: String, name: String, osVersion: String,
                unavailable: String?) {
        self.key = key
        self.hardwareUDID = hardwareUDID
        self.name = name
        self.osVersion = osVersion
        self.unavailable = unavailable
    }
}

public enum NetworkPhoneList {
    /// The first iOS whose display service streams the screen.
    public static let minimumIOS = 27

    /// The phones on the network in devicectl's JSON, sorted by name as Finder does, each
    /// with why it cannot be mirrored, if it cannot. Nil when the output is
    /// not that shape: "could not read the answer" is not "no phones".
    ///
    /// A phone is one devicectl calls `physical` and `paired`, reached over
    /// `localNetwork` (a USB phone is a capture device, which the app finds
    /// itself), with a tunnel that is not `unavailable`.
    ///
    /// `bridgeMissing`, when given, is why the phone bridge cannot be run on
    /// this Mac; it greys out every phone that could otherwise be mirrored.
    public static func parse(_ data: Data, bridgeMissing: String? = nil) -> [NetworkPhone]? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let result = json["result"] as? [String: Any],
              let devices = result["devices"] as? [[String: Any]] else { return nil }
        var found: [NetworkPhone] = []
        for device in devices {
            let connection = device["connectionProperties"] as? [String: Any] ?? [:]
            let hardware = device["hardwareProperties"] as? [String: Any] ?? [:]
            let properties = device["deviceProperties"] as? [String: Any] ?? [:]
            guard hardware["reality"] as? String == "physical",
                  connection["pairingState"] as? String == "paired",
                  connection["transportType"] as? String == "localNetwork",
                  (connection["tunnelState"] as? String ?? "unavailable") != "unavailable",
                  let key = device["identifier"] as? String, !key.isEmpty
            else { continue }
            let hardwareUDID = hardware["udid"] as? String ?? ""
            let version = properties["osVersionNumber"] as? String ?? ""
            let os = version.isEmpty ? "" : "iOS \(version)"
            let why: String?
            if hardwareUDID.isEmpty {
                why = "its hardware UDID is unknown, so it cannot be named to the stream"
            } else if let major = majorVersion(version), major < minimumIOS {
                why = "needs iOS \(minimumIOS) or later (this one runs \(os))"
            } else {
                why = bridgeMissing
            }
            found.append(NetworkPhone(
                key: key, hardwareUDID: hardwareUDID,
                name: properties["name"] as? String ?? key, osVersion: os, unavailable: why
            ))
        }
        // As Finder sorts names: "iPhone 11" before "J iPad Air", which a
        // plain comparison puts the other way round.
        return found.sorted {
            let order = $0.name.localizedStandardCompare($1.name)
            return order == .orderedSame ? $0.key < $1.key : order == .orderedAscending
        }
    }

    static func majorVersion(_ version: String) -> Int? {
        Int(version.split(separator: ".", maxSplits: 1).first ?? "")
    }
}
