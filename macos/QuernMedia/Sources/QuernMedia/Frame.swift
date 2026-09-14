import CoreMedia
import Foundation
import IOSurface

/// One captured frame, and when it happened.
///
/// The timestamp is why this is a struct rather than a bare `IOSurface`.
/// Streaming never needed it — frames go out as they arrive and nobody asks
/// when. Recording does, and the sources differ in what they can honestly
/// report, which is the single most important fact this protocol encodes.
public struct CapturedFrame {
    public let surface: IOSurface
    /// Host clock (`CMClockGetHostTimeClock`), so every source, the encoder
    /// and the recorder share one timebase.
    public let time: CMTime
    /// Whether `time` is the moment the frame was composited, or merely the
    /// moment we noticed it.
    public let timeAccuracy: TimeAccuracy

    public init(surface: IOSurface, time: CMTime, timeAccuracy: TimeAccuracy) {
        self.surface = surface
        self.time = time
        self.timeAccuracy = timeAccuracy
    }
}

/// How much to trust a frame's timestamp.
///
/// Not decoration. A physical device's capture sample buffer carries a real
/// presentation timestamp from AVFoundation. A simulator's framebuffer
/// callback carries nothing at all — it says only "a frame happened" — so the
/// best available answer is the host clock read on arrival, which is later
/// and noisier than the true composite by an unmeasured amount.
///
/// Anything aligning video against logs needs to know which it is holding.
public enum TimeAccuracy: Sendable {
    /// The source reported when the frame was produced.
    case reported
    /// We stamped it when we received it. Late by an unknown amount.
    case arrival
}

/// A source of raw frames we encode ourselves: simulators, physical iOS.
public protocol FrameSource: AnyObject {
    func start() throws
    func stop()
}

/// One already-compressed frame from a source that encodes on our behalf.
public struct EncodedFrame {
    public let sample: CMSampleBuffer
    public let time: CMTime
    public let isKeyframe: Bool

    public init(sample: CMSampleBuffer, time: CMTime, isKeyframe: Bool) {
        self.sample = sample
        self.time = time
        self.isKeyframe = isKeyframe
    }
}

/// A source that hands us frames already encoded — Android, today.
///
/// Deliberately separate from `FrameSource` rather than folded into it.
/// Android arrives compressed, from a subprocess or an on-device server, and
/// forcing it through a raw-frame protocol would mean decoding it only to
/// re-encode, discarding the efficiency that makes it worth having.
///
/// `requestKeyframe()` is on the protocol even though the current Android
/// path implements it badly. `adb screenrecord` emits one IDR per session
/// with no way to ask for another, so its only lever is restarting the
/// subprocess. An on-device MediaCodec encoder — which quern is positioned
/// to ship, since it already installs an APK — does it properly via
/// `PARAMETER_KEY_REQUEST_SYNC_FRAME`. Designing the protocol around
/// screenrecord's limits would bake in a constraint we have a route past.
public protocol EncodedFrameSource: AnyObject {
    func start() throws
    func stop()
    /// Ask for a keyframe as soon as possible, so a late viewer can decode.
    /// Implementations that cannot honour this should say so rather than
    /// silently do nothing.
    func requestKeyframe() -> KeyframeRequestResult
}

public enum KeyframeRequestResult: Sendable, Equatable {
    /// The encoder will emit a keyframe promptly.
    case honoured
    /// Satisfied by restarting the source: expect a brief interruption.
    case viaRestart
    /// Not supported. A late viewer must wait for a periodic keyframe.
    case unsupported
}
