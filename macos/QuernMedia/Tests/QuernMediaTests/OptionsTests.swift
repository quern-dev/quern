import Foundation
import Testing
@testable import QuernMedia

@Test("a minimal simulator invocation parses")
func minimalSimulator() throws {
    let o = try OptionsParser.parse(["--sim-udid", "ABC-123"])
    #expect(o.source == .simulator(udid: "ABC-123"))
    #expect(o.codec == .mjpeg)
    #expect(o.window)
    #expect(o.servePort == nil)
    #expect(o.fps == 15)
    #expect(o.maxDimension == 900)
}

@Test("a headless streaming invocation parses")
func headlessStream() throws {
    let o = try OptionsParser.parse([
        "--device", "iPhone 11", "--serve", "8424", "--no-window", "--fps", "60",
    ])
    #expect(o.source == .device(match: "iPhone 11"))
    #expect(o.servePort == 8424)
    #expect(o.window == false)
    #expect(o.fps == 60)
}

@Test("an unknown flag is refused rather than ignored")
func unknownFlagIsRefused() {
    // The spike ignored anything it did not recognise, so `--fp 30` silently
    // ran at the default rate and looked like the throttle was broken.
    #expect(throws: OptionsError.unknownFlag("--fp")) {
        try OptionsParser.parse(["--sim-udid", "X", "--fp", "30"])
    }
    #expect(throws: OptionsError.unknownFlag("--verbose")) {
        try OptionsParser.parse(["--sim-udid", "X", "--verbose"])
    }
}

@Test("recording implies H.264 without being asked")
func recordImpliesH264() throws {
    // An .mp4 wants a video codec; a caller should not need to know that.
    let o = try OptionsParser.parse(["--sim-udid", "X", "--record", "/tmp/out.mp4"])
    #expect(o.codec == .h264)
    #expect(o.recordPath == "/tmp/out.mp4")
}

@Test("--h264 alone switches codec without recording")
func h264WithoutRecording() throws {
    let o = try OptionsParser.parse(["--sim-udid", "X", "--serve", "9000", "--h264"])
    #expect(o.codec == .h264)
    #expect(o.recordPath == nil)
}

@Test("exactly one source is required", arguments: [
    ([], OptionsError.noSource),
    (["--serve", "8422"], OptionsError.noSource),
    (["--sim-udid", "A", "--device", "B"], OptionsError.conflictingSources),
])
func sourceValidation(args: [String], expected: OptionsError) {
    #expect(throws: expected) { try OptionsParser.parse(args) }
}

@Test("a flag where a value belongs is a missing value, not an empty one")
func flagInValuePosition() {
    // `--sim-udid --no-window` should not silently take "--no-window" as a udid.
    #expect(throws: OptionsError.missingValue("--sim-udid")) {
        try OptionsParser.parse(["--sim-udid", "--no-window"])
    }
    #expect(throws: OptionsError.missingValue("--fps")) {
        try OptionsParser.parse(["--sim-udid", "X", "--fps"])
    }
}

@Test("unparseable numbers are reported with the offending value", arguments: [
    ("--fps", "fast"), ("--serve", "99999999"), ("--max-dim", "big"),
    ("--bitrate", "2M"), ("--quality", "high"),
])
func badNumbers(flag: String, value: String) {
    // "2M" is worth calling out: adb screenrecord accepts it, this does not,
    // and silently reading 0 would be worse than refusing.
    #expect(throws: OptionsError.badValue(flag: flag, value: value)) {
        try OptionsParser.parse(["--sim-udid", "X", "--serve", "8422", flag, value])
    }
}

@Test("an invocation that would do nothing is refused")
func nothingToDoIsRefused() {
    // No window, no server, no recording: the process would capture frames
    // and throw them away.
    #expect(throws: OptionsError.nothingToDo) {
        try OptionsParser.parse(["--sim-udid", "X", "--no-window"])
    }
}

@Test("help and list short-circuit before source validation", arguments: [
    (["--help"], OptionsError.help),
    (["-h"], OptionsError.help),
    (["--list"], OptionsError.list),
    (["--sim-udid", "X", "--help"], OptionsError.help),
])
func helpAndListShortCircuit(args: [String], expected: OptionsError) {
    // `--help` with no source must print help, not complain about the source.
    #expect(throws: expected) { try OptionsParser.parse(args) }
}

@Test("flag order does not matter")
func orderIndependent() throws {
    let a = try OptionsParser.parse(["--sim-udid", "X", "--h264", "--serve", "9000"])
    let b = try OptionsParser.parse(["--serve", "9000", "--h264", "--sim-udid", "X"])
    #expect(a == b)
}
