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

@Test("resolving an unknown simulator udid returns nil rather than throwing")
func unknownSimulatorResolvesToNil() {
    MediaLog.silenced {
        #expect(PrivateFrameworks.resolveDevice(udid: "00000000-0000-0000-0000-000000000000") == nil)
    }
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
