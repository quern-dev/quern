import CoreMedia
import CoreVideo
import Foundation
import IOSurface
import VideoToolbox

/// A physical iPhone's screen, arriving as HEVC on stdin.
///
/// iOS 27's CoreDevice display service streams a phone's screen as HEVC over
/// RTP, over USB or Wi-Fi alike -- what Xcode's Device Hub shows. quern's
/// server receives it (it holds the CoreDevice connection) and writes the
/// pictures here; this decodes them into the surfaces every other source
/// produces, so a network-only phone streams, serves and records like a
/// simulator. Measured on an iPhone 11, iOS 27.2.1: about 32 fps over Wi-Fi.
///
/// The framing on stdin, one access unit (one picture) at a time:
///
///     [4-byte big-endian length L][L bytes: NAL units, each [4-byte BE length][NAL]]
///
/// which is HEVC's own length-prefixed form, so the units go to VideoToolbox
/// as they are. Parameter sets (VPS, SPS, PPS) arrive in the stream, before
/// the first picture and whenever the picture size changes -- a turn.
public enum HEVCNAL {
    public static let vps: UInt8 = 32
    public static let sps: UInt8 = 33
    public static let pps: UInt8 = 34

    /// The unit's type, from its two-byte header.
    public static func type(_ nal: Data) -> UInt8? {
        guard let first = nal.first else { return nil }
        return (first >> 1) & 0x3F
    }

    public static func isParameterSet(_ nal: Data) -> Bool {
        guard let t = type(nal) else { return false }
        return t == vps || t == sps || t == pps
    }
}

/// One picture's NAL units.
public struct HEVCAccessUnit: Equatable {
    public let nals: [Data]

    public init(nals: [Data]) { self.nals = nals }

    /// This unit in the stdin framing, for a writer -- and for the tests.
    public func framed() -> Data {
        var body = Data()
        for nal in nals {
            body.append(contentsOf: bigEndian(UInt32(nal.count)))
            body.append(nal)
        }
        var out = Data(bigEndian(UInt32(body.count)))
        out.append(body)
        return out
    }
}

private func bigEndian(_ v: UInt32) -> [UInt8] {
    [UInt8(v >> 24), UInt8((v >> 16) & 0xFF), UInt8((v >> 8) & 0xFF), UInt8(v & 0xFF)]
}

private func readBigEndian(_ d: Data, at i: Int) -> Int {
    let b = d.startIndex + i
    return Int(d[b]) << 24 | Int(d[b + 1]) << 16 | Int(d[b + 2]) << 8 | Int(d[b + 3])
}

public enum HEVCFramingError: Error, Equatable, CustomStringConvertible {
    /// A length past what any picture needs: the stream is not this framing.
    case implausibleLength(Int)
    /// A NAL unit whose length runs past its access unit.
    case truncatedUnit

    public var description: String {
        switch self {
        case .implausibleLength(let n): return "an access unit of \(n) bytes is not this framing"
        case .truncatedUnit: return "a NAL unit runs past the end of its access unit"
        }
    }
}

/// Splits the stdin bytes into access units, however they arrive: a read can
/// end mid-length or mid-unit, and the rest comes with the next. Pure, so the
/// tests drive it a byte at a time.
public struct HEVCFrameReader {
    /// Far past a 4K keyframe; anything larger is a stream out of step.
    public static let maxAccessUnit = 16 * 1024 * 1024

    private var buffer = Data()

    public init() {}

    public mutating func feed(_ bytes: Data) throws -> [HEVCAccessUnit] {
        buffer.append(bytes)
        var out: [HEVCAccessUnit] = []
        while buffer.count >= 4 {
            let length = readBigEndian(buffer, at: 0)
            guard length <= Self.maxAccessUnit else {
                throw HEVCFramingError.implausibleLength(length)
            }
            guard buffer.count >= 4 + length else { break }
            let body = buffer.subdata(in: buffer.startIndex + 4 ..< buffer.startIndex + 4 + length)
            buffer.removeSubrange(buffer.startIndex ..< buffer.startIndex + 4 + length)
            out.append(HEVCAccessUnit(nals: try Self.units(body)))
        }
        return out
    }

    static func units(_ body: Data) throws -> [Data] {
        var nals: [Data] = []
        var i = 0
        while i + 4 <= body.count {
            let n = readBigEndian(body, at: i)
            guard n > 0, i + 4 + n <= body.count else { throw HEVCFramingError.truncatedUnit }
            nals.append(body.subdata(in: body.startIndex + i + 4 ..< body.startIndex + i + 4 + n))
            i += 4 + n
        }
        guard i == body.count else { throw HEVCFramingError.truncatedUnit }
        return nals
    }
}

