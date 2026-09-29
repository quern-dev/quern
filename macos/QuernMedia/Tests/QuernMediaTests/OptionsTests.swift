import Foundation
import Testing
@testable import QuernMedia

@Test("a minimal simulator invocation parses")
func minimalSimulator() throws {
    let o = try OptionsParser.parse(["--sim-udid", "ABC-123", "--serve", "8422"])
    #expect(o.source == .simulator(udid: "ABC-123"))
    #expect(o.codec == .mjpeg)
    #expect(o.servePort == 8422)
    #expect(o.fps == 15)
    #expect(o.maxDimension == 900)
}

@Test("a headless streaming invocation parses")
func headlessStream() throws {
    let o = try OptionsParser.parse([
        "--device", "iPhone 11", "--serve", "8424", "--fps", "60",
    ])
    #expect(o.source == .device(match: "iPhone 11"))
    #expect(o.servePort == 8424)
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
        try OptionsParser.parse(["--sim-udid", "X", "--serve", "8422", "--fps"])
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
        try OptionsParser.parse(["--sim-udid", "X", "--record", "/tmp/x.mp4", flag, value])
    }
}

@Test("an invocation with no output is refused")
func noOutputIsRefused() {
    // This tool is a headless producer. With neither a server nor a
    // recording it would capture frames and discard them. Showing a window
    // is the preview app's job, not this one's.
    #expect(throws: OptionsError.noOutput) {
        try OptionsParser.parse(["--sim-udid", "X"])
    }
}

@Test("--no-window is gone, not silently accepted")
func noWindowFlagRemoved() {
    // It used to be meaningful. Accepting and ignoring it would leave a
    // caller believing they had suppressed a window that never existed.
    #expect(throws: OptionsError.unknownFlag("--no-window")) {
        try OptionsParser.parse(["--sim-udid", "X", "--serve", "8422", "--no-window"])
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

@Test("a short flag in a value position is a missing value, not an operand")
func shortFlagIsNotAnOperand() {
    // Only the flags the parser actually knows are refused here. `-l` is not
    // one of them, so it stays a legitimate operand.
    let args = ["--sim-udid", "X", "--record", "-h"]
    // The guard tested `hasPrefix("--")`, which waves `-h` straight through:
    // `--record -h` stored "-h" as the output path and help never ran. The
    // flag sets are the authority on what is a flag, not the spelling.
    #expect(throws: OptionsError.missingValue("--record")) {
        try OptionsParser.parse(args)
    }
}

@Test("a value flag in a value position is refused too")
func valueFlagIsNotAnOperand() {
    #expect(throws: OptionsError.missingValue("--record")) {
        try OptionsParser.parse(["--sim-udid", "X", "--record", "--serve", "8422"])
    }
}

@Test("an unusable frame rate is refused rather than trapping", arguments: [
    "1e308", "0", "-5", "nan", "inf", "100000",
])
func absurdFrameRatesAreRefused(raw: String) {
    // `Int(expectedFPS * 2)` traps on a non-finite or huge value, and the
    // encoder is where that lands. argv is where it comes from, and the
    // parser is the only layer that can name the flag that was wrong.
    #expect(throws: OptionsError.self) {
        try OptionsParser.parse(["--sim-udid", "X", "--serve", "8422", "--fps", raw])
    }
}

@Test("an ordinary frame rate still parses")
func sensibleFrameRateParses() throws {
    let options = try OptionsParser.parse(
        ["--sim-udid", "X", "--serve", "8422", "--fps", "60"]
    )
    #expect(options.fps == 60)
}

@Test("a quality outside the documented range is refused", arguments: [
    "1.5", "-0.1", "nan", "inf", "1e308",
])
func qualityOutsideRangeIsRefused(raw: String) {
    // Documented as 0...1 and passed straight to VTSessionSetProperty, whose
    // result nothing checks — so out of range was accepted in silence and
    // simply did not do what was asked.
    #expect(throws: OptionsError.self) {
        try OptionsParser.parse(["--sim-udid", "X", "--serve", "8422", "--quality", raw])
    }
}

@Test("the documented quality bounds are accepted", arguments: ["0", "0.6", "1"])
func qualityInRangeIsAccepted(raw: String) throws {
    let options = try OptionsParser.parse(
        ["--sim-udid", "X", "--serve", "8422", "--quality", raw]
    )
    #expect(options.quality == Double(raw))
}

@Test("values that reach an unchecked API are refused", arguments: [
    ["--bitrate", "0"], ["--bitrate", "-1"], ["--max-dim", "-1"], ["--serve", "0"],
])
func unusableNumericValuesAreRefused(pair: [String]) {
    // Each of these goes to VTSessionSetProperty or to a bind, and nothing
    // reads the result -- so out of range was accepted in silence and did
    // something other than what was asked: --bitrate 0 ran at VideoToolbox's
    // default, --serve 0 bound an ephemeral port and advertised :0.
    #expect(throws: OptionsError.self) {
        try OptionsParser.parse(["--sim-udid", "X", "--serve", "8422"] + pair)
    }
}

@Test("max-dim 0 still means native")
func maxDimZeroIsNative() throws {
    let options = try OptionsParser.parse(
        ["--sim-udid", "X", "--serve", "8422", "--max-dim", "0"]
    )
    #expect(options.maxDimension == 0)
}
