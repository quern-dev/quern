import Foundation
import Testing
@testable import QuernMedia

private func device(
    _ id: String, name: String, os: String = "27.2.1", transport: String = "localNetwork",
    tunnel: String = "connected", paired: String = "paired", reality: String = "physical",
    udid: String? = "00008030-HW"
) -> [String: Any] {
    var hardware: [String: Any] = ["reality": reality]
    if let udid { hardware["udid"] = udid }
    return [
        "identifier": id,
        "connectionProperties": [
            "transportType": transport, "tunnelState": tunnel, "pairingState": paired,
        ],
        "deviceProperties": ["name": name, "osVersionNumber": os],
        "hardwareProperties": hardware,
    ]
}

private func listing(_ devices: [[String: Any]]) throws -> Data {
    try JSONSerialization.data(withJSONObject: ["result": ["devices": devices]])
}

@Test("only paired, physical phones on the network are listed, sorted by name")
func onlyNetworkPhones() throws {
    let phones = try #require(NetworkPhoneList.parse(listing([
        device("B", name: "iPhone 11"),
        device("A", name: "J iPad Air"),
        device("USB", name: "Plugged in", transport: "wired"),
        device("SIM", name: "Simulator", transport: "sameMachine", reality: "simulated"),
        device("AWAY", name: "Unreachable", tunnel: "unavailable"),
        device("NEW", name: "Unpaired", paired: "unpaired"),
    ])))
    #expect(phones.map(\.key) == ["B", "A"])
    #expect(phones[0] == NetworkPhone(
        key: "B", hardwareUDID: "00008030-HW", name: "iPhone 11",
        osVersion: "iOS 27.2.1", unavailable: nil
    ))
}

@Test("a phone that cannot be mirrored is listed with the reason")
func reasons() throws {
    let phones = try #require(NetworkPhoneList.parse(listing([
        device("OLD", name: "a old", os: "26.5"),
        device("NOHW", name: "b no udid", udid: nil),
        device("OK", name: "c fine"),
    ]), bridgeMissing: "start quern once"))
    #expect(phones[0].unavailable == "needs iOS 27 or later (this one runs iOS 26.5)")
    #expect(phones[1].unavailable?.contains("hardware UDID") == true)
    #expect(phones[2].unavailable == "start quern once",
            "a missing bridge greys out the phone that would otherwise open")
}

@Test("an answer that is not devicectl's is nil, not no phones")
func notTheShape() {
    #expect(NetworkPhoneList.parse(Data("not json".utf8)) == nil)
    #expect(NetworkPhoneList.parse(Data("{\"result\":{}}".utf8)) == nil)
    #expect(NetworkPhoneList.parse(Data("{\"result\":{\"devices\":[]}}".utf8)) == [])
}