public enum HEVCDecoderError: Error, CustomStringConvertible {
    case formatDescription(OSStatus)
    case session(OSStatus)

    public var description: String {
        switch self {
        case .formatDescription(let s): return "the parameter sets were refused (\(s))"
        case .session(let s): return "VideoToolbox would not open an HEVC decoder (\(s))"
        }
    }
}

/// Decodes access units into IOSurface-backed BGRA buffers -- the same pixels
/// the simulator framebuffer and the USB capture hand over, so the encoders
/// downstream do not know which source they have.
public final class HEVCDecoder {
    private var session: VTDecompressionSession?
    private var format: CMVideoFormatDescription?
    private var parameterSets: [Data] = []

    public init() {}

    deinit { invalidate() }

    public func invalidate() {
        if let session { VTDecompressionSessionInvalidate(session) }
        session = nil
        format = nil
    }

    /// The picture's pixels, or nil when there is nothing to show: no
    /// parameter sets yet, a unit with no picture in it, or a decode error --
    /// each a dropped frame, never a wrong one.
    public func decode(_ unit: HEVCAccessUnit) throws -> CVPixelBuffer? {
        let sets = unit.nals.filter(HEVCNAL.isParameterSet)
        if !sets.isEmpty { try configure(sets) }
        let picture = unit.nals.filter { !HEVCNAL.isParameterSet($0) }
        guard let session, let format, !picture.isEmpty else { return nil }

        var body = Data()
        for nal in picture {
            body.append(contentsOf: bigEndian(UInt32(nal.count)))
            body.append(nal)
        }
        guard let sample = Self.sample(body, format: format) else { return nil }
        var decoded: CVPixelBuffer?
        let status = VTDecompressionSessionDecodeFrame(
            session, sampleBuffer: sample, flags: [], infoFlagsOut: nil
        ) { status, _, image, _, _ in
            if status == noErr { decoded = image }
        }
        return status == noErr ? decoded : nil
    }

    /// A new decoder only when the parameter sets change -- a turn, a size
    /// change -- and kept when the session can take the new format as it is.
    private func configure(_ sets: [Data]) throws {
        let vps = sets.last { HEVCNAL.type($0) == HEVCNAL.vps }
            ?? parameterSets.first { HEVCNAL.type($0) == HEVCNAL.vps }
        let sps = sets.last { HEVCNAL.type($0) == HEVCNAL.sps }
            ?? parameterSets.first { HEVCNAL.type($0) == HEVCNAL.sps }
        let pps = sets.last { HEVCNAL.type($0) == HEVCNAL.pps }
            ?? parameterSets.first { HEVCNAL.type($0) == HEVCNAL.pps }
        guard let vps, let sps, let pps else { return }
        let next = [vps, sps, pps]
        if next == parameterSets, format != nil { return }

        var fmt: CMFormatDescription?
        let status = next.withUnsafePointers { pointers, sizes in
            CMVideoFormatDescriptionCreateFromHEVCParameterSets(
                allocator: kCFAllocatorDefault, parameterSetCount: 3,
                parameterSetPointers: pointers, parameterSetSizes: sizes,
                nalUnitHeaderLength: 4, extensions: nil, formatDescriptionOut: &fmt)
        }
        guard status == noErr, let fmt else { throw HEVCDecoderError.formatDescription(status) }
        parameterSets = next
        format = fmt
        if let session, VTDecompressionSessionCanAcceptFormatDescription(session, formatDescription: fmt) {
            return
        }
        if let session { VTDecompressionSessionInvalidate(session) }
        session = nil

        let attributes: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
        ]
        var made: VTDecompressionSession?
        let created = VTDecompressionSessionCreate(
            allocator: kCFAllocatorDefault, formatDescription: fmt, decoderSpecification: nil,
            imageBufferAttributes: attributes as CFDictionary, outputCallback: nil,
            decompressionSessionOut: &made)
        guard created == noErr, let made else { throw HEVCDecoderError.session(created) }
        session = made
    }

    private static func sample(_ body: Data, format: CMVideoFormatDescription) -> CMSampleBuffer? {
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: body.count,
            blockAllocator: kCFAllocatorDefault, customBlockSource: nil, offsetToData: 0,
            dataLength: body.count, flags: 0, blockBufferOut: &block) == noErr, let block
        else { return nil }
        let copied = body.withUnsafeBytes { raw in
            CMBlockBufferReplaceDataBytes(with: raw.baseAddress!, blockBuffer: block,
                                          offsetIntoDestination: 0, dataLength: body.count)
        }
        guard copied == noErr else { return nil }
        var sample: CMSampleBuffer?
        var size = body.count
        guard CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: format,
            sampleCount: 1, sampleTimingEntryCount: 0, sampleTimingArray: nil,
            sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sample) == noErr
        else { return nil }
        return sample
    }
}

