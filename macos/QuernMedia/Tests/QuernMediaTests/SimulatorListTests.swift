import Foundation
import Testing
@testable import QuernMedia

// The preview app's Devices menu lists what this parses, so a simulator it
// drops is one nobody can pick.

private let sample = Data("""
{
  "devices" : {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-5" : [
      { "udid" : "6401A02A-FCAC-42E6-BE10-EB3AAD523A39", "name" : "iPhone 17 Pro",
        "state" : "Booted", "isAvailable" : true },
      { "udid" : "916893DB-184C-47A3-9837-5963F6869A81", "name" : "iPhone 16",
        "state" : "Shutdown", "isAvailable" : true }
    ],
    "com.apple.CoreSimulator.SimRuntime.iOS-18-6" : [
      { "udid" : "45395D76-AF20-4CEF-8966-9B1C43BF9475", "name" : "iPhone 16 Pro",
        "state" : "Booted" }
    ],
    "com.apple.CoreSimulator.SimRuntime.watchOS-11-0" : []
  }
}
""".utf8)

@Test("only booted simulators are listed, across every runtime, sorted by name")
func listsBootedSimulators() throws {
    let list = try #require(SimulatorList.parse(sample))
    #expect(list.map(\.name) == ["iPhone 16 Pro", "iPhone 17 Pro"])
    #expect(list.map(\.runtime) == ["iOS 18.6", "iOS 26.5"])
    #expect(list.first?.udid == "45395D76-AF20-4CEF-8966-9B1C43BF9475")
    #expect(list.last?.menuTitle == "iPhone 17 Pro (iOS 26.5)")
}

@Test("output that is not simctl's shape is unreadable, not empty", arguments: [
    "", "not json", "[]", "{}", #"{"devices": []}"#,
])
func unreadableOutputIsNil(text: String) {
    // An empty list would empty the menu on one bad read; nil keeps the last.
    #expect(SimulatorList.parse(Data(text.utf8)) == nil)
}

@Test("nothing booted is an empty list, not an unreadable one")
func nothingBootedIsEmpty() throws {
    let list = try #require(SimulatorList.parse(Data(#"{"devices": {"x": []}}"#.utf8)))
    #expect(list.isEmpty)
}

@Test("a device missing its udid or name is skipped, not trapped on")
func incompleteDevicesAreSkipped() throws {
    let text = #"{"devices": {"r": [{"state": "Booted", "name": "No UDID"}, {"state": "Booted", "udid": "U"}]}}"#
    let list = try #require(SimulatorList.parse(Data(text.utf8)))
    #expect(list.isEmpty)
}

@Test("runtime identifiers read as platform and version", arguments: [
    ("com.apple.CoreSimulator.SimRuntime.iOS-26-5", "iOS 26.5"),
    ("com.apple.CoreSimulator.SimRuntime.watchOS-11-0", "watchOS 11.0"),
    ("com.apple.CoreSimulator.SimRuntime.xrOS-2-0", "xrOS 2.0"),
    ("com.apple.CoreSimulator.SimRuntime.iOS-18-6-1", "iOS 18.6.1"),
    ("SomethingElse", "SomethingElse"),
])
func runtimeLabels(id: String, expected: String) {
    #expect(SimulatorList.runtimeLabel(id) == expected)
}
