import CoreVideo
import Foundation
import Testing
@testable import QuernMedia

@Test("four-character codes render readably", arguments: [
    (kCVPixelFormatType_32BGRA, "BGRA"),
    (kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, "420v"),
    (kCVPixelFormatType_420YpCbCr8BiPlanarFullRange, "420f"),
])
func fourCCStrings(code: OSType, expected: String) {
    // The pixel format is the difference between a correct frame and garbage,
    // so it gets logged -- and a log line reading "875704438" helps nobody.
    #expect(FourCC.string(code) == expected)
}

@Test("simulator device states have names")
func simDeviceStateNames() {
    #expect(SimDeviceState(rawValue: 3)?.name == "booted")
    #expect(SimDeviceState(rawValue: 1)?.name == "shutdown")
    #expect(SimDeviceState(rawValue: 99) == nil)
}

@Test("capture device discovery is safe with nothing attached")
func discoveryDoesNotRequireADevice() {
    // Returns whatever is plugged in, including nothing. The point is that it
    // does not throw or hang on a machine with no device -- CI has none.
    let found = CaptureDeviceDiscovery.devices()
    for device in found {
        #expect(device.modelID == CaptureDeviceDiscovery.screenCaptureModelID)
    }
}

/// Whether this machine has an Xcode with the private frameworks we need.
/// Checked by looking for the file rather than by trying to load it, so the
/// gate itself costs nothing.
private var hasSimulatorFrameworks: Bool {
    FileManager.default.fileExists(
        atPath: "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator"
    ) && PrivateFrameworks.hasSimulatorKit(at: PrivateFrameworks.developerDir())
}

@Test(
    "the private-framework path still enumerates simulators",
    .enabled(if: hasSimulatorFrameworks)
)
func enumerationActuallyWorks() {
    // The early-warning test for the riskiest part of this package: everything
    // in PrivateFrameworks is unsupported API reached by dlopen and selector
    // name, and a toolchain update is what breaks it.
    //
    // It has to assert enumeration *succeeds*. An earlier version asserted
    // only that an unknown udid resolves to nil -- which is true both when the
    // frameworks work and when they fail to load entirely, so it passed while
    // testing nothing and still paid the full CoreSimulator cost.
    //
    // Any machine with Xcode installed has simulator device types, so an empty
    // list here means the private path is broken rather than that the machine
    // is bare.
    let devices = MediaLog.silenced { PrivateFrameworks.availableDevices() }
    #expect(!devices.isEmpty,
            "SimServiceContext returned no devices — the private API path is broken")

    // And with enumeration known good, nil for an unknown udid means what it
    // is supposed to mean.
    let missing = MediaLog.silenced {
        PrivateFrameworks.resolveDevice(udid: "00000000-0000-0000-0000-000000000000")
    }
    #expect(missing == nil)
}

@Test("starting a framebuffer for an unknown udid reports device-not-found")
func framebufferRejectsUnknownDevice() {
    let source = SimulatorFramebuffer(udid: "not-a-real-udid") { _ in }
    MediaLog.silenced {
        #expect(throws: SimulatorFramebufferError.self) { try source.start() }
    }
}

@Test("the log handler can be replaced and restored")
func logHandlerIsSwappable() {
    var captured: [String] = []
    let previous = MediaLog.handler
    MediaLog.handler = { captured.append($0) }
    MediaLog.log("hello")
    MediaLog.handler = previous
    #expect(captured == ["hello"])
}
