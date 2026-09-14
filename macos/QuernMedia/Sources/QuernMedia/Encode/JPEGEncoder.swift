import CoreMedia
import Foundation
import IOSurface
import VideoToolbox

/// JPEG via VideoToolbox rather than ImageIO.
///
/// Same bytes on the wire, same bare `<img>` on the client. The difference is
/// where the work happens: measured on an M4, `CGImageDestination` runs at
/// 99-100% CPU-to-wall while this runs at ~23%, for roughly a sixth of the CPU
/// time. On a live simulator stream that was 10.0% of a core against 2.8%.
///
/// Do not gate anything on `UsingHardwareAcceleratedVideoEncoder`. It reports
/// **false** for the JPEG codec even though the work is plainly leaving the
/// cores. Measure instead.
///
/// Scaling is the session's job. VideoToolbox resamples a mismatched input
/// buffer down to the dimensions the session was created with, which keeps the
/// resize on the media engine instead of in a `CGContext`.
public final class JPEGEncoder {
    private let maxDimension: Int
    private let quality: Double

    private var session: VTCompressionSession?
    private var sessionSize: (width: Int, height: Int) = (0, 0)
    private let lock = NSLock()

    /// - Parameters:
    ///   - maxDimension: downscale so the longest side is at most this. 0 = native.
    ///   - quality: 0...1. Not comparable to ImageIO's scale — at a nominal
    ///     0.6 VideoToolbox produced 38 KB frames where ImageIO produced 32 KB,
    ///     so matching byte size means re-tuning the number rather than reusing it.
    public init(maxDimension: Int, quality: Double) {
        self.maxDimension = maxDimension
        self.quality = quality
    }

    deinit { invalidate() }

    public func invalidate() {
        lock.lock()
        if let session {
            VTCompressionSessionInvalidate(session)
            self.session = nil
        }
        lock.unlock()
    }

    /// Synchronous on purpose.
    ///
    /// `CompleteFrames` after every frame gives up media-engine pipelining,
    /// which costs wall time but not CPU. It buys the contract the caller
    /// needs: the surface is fully read before returning. That matters because
    /// a capture buffer's IOSurface belongs to a recycling pool and can be
    /// rewritten as soon as we let go, and a simulator's framebuffer surface is
    /// a single persistent surface always rewritten in place — retaining it
    /// would not help.
    public func encode(_ surface: IOSurface) -> Data? {
        let sourceWidth = IOSurfaceGetWidth(surface)
        let sourceHeight = IOSurfaceGetHeight(surface)
        guard sourceWidth > 0, sourceHeight > 0 else { return nil }
        let target = ScaleTarget.fit(
            width: sourceWidth, height: sourceHeight, maxDimension: maxDimension
        )

        lock.lock()
        defer { lock.unlock() }
        guard ensureSession(width: target.width, height: target.height),
              let session else { return nil }

        var unmanaged: Unmanaged<CVPixelBuffer>?
        guard CVPixelBufferCreateWithIOSurface(nil, surface, nil, &unmanaged)
                == kCVReturnSuccess,
              let pixelBuffer = unmanaged?.takeRetainedValue() else { return nil }

        var encoded: Data?
        let status = VTCompressionSessionEncodeFrame(
            session, imageBuffer: pixelBuffer,
            presentationTimeStamp: CMTime(value: 0, timescale: 30),
            duration: .invalid, frameProperties: nil, infoFlagsOut: nil
        ) { status, _, sample in
            guard status == noErr, let sample else { return }
            encoded = SampleData.copy(from: sample)
        }
        guard status == noErr else { return nil }
        VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
        return encoded
    }

    /// Caller holds `lock`.
    private func ensureSession(width: Int, height: Int) -> Bool {
        if session != nil, sessionSize == (width, height) { return true }
        if let existing = session {
            VTCompressionSessionInvalidate(existing)
            session = nil
        }
        var created: VTCompressionSession?
        guard VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_JPEG, encoderSpecification: nil,
            imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: nil, refcon: nil, compressionSessionOut: &created
        ) == noErr, let created else { return false }

        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_RealTime,
                             value: kCFBooleanTrue)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_Quality,
                             value: NSNumber(value: quality))
        VTCompressionSessionPrepareToEncodeFrames(created)
        session = created
        sessionSize = (width, height)
        return true
    }
}

/// Target dimensions for a downscale, kept separate so it can be tested
/// without an encoder.
public enum ScaleTarget {
    public static func fit(width: Int, height: Int, maxDimension: Int) -> (width: Int, height: Int) {
        let longest = max(width, height)
        guard maxDimension > 0, longest > maxDimension else {
            return (even(width), even(height))
        }
        let factor = Double(maxDimension) / Double(longest)
        return (
            even(Int((Double(width) * factor).rounded())),
            even(Int((Double(height) * factor).rounded()))
        )
    }

    /// Odd sizes are legal for these codecs but a reliable source of
    /// off-by-one chroma handling across decoders.
    private static func even(_ v: Int) -> Int { max(2, v & ~1) }
}

enum SampleData {
    /// Copies a sample buffer's payload out. The block buffer may not be
    /// contiguous, so this cannot just take a base pointer.
    static func copy(from sample: CMSampleBuffer) -> Data? {
        guard let block = CMSampleBufferGetDataBuffer(sample) else { return nil }
        let length = CMBlockBufferGetDataLength(block)
        guard length > 0 else { return nil }
        var data = Data(count: length)
        let ok = data.withUnsafeMutableBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            return CMBlockBufferCopyDataBytes(
                block, atOffset: 0, dataLength: length, destination: base
            ) == kCMBlockBufferNoErr
        }
        return ok ? data : nil
    }
}
