import CoreMedia
import Foundation

/// One encoded frame, in whichever shape the codec produces.
public enum EncodedPayload {
    case jpeg(Data)
    case h264(H264Output)

    public var isKeyframe: Bool {
        switch self {
        case .jpeg: return true          // every JPEG stands alone
        case .h264(let out): return out.frame.isKeyframe
        }
    }

    public var byteCount: Int {
        switch self {
        case .jpeg(let d): return d.count
        case .h264(let out): return out.annexB.count
        }
    }
}

/// Anything that consumes encoded frames: an HTTP server, a recorder, a
/// future WebSocket or disk sink.
public protocol FrameSink: AnyObject {
    /// Whether this sink currently has any use for frames. A pipeline whose
    /// sinks all say no skips encoding entirely, which is what makes an idle
    /// preview free.
    var wantsFrames: Bool { get }
    func receive(_ payload: EncodedPayload)
}

/// Owns the encoder and the throttle, and fans encoded frames out to sinks.
///
/// The spike conflated three jobs in its HTTP server: transport, encoder
/// ownership and recorder ownership. `publish()` picked the codec, fed the
/// recorder and wrote to sockets. That made "record without serving" need an
/// awkward server object that was created but never listened, and it had no
/// answer at all for the farm case of one source feeding many consumers.
///
/// Splitting it means a sink is just a sink. Recording and serving are peers,
/// either can be absent, and adding a third costs nothing structural.
public final class StreamPipeline {
    public enum Codec: Sendable, Equatable {
        case mjpeg
        case h264
    }

    private let codec: Codec
    private let jpeg: JPEGEncoder
    private let h264: H264Encoder

    private var throttle: FrameThrottle
    private var sinks: [FrameSink] = []
    private var pendingKeyframe = false
    /// Bumped by every request. `consume` releases the lock to encode, so a
    /// request can arrive while a frame is in flight; comparing the counter
    /// afterwards is how that request is told apart from the one being served.
    private var keyframeRequestID: UInt64 = 0
    private let lock = NSLock()

    public private(set) var framesOffered = 0
    public private(set) var framesEncoded = 0
    public private(set) var bytesProduced = 0

    public init(
        codec: Codec,
        fps: Double,
        maxDimension: Int,
        quality: Double = 0.6,
        bitrate: Int = 2_000_000
    ) {
        self.codec = codec
        self.throttle = FrameThrottle(fps: fps)
        self.jpeg = JPEGEncoder(maxDimension: maxDimension, quality: quality)
        self.h264 = H264Encoder(
            maxDimension: maxDimension, bitrate: bitrate, expectedFPS: fps
        )
    }

    deinit { invalidate() }

    public func invalidate() {
        jpeg.invalidate()
        h264.invalidate()
    }

    public func add(_ sink: FrameSink) {
        lock.lock()
        sinks.append(sink)
        lock.unlock()
    }

    public func remove(_ sink: FrameSink) {
        lock.lock()
        sinks.removeAll { $0 === sink }
        lock.unlock()
    }

    /// Ask for a keyframe on the next encoded frame.
    ///
    /// Called when a viewer attaches, or when a recording starts: H.264 frames
    /// depend on earlier ones, so a consumer joining mid-stream decodes
    /// nothing until an IDR arrives. Harmless under MJPEG, where every frame
    /// already stands alone.
    public func requestKeyframe() {
        lock.lock()
        pendingKeyframe = true
        keyframeRequestID &+= 1
        lock.unlock()
    }

    /// Feed one captured frame in. Call this from a `FrameSource` callback.
    ///
    /// Encodes synchronously on the caller's queue, deliberately. The
    /// `IOSurface` is wrapped rather than copied, and both sources rewrite
    /// theirs in place — a capture buffer belongs to a recycling pool and the
    /// simulator's framebuffer surface is a single persistent one. Handing the
    /// work to another queue would encode whichever frame arrived next.
    public func consume(_ frame: CapturedFrame) {
        lock.lock()
        framesOffered += 1
        let interested = sinks.filter(\.wantsFrames)
        guard !interested.isEmpty else {
            lock.unlock()
            return
        }
        let due = throttle.shouldEncode(at: CMTimeGetSeconds(frame.time))
        let wantKey = pendingKeyframe
        let servingRequest = keyframeRequestID
        lock.unlock()
        guard due else { return }

        guard let payload = encode(frame, forceKeyframe: wantKey) else { return }

        lock.lock()
        framesEncoded += 1
        bytesProduced += payload.byteCount
        // Cleared once a keyframe has actually come out, not when one was
        // asked for. Clearing on request meant an encode that returned nil
        // swallowed the request, and the viewer that triggered it decoded
        // nothing until the next periodic IDR.
        //
        // And only for the request being served. A viewer attaching while
        // this frame was encoding raised a new one, and that viewer is not in
        // the `interested` list this payload goes to -- so clearing on the
        // stale flag alone dropped a request whose keyframe was never sent
        // to the client that asked for it.
        if wantKey, payload.isKeyframe, keyframeRequestID == servingRequest {
            pendingKeyframe = false
        }
        lock.unlock()

        for sink in interested { sink.receive(payload) }
    }

    /// Test seam. The encoders fail only on conditions a test cannot
    /// manufacture -- a zero-sized IOSurface cannot be allocated -- so the
    /// "encode returned nil" branch would otherwise be unreachable from a
    /// test, which is how the keyframe request came to be dropped there in
    /// the first place. Internal, and set before the pipeline is fed.
    var encodeOverride: ((CapturedFrame, Bool) -> EncodedPayload?)?

    private func encode(_ frame: CapturedFrame, forceKeyframe: Bool) -> EncodedPayload? {
        if let encodeOverride { return encodeOverride(frame, forceKeyframe) }
        switch codec {
        case .mjpeg:
            return jpeg.encode(frame.surface).map { .jpeg($0) }
        case .h264:
            return h264.encode(frame, forceKeyframe: forceKeyframe).map { .h264($0) }
        }
    }
}

/// Adapts a `Recorder` to the sink protocol.
///
/// A recording is a consumer like any other, which is the point of the split:
/// recording with no viewer attached needs no server, and a viewer leaving
/// does not stop the recording.
public final class RecordingSink: FrameSink {
    private let recorder: Recorder
    private var stopped = false
    private let lock = NSLock()

    public init(recorder: Recorder) {
        self.recorder = recorder
    }

    public var wantsFrames: Bool {
        lock.lock()
        defer { lock.unlock() }
        return !stopped
    }

    public func receive(_ payload: EncodedPayload) {
        // Only H.264 can go in an .mp4. A pipeline configured for MJPEG and
        // asked to record is a caller error, not something to paper over.
        guard case .h264(let out) = payload else { return }
        recorder.append(out.frame)
    }

    @discardableResult
    public func finish() -> Recorder.Summary? {
        lock.lock()
        stopped = true
        lock.unlock()
        return recorder.finish()
    }
}
