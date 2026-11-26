import CoreMedia
import CoreVideo
import Foundation
import Testing
import VideoToolbox
@testable import QuernMedia

// MARK: - a real HEVC stream, made here

/// An HEVC encoder for the round trip, or nil on a machine without one -- a
/// CI virtual machine may have none, and then the decode tests are skipped
/// rather than failed.
private final class TestHEVCEncoder {
    private var session: VTCompressionSession?
    private var out: [CMSampleBuffer] = []

    init?(width: Int, height: Int) {
        var made: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_HEVC, encoderSpecification: nil,
            imageBufferAttributes: nil, compressedDataAllocator: nil, outputCallback: nil,
            refcon: nil, compressionSessionOut: &made)
        guard status == noErr, let made else { return nil }
        VTSessionSetProperty(made, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(made, key: kVTCompressionPropertyKey_AllowFrameReordering,
                             value: kCFBooleanFalse)
        session = made
    }

    deinit { if let session { VTCompressionSessionInvalidate(session) } }

    /// One frame of `surface`, as the access unit the server would write:
    /// the parameter sets first on a keyframe, as the phone sends them.
    func unit(_ surface: IOSurface, at seconds: Double) -> HEVCAccessUnit? {
        guard let session else { return nil }
        var pixels: Unmanaged<CVPixelBuffer>?
        CVPixelBufferCreateWithIOSurface(kCFAllocatorDefault, surface, nil, &pixels)
        guard let buffer = pixels?.takeRetainedValue() else { return nil }
        var encoded: CMSampleBuffer?
        let status = VTCompressionSessionEncodeFrame(
            session, imageBuffer: buffer,
            presentationTimeStamp: CMTime(seconds: seconds, preferredTimescale: 600),
            duration: .invalid, frameProperties: nil, infoFlagsOut: nil
        ) { status, _, sample in
            if status == noErr { encoded = sample }
        }
        VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
        guard status == noErr, let encoded else { return nil }
        return Self.accessUnit(encoded)
    }

    private static func accessUnit(_ sample: CMSampleBuffer) -> HEVCAccessUnit? {
        guard let format = CMSampleBufferGetFormatDescription(sample),
              let block = CMSampleBufferGetDataBuffer(sample) else { return nil }
        var nals: [Data] = []
        var count = 0
        CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
            format, parameterSetIndex: 0, parameterSetPointerOut: nil,
            parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil)
        for i in 0..<count {
            var pointer: UnsafePointer<UInt8>?
            var size = 0
            CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
                format, parameterSetIndex: i, parameterSetPointerOut: &pointer,
                parameterSetSizeOut: &size, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil)
            if let pointer { nals.append(Data(bytes: pointer, count: size)) }
        }
        var length = 0
        var raw: UnsafeMutablePointer<CChar>?
        CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil,
                                    totalLengthOut: &length, dataPointerOut: &raw)
        guard let raw else { return nil }
        let body = Data(bytes: raw, count: length)
        guard let units = try? HEVCFrameReader.units(body) else { return nil }
        return HEVCAccessUnit(nals: nals + units)
    }
}

private let hevcEncoderAvailable = TestHEVCEncoder(width: 64, height: 64) != nil

private func surface(_ w: Int, _ h: Int) throws -> IOSurface {
    try #require(TestSurface.make(width: w, height: h))
}

// MARK: - framing

@Test("an access unit read back is the one written, however the bytes arrive")
func framingRoundTrip() throws {
    let units = [
        HEVCAccessUnit(nals: [Data([0x40, 0x01, 9]), Data([0x42, 0x01, 8, 7]), Data([0x44, 0x01])]),
        HEVCAccessUnit(nals: [Data([0x26, 0x01, 1, 2, 3, 4, 5])]),
    ]
    let stream = units.reduce(Data()) { $0 + $1.framed() }

    var whole = HEVCFrameReader()
    #expect(try whole.feed(stream) == units)

    // A read can end anywhere: mid-length, mid-unit.
    var trickle = HEVCFrameReader()
    var got: [HEVCAccessUnit] = []
    for byte in stream { got += try trickle.feed(Data([byte])) }
    #expect(got == units)
}

@Test("a length no picture needs is a stream out of step, not a huge allocation")
func implausibleLength() {
    var reader = HEVCFrameReader()
    #expect(throws: HEVCFramingError.self) {
        _ = try reader.feed(Data([0x7F, 0xFF, 0xFF, 0xFF]))
    }
}

