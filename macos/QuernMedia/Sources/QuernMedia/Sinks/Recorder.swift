import AVFoundation
import CoreMedia
import Foundation

/// Writes encoded frames to an .mp4 via `AVAssetWriter` passthrough.
///
/// Passthrough, not re-encode: the compression session already produced H.264
/// sample buffers with correct timing, so the writer only has to container
/// them. That also means the Annex-B rendering is bypassed entirely — Annex-B
/// carries no timestamps, so a recording built from it would have to invent
/// them, which is exactly the bug this design avoids.
public final class Recorder {
    public enum Failure: Error, CustomStringConvertible {
        case noFormatDescription
        case writerRejectedInput
        case writerFailed(String)
        case finishTimedOut(TimeInterval)

        public var description: String {
            switch self {
            case .noFormatDescription: return "first sample carried no format description"
            case .writerRejectedInput: return "AVAssetWriter rejected a passthrough input"
            case .writerFailed(let m): return "AVAssetWriter failed: \(m)"
            case .finishTimedOut(let t):
                return "AVAssetWriter did not finish writing within \(t)s"
            }
        }
    }

    public struct Summary: Sendable, Equatable {
        public let framesWritten: Int
        public let framesDropped: Int
        /// Span between the first and last written frame, in seconds.
        public let duration: Double
        public let url: URL
    }

    private let writer: AVAssetWriter
    /// Created on the first sample, not at init.
    ///
    /// A passthrough input (nil `outputSettings`) has no way to describe the
    /// media it will carry, so `canAdd` refuses it unless given a
    /// `sourceFormatHint`. That hint is the encoder's format description,
    /// which does not exist until the first frame comes out. Building the
    /// input at init fails outright.
    private var input: AVAssetWriterInput?
    private let lock = NSLock()

    private var started = false
    private var finished = false
    private var firstPTS: CMTime = .invalid
    private var lastPTS: CMTime = .invalid
    private var framesWritten = 0
    private var framesDropped = 0
    private var failureReason: Failure?

    public let url: URL

    public init(url: URL) throws {
        self.url = url
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
    }

    /// - Returns: whether the frame was written.
    @discardableResult
    public func append(_ frame: EncodedFrame) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return false }

        if !started {
            // A file that opens on a non-keyframe is undecodable until the
            // next IDR, which for a short recording can mean forever.
            guard frame.isKeyframe else {
                framesDropped += 1
                return false
            }
            do {
                try start(with: frame.sample)
                started = true
            } catch let error as Failure {
                failureReason = error
                finished = true
                return false
            } catch {
                failureReason = .writerFailed("\(error)")
                finished = true
                return false
            }
        }

        guard let input, input.isReadyForMoreMediaData else {
            framesDropped += 1
            return false
        }
        guard input.append(frame.sample) else {
            framesDropped += 1
            return false
        }
        framesWritten += 1
        lastPTS = CMSampleBufferGetPresentationTimeStamp(frame.sample)
        return true
    }

    /// Caller holds `lock`.
    private func start(with sample: CMSampleBuffer) throws {
        guard let hint = CMSampleBufferGetFormatDescription(sample) else {
            throw Failure.noFormatDescription
        }
        let created = AVAssetWriterInput(
            mediaType: .video, outputSettings: nil, sourceFormatHint: hint
        )
        created.expectsMediaDataInRealTime = true
        guard writer.canAdd(created) else { throw Failure.writerRejectedInput }
        writer.add(created)
        guard writer.startWriting() else {
            throw Failure.writerFailed(writer.error?.localizedDescription ?? "unknown")
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        // The session starts at the first frame's real timestamp, so the
        // movie's timeline is the host clock rather than zero-based.
        writer.startSession(atSourceTime: pts)
        firstPTS = pts
        input = created
    }

    /// Blocking, and it has to be.
    ///
    /// An .mp4 whose moov atom was never written is not a shorter recording,
    /// it is an unopenable file. Anything that ends the process — including a
    /// signal — must get here first.
    @discardableResult
    /// - Parameter timeout: how long to wait for `finishWriting`. A liveness
    ///   guard against a wedged writer, not a performance assertion -- so it
    ///   is generous. Software VideoToolbox on a loaded CI runner took longer
    ///   than the 10s this used to allow, and the recording was sound; only
    ///   the deadline was wrong.
    public func finish(timeout: TimeInterval = 60) -> Summary? {
        lock.lock()
        guard started, !finished else {
            finished = true
            lock.unlock()
            return nil
        }
        finished = true
        let written = framesWritten
        let dropped = framesDropped
        let duration = CMTimeGetSeconds(CMTimeSubtract(lastPTS, firstPTS))
        let localInput = input
        lock.unlock()

        localInput?.markAsFinished()
        let done = DispatchSemaphore(value: 0)
        finishWriting { done.signal() }

        // The wait's result is the whole reason for waiting. Until
        // `finishWriting` lands the moov atom the file holds samples nothing
        // can index, and AVFoundation reports reading it as "Cannot Open ...
        // may be damaged" with Duration as the failed dependency. Discarding
        // the result returned a Summary describing a complete recording
        // precisely when the machine was too slow to have made one.
        if done.wait(timeout: .now() + timeout) == .timedOut {
            note(.finishTimedOut(timeout))
            return nil
        }
        // Finishing can complete and still have failed; status is the only
        // place that says so.
        if writer.status == .failed {
            note(.writerFailed(writer.error?.localizedDescription ?? "unknown"))
            return nil
        }

        return Summary(
            framesWritten: written, framesDropped: dropped,
            duration: duration.isFinite ? duration : 0, url: url
        )
    }

    private func note(_ reason: Failure) {
        lock.lock()
        defer { lock.unlock() }
        failureReason = reason
    }

    /// Test seam: stands in for `AVAssetWriter.finishWriting`. The real one
    /// completes on its own schedule, so a test for the timeout path would be
    /// racing it -- and on a fast machine it wins, which makes the test pass
    /// for the wrong reason. Withhold the completion to make the deadline
    /// deterministic.
    var finishWritingOverride: ((@escaping () -> Void) -> Void)?

    private func finishWriting(_ completion: @escaping () -> Void) {
        if let finishWritingOverride {
            finishWritingOverride(completion)
            return
        }
        writer.finishWriting(completionHandler: completion)
    }

    /// Non-nil when the writer refused to start, or could not be finished --
    /// for callers that want to report why rather than silently produce
    /// nothing. A nil `finish()` always leaves a reason here.
    public var failure: Failure? {
        lock.lock()
        defer { lock.unlock() }
        return failureReason
    }
}
