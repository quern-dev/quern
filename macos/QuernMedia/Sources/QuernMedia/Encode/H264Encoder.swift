import CoreMedia
import Foundation
import IOSurface
import VideoToolbox

/// One encoded frame, in both the shapes consumers need.
///
/// Recording wants the sample buffer: it carries timing, and `AVAssetWriter`
/// containers it without re-encoding. A socket wants Annex-B. Producing both
/// costs one extra copy per frame and avoids making the encoder guess.
public struct H264Output {
    public let frame: EncodedFrame
    /// Annex-B, with SPS/PPS prepended when this is a keyframe.
    public let annexB: Data
}

/// H.264 via VideoToolbox.
///
/// Same API as the JPEG path and the same hardware, but three things differ
/// and all of them matter. Output is AVCC with parameter sets out of band.
/// Frames depend on each other, so a viewer joining mid-stream decodes
/// nothing until a keyframe. And bitrate is a target we set rather than an
/// outcome of per-frame quality.
///
/// Measured against MJPEG on the same device at 60 fps under identical load:
/// 29.6 MB became 2.5 MB over the same window, at the same CPU, memory and
/// delivered frame rate. Both paths are decode-bound, so the swap shows up
/// entirely in the output.
public final class H264Encoder {
    private let maxDimension: Int
    private let bitrate: Int
    private let expectedFPS: Double

    private var session: VTCompressionSession?
    private var sessionSize: (width: Int, height: Int) = (0, 0)
    private var parameterSets: Data?
    private let lock = NSLock()

    public init(maxDimension: Int, bitrate: Int, expectedFPS: Double) {
        self.maxDimension = maxDimension
        self.bitrate = bitrate
        self.expectedFPS = max(expectedFPS, 1)
    }

    deinit { invalidate() }

    public func invalidate() {
        lock.lock()
        if let session {
            VTCompressionSessionInvalidate(session)
            self.session = nil
        }
        parameterSets = nil
        lock.unlock()
    }

    /// Synchronous, for the reason the JPEG encoder is: the IOSurface is
    /// wrapped rather than copied, and both sources rewrite theirs in place.
    ///
    /// - Parameter forceKeyframe: emit an IDR now. This is what a late viewer
    ///   needs, and what `screenrecord` cannot offer on Android.
    public func encode(_ frame: CapturedFrame, forceKeyframe: Bool = false) -> H264Output? {
        let sw = IOSurfaceGetWidth(frame.surface)
        let sh = IOSurfaceGetHeight(frame.surface)
        guard sw > 0, sh > 0 else { return nil }
        let target = ScaleTarget.fit(width: sw, height: sh, maxDimension: maxDimension)

        lock.lock()
        defer { lock.unlock() }
        guard ensureSession(width: target.width, height: target.height),
              let session else { return nil }

        var unmanaged: Unmanaged<CVPixelBuffer>?
        guard CVPixelBufferCreateWithIOSurface(nil, frame.surface, nil, &unmanaged)
                == kCVReturnSuccess,
              let pixelBuffer = unmanaged?.takeRetainedValue() else { return nil }

        var properties: CFDictionary?
        if forceKeyframe {
            properties = [kVTEncodeFrameOptionKey_ForceKeyFrame: kCFBooleanTrue] as CFDictionary
        }

        var output: H264Output?
        // The frame's own timestamp, never a frame counter. A synthetic PTS is
        // harmless on the wire and wrong on disk: a source running at 49 fps
        // against a 60 fps nominal plays 22% fast, and an idle gap collapses to
        // nothing instead of showing as a pause.
        let status = VTCompressionSessionEncodeFrame(
            session, imageBuffer: pixelBuffer, presentationTimeStamp: frame.time,
            duration: .invalid, frameProperties: properties, infoFlagsOut: nil
        ) { [weak self] status, _, sample in
            guard status == noErr, let sample, let self else { return }
            output = self.package(sample)
        }
        guard status == noErr else { return nil }
        VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
        return output
    }

    /// Caller holds `lock`.
    private func package(_ sample: CMSampleBuffer) -> H264Output? {
        let keyframe = Self.isKeyframe(sample)
        if keyframe, let fd = CMSampleBufferGetFormatDescription(sample) {
            parameterSets = AnnexB.parameterSets(from: fd)
        }
        guard let avcc = SampleData.copy(from: sample),
              let body = AnnexB.fromAVCC(avcc) else { return nil }

        var annexB = Data()
        // Parameter sets ahead of every keyframe, not only the first, so a
        // viewer can join at any keyframe rather than only at stream start.
        if keyframe, let sets = parameterSets { annexB.append(sets) }
        annexB.append(body)

        return H264Output(
            frame: EncodedFrame(
                sample: sample,
                time: CMSampleBufferGetPresentationTimeStamp(sample),
                isKeyframe: keyframe
            ),
            annexB: annexB
        )
    }

    static func isKeyframe(_ sample: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(
            sample, createIfNecessary: false
        ) as? [[CFString: Any]], let first = attachments.first else {
            return true  // no attachments at all: treat as a sync sample
        }
        if let notSync = first[kCMSampleAttachmentKey_NotSync] as? Bool { return !notSync }
        return true
    }

    /// Caller holds `lock`.
    private func ensureSession(width: Int, height: Int) -> Bool {
        if session != nil, sessionSize == (width, height) { return true }
        if let existing = session {
            VTCompressionSessionInvalidate(existing)
            session = nil
        }
        parameterSets = nil

        var created: VTCompressionSession?
        let spec: [CFString: Any] = [
            kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder: true
        ]
        guard VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: spec as CFDictionary,
            imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: nil, refcon: nil, compressionSessionOut: &created
        ) == noErr, let created else { return false }

        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_RealTime,
                             value: kCFBooleanTrue)
        // No B-frames: reordering delay is not worth the bitrate for a live
        // view, and it keeps presentation order equal to decode order.
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_AllowFrameReordering,
                             value: kCFBooleanFalse)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_ProfileLevel,
                             value: kVTProfileLevel_H264_High_AutoLevel)
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_AverageBitRate,
                             value: NSNumber(value: bitrate))
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_ExpectedFrameRate,
                             value: NSNumber(value: expectedFPS))
        // A periodic IDR bounds join latency without an explicit request.
        // Note this counts *frames*, not seconds: on an event-driven source
        // the interval in wall time stretches whenever the screen is idle.
        VTSessionSetProperty(created, key: kVTCompressionPropertyKey_MaxKeyFrameInterval,
                             value: NSNumber(value: Int(expectedFPS * 2)))
        VTCompressionSessionPrepareToEncodeFrames(created)

        session = created
        sessionSize = (width, height)
        return true
    }
}