@Test("a NAL unit running past its access unit is refused")
func truncatedUnit() {
    // An access unit of 6 bytes holding a NAL unit that claims 9.
    var reader = HEVCFrameReader()
    #expect(throws: HEVCFramingError.truncatedUnit) {
        _ = try reader.feed(Data([0, 0, 0, 6, 0, 0, 0, 9, 0x26, 0x01]))
    }
}

@Test("a zero-length NAL unit, or bytes left over after the last, is a stream out of step")
func malformedBody() {
    // Each body would otherwise go to VideoToolbox as a NAL unit of nothing,
    // or lose its tail without a word.
    for body in [Data([0, 0, 0, 0]),
                 Data([0, 0, 0, 2, 0x26, 0x01, 0xAA])] {
        var reader = HEVCFrameReader()
        let framed = Data([0, 0, 0, UInt8(body.count)]) + body
        #expect(throws: HEVCFramingError.truncatedUnit) { _ = try reader.feed(framed) }
    }
}

@Test("NAL types are read from the header, and only VPS, SPS, PPS are parameter sets")
func nalTypes() {
    #expect(HEVCNAL.type(Data([0x40, 0x01])) == 32)           // VPS
    #expect(HEVCNAL.type(Data([0x42, 0x01])) == 33)           // SPS
    #expect(HEVCNAL.type(Data([0x44, 0x01])) == 34)           // PPS
    #expect(HEVCNAL.type(Data([0x26, 0x01])) == 19)           // IDR
    #expect(HEVCNAL.isParameterSet(Data([0x44, 0x01])))
    #expect(!HEVCNAL.isParameterSet(Data([0x26, 0x01])))
    #expect(!HEVCNAL.isParameterSet(Data()))
}

// MARK: - decoding

@Test("a real HEVC stream decodes into IOSurface-backed frames of its size",
      .enabled(if: hevcEncoderAvailable, "this machine has no HEVC encoder to make a stream"))
func decodesARealStream() throws {
    let encoder = try #require(TestHEVCEncoder(width: 320, height: 240))
    let decoder = HEVCDecoder()
    defer { decoder.invalidate() }
    let picture = try surface(320, 240)

    var decoded = 0
    for i in 0..<5 {
        let unit = try #require(encoder.unit(picture, at: Double(i) / 30))
        if let pixels = try decoder.decode(unit) {
            decoded += 1
            #expect(CVPixelBufferGetWidth(pixels) == 320 && CVPixelBufferGetHeight(pixels) == 240)
            #expect(CVPixelBufferGetIOSurface(pixels) != nil,
                    "the pipeline takes surfaces: a decode not backed by one is a frame lost")
        }
    }
    #expect(decoded == 5, "every picture should decode")
}

@Test("pictures before any parameter sets are dropped, not guessed at",
      .enabled(if: hevcEncoderAvailable, "this machine has no HEVC encoder to make a stream"))
func noParameterSetsYet() throws {
    let encoder = try #require(TestHEVCEncoder(width: 160, height: 120))
    let unit = try #require(encoder.unit(try surface(160, 120), at: 0))
    let decoder = HEVCDecoder()
    defer { decoder.invalidate() }

    let pictureOnly = HEVCAccessUnit(nals: unit.nals.filter { !HEVCNAL.isParameterSet($0) })
    #expect(try decoder.decode(pictureOnly) == nil)
    #expect(try decoder.decode(unit) != nil, "with its parameter sets the same picture decodes")
}

@Test("a size change mid-stream -- a turn -- decodes at the new size",
      .enabled(if: hevcEncoderAvailable, "this machine has no HEVC encoder to make a stream"))
func sizeChangeMidStream() throws {
    let decoder = HEVCDecoder()
    defer { decoder.invalidate() }
    let portrait = try #require(TestHEVCEncoder(width: 240, height: 320))
    let landscape = try #require(TestHEVCEncoder(width: 320, height: 240))

    let portraitUnit = try #require(portrait.unit(try surface(240, 320), at: 0))
    let landscapeUnit = try #require(landscape.unit(try surface(320, 240), at: 0))
    let a = try #require(try decoder.decode(portraitUnit))
    let b = try #require(try decoder.decode(landscapeUnit))
    #expect(CVPixelBufferGetWidth(a) == 240)
    #expect(CVPixelBufferGetWidth(b) == 320, "the decoder kept the first stream's size")
}