private extension Array where Element == Data {
    /// The three sets as the C call wants them: pointers and sizes, alive for
    /// the length of `body`.
    func withUnsafePointers<R>(
        _ body: (UnsafePointer<UnsafePointer<UInt8>>, UnsafePointer<Int>) -> R
    ) -> R {
        let copies = map { data -> UnsafeMutablePointer<UInt8> in
            let p = UnsafeMutablePointer<UInt8>.allocate(capacity: data.count)
            data.copyBytes(to: p, count: data.count)
            return p
        }
        defer { copies.forEach { $0.deallocate() } }
        let pointers = copies.map { UnsafePointer($0) }
        let sizes = map(\.count)
        return pointers.withUnsafeBufferPointer { p in
            sizes.withUnsafeBufferPointer { s in body(p.baseAddress!, s.baseAddress!) }
        }
    }
}

/// Frames from the HEVC on stdin, as a `FrameSource`.
public final class HEVCStdinSource: FrameSource {
    /// Told when the stream stops (stdin closed, or a stream out of step)
    /// with the reason, so viewers see the phone go off air rather than a
    /// frozen last picture.
    public var onAvailability: ((Bool, String) -> Void)?

    private let input: FileHandle
    private let onFrame: (CapturedFrame) -> Void
    private let queue = DispatchQueue(label: "quern.media.hevc", qos: .userInteractive)
    private let lock = NSLock()
    private var last: CVPixelBuffer?
    private var stopped = false
    private var describedFormat = false

    public init(input: FileHandle = .standardInput,
                onFrame: @escaping (CapturedFrame) -> Void) {
        self.input = input
        self.onFrame = onFrame
    }

    public func start() throws {
        queue.async { [weak self] in self?.run() }
    }

    public func stop() {
        lock.lock()
        stopped = true
        lock.unlock()
    }

    /// The last picture again, for a viewer that has just attached: the phone
    /// sends a new picture only when its screen changes.
    public func requestCurrentFrame() {
        lock.lock()
        let pixels = last
        lock.unlock()
        if let pixels { deliver(pixels) }
    }

    private var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }

    private func run() {
        var reader = HEVCFrameReader()
        let decoder = HEVCDecoder()
        defer { decoder.invalidate() }
        while !isStopped {
            let chunk = input.availableData        // blocks; empty at end of file
            if chunk.isEmpty {
                MediaLog.log("[hevc] the stream ended (stdin closed)")
                onAvailability?(false, "the phone's screen stream ended")
                return
            }
            do {
                for unit in try reader.feed(chunk) {
                    guard let pixels = try decoder.decode(unit) else { continue }
                    lock.lock()
                    last = pixels
                    lock.unlock()
                    deliver(pixels)
                }
            } catch {
                // Out of step: nothing after this can be trusted to be framed
                // as it should, so the stream is over rather than garbled.
                MediaLog.log("[hevc] \(error)")
                onAvailability?(false, "the phone's screen stream could not be read: \(error)")
                return
            }
        }
    }

    private func deliver(_ pixels: CVPixelBuffer) {
        // Both the reader and a viewer's requestCurrentFrame come here.
        lock.lock()
        let first = !describedFormat
        describedFormat = true
        lock.unlock()
        if first {
            MediaLog.log("[hevc] \(CVPixelBufferGetWidth(pixels))x\(CVPixelBufferGetHeight(pixels)) px")
        }
        guard let ref = CVPixelBufferGetIOSurface(pixels)?.takeUnretainedValue() else { return }
        // Stamped on arrival: the stdin framing carries no timestamp, and the
        // RTP clock is the phone's, not this host's.
        onFrame(CapturedFrame(surface: unsafeBitCast(ref, to: IOSurface.self),
                              time: CMClockGetTime(CMClockGetHostTimeClock()),
                              timeAccuracy: .arrival))
    }
}