// MARK: - the stdin source

@Test("the source turns stdin into frames, and says when the stream ends",
      .enabled(if: hevcEncoderAvailable, "this machine has no HEVC encoder to make a stream"))
func stdinSource() throws {
    let encoder = try #require(TestHEVCEncoder(width: 320, height: 240))
    let pipe = Pipe()
    let frames = Mutex(0)
    let ended = DispatchSemaphore(value: 0)
    let reason = Mutex<String?>(nil)
    let source = HEVCStdinSource(input: pipe.fileHandleForReading) { _ in frames.withLock { $0 += 1 } }
    source.onAvailability = { available, why in
        if !available {
            reason.withLock { $0 = why }
            ended.signal()
        }
    }
    try source.start()
    defer { source.stop() }

    let picture = try surface(320, 240)
    for i in 0..<3 {
        pipe.fileHandleForWriting.write(try #require(encoder.unit(picture, at: Double(i) / 30)).framed())
    }
    try pipe.fileHandleForWriting.close()

    #expect(ended.wait(timeout: .now() + 10) == .success, "the end of stdin was never reported")
    #expect(frames.withLock { $0 } == 3)
    #expect(reason.withLock { $0 }?.contains("ended") == true)
}

@Test("a late viewer gets the last picture again",
      .enabled(if: hevcEncoderAvailable, "this machine has no HEVC encoder to make a stream"))
func requestCurrentFrame() throws {
    let encoder = try #require(TestHEVCEncoder(width: 160, height: 120))
    let pipe = Pipe()
    let frames = Mutex(0)
    let first = DispatchSemaphore(value: 0)
    let source = HEVCStdinSource(input: pipe.fileHandleForReading) { _ in
        frames.withLock { $0 += 1 }
        first.signal()
    }
    try source.start()
    defer {
        source.stop()
        try? pipe.fileHandleForWriting.close()
    }

    pipe.fileHandleForWriting.write(try #require(encoder.unit(try surface(160, 120), at: 0)).framed())
    #expect(first.wait(timeout: .now() + 10) == .success)
    source.requestCurrentFrame()
    #expect(frames.withLock { $0 } == 2, "the phone sends nothing new while its screen is still")
}

@Test("a stream out of step ends the source and says why")
func garbageEndsTheStream() throws {
    let pipe = Pipe()
    let ended = DispatchSemaphore(value: 0)
    let reason = Mutex<String?>(nil)
    let source = HEVCStdinSource(input: pipe.fileHandleForReading) { _ in }
    source.onAvailability = { available, why in
        if !available { reason.withLock { $0 = why }; ended.signal() }
    }
    try source.start()
    defer { source.stop() }

    pipe.fileHandleForWriting.write(Data([0x7F, 0xFF, 0xFF, 0xFF]))
    #expect(ended.wait(timeout: .now() + 10) == .success)
    #expect(reason.withLock { $0 }?.contains("could not be read") == true)
    try? pipe.fileHandleForWriting.close()
}

// MARK: - options

@Test("--hevc-stdin is a source of its own")
func hevcOption() throws {
    let o = try OptionsParser.parse(["--hevc-stdin", "--serve", "8430"])
    #expect(o.source == .hevcStdin(id: nil))
    let named = try OptionsParser.parse(["--hevc-stdin", "--source-id", "00008030-X", "--serve", "8430"])
    #expect(named.source == .hevcStdin(id: "00008030-X"))
    #expect(throws: OptionsError.sourceIDWithoutStdin) {
        try OptionsParser.parse(["--sim-udid", "A", "--source-id", "B", "--serve", "8430"])
    }
    #expect(throws: OptionsError.conflictingSources) {
        try OptionsParser.parse(["--hevc-stdin", "--sim-udid", "X", "--serve", "8430"])
    }
    #expect(throws: OptionsError.conflictingSources) {
        try OptionsParser.parse(["--hevc-stdin", "--device", "iPhone", "--serve", "8430"])
    }
}

/// A value shared with a callback on another queue.
private final class Mutex<Value>: @unchecked Sendable {
    private var value: Value
    private let lock = NSLock()
    init(_ value: Value) { self.value = value }
    func withLock<R>(_ body: (inout Value) -> R) -> R {
        lock.lock()
        defer { lock.unlock() }
        return body(&value)
    }
}
