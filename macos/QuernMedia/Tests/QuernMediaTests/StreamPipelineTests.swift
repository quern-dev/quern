import CoreMedia
import Foundation
import Testing
@testable import QuernMedia

/// A sink that records what it was handed, and can decline frames.
private final class SpySink: FrameSink {
    var wantsFrames: Bool
    private(set) var received: [EncodedPayload] = []

    init(wantsFrames: Bool = true) { self.wantsFrames = wantsFrames }
    func receive(_ payload: EncodedPayload) { received.append(payload) }

    var keyframeCount: Int { received.filter(\.isKeyframe).count }
}

/// Builds a frame at frame index `i` of a `fps` stream.
///
/// Deliberately integer arithmetic. `CMTime(seconds:preferredTimescale:)`
/// truncates: 11/60 is 0.18333333333333332 in binary, times 600 is
/// 109.99999999999999, which becomes 109 rather than 110 -- a frame stamped
/// one tick early. That made a throttle test drop frames 11 and 22 and look
/// like a bug in the throttle.
private func captured(_ surface: IOSurface, frame i: Int, fps: Int = 60) -> CapturedFrame {
    CapturedFrame(
        surface: surface,
        time: CMTime(value: CMTimeValue(i * (600 / fps)), timescale: 600),
        timeAccuracy: .reported
    )
}

@Test("no interested sinks means no encoding at all")
func idleCostsNothing() throws {
    // The property that makes an unwatched preview free: if nothing wants
    // frames, the encoder is never touched.
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .h264, fps: 30, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = SpySink(wantsFrames: false)
    pipeline.add(sink)

    for i in 0..<10 { pipeline.consume(captured(surface, frame: i)) }

    #expect(pipeline.framesOffered == 10)
    #expect(pipeline.framesEncoded == 0, "encoded frames nobody asked for")
    #expect(sink.received.isEmpty)
}

@Test("a sink that wants frames gets them")
func deliversToInterestedSinks() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .h264, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = SpySink()
    pipeline.add(sink)

    for i in 0..<5 { pipeline.consume(captured(surface, frame: i)) }
    #expect(sink.received.count == 5)
    #expect(pipeline.bytesProduced > 0)
}

@Test("every interested sink sees the same frame")
func fansOutToAllSinks() throws {
    // The farm case: one source, many consumers. Encoding happens once.
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .h264, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    let a = SpySink(), b = SpySink(), inactive = SpySink(wantsFrames: false)
    pipeline.add(a); pipeline.add(b); pipeline.add(inactive)

    for i in 0..<4 { pipeline.consume(captured(surface, frame: i)) }

    #expect(a.received.count == 4)
    #expect(b.received.count == 4)
    #expect(inactive.received.isEmpty)
    #expect(pipeline.framesEncoded == 4, "should encode once, not once per sink")
}

@Test("the throttle applies to the pipeline, not to each sink")
func throttleIsShared() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .h264, fps: 30, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = SpySink()
    pipeline.add(sink)

    // A 60 fps source against a 30 fps pipeline over one second.
    for i in 0..<60 { pipeline.consume(captured(surface, frame: i)) }
    #expect(pipeline.framesOffered == 60)
    #expect(sink.received.count >= 29 && sink.received.count <= 31,
            "expected ~30 encoded, got \(sink.received.count)")
}

@Test("requesting a keyframe produces one on the next encoded frame")
func keyframeOnRequest() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .h264, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = SpySink()
    pipeline.add(sink)

    for i in 0..<6 { pipeline.consume(captured(surface, frame: i)) }
    let before = sink.keyframeCount

    // What happens when a viewer attaches mid-stream.
    pipeline.requestKeyframe()
    pipeline.consume(captured(surface, frame: 6))

    #expect(sink.keyframeCount == before + 1,
            "a late joiner would have decoded nothing")
    #expect(sink.received.last?.isKeyframe == true)
}

@Test("a recording keeps running after the last viewer leaves")
func recordingOutlivesViewers() throws {
    // The case the spike's shape made awkward: recording and serving are
    // peers, so one going away must not stop the other.
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("quern-pipe-\(UUID().uuidString).mp4")
    defer { try? FileManager.default.removeItem(at: url) }

    let pipeline = StreamPipeline(codec: .h264, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    let viewer = SpySink()
    let recording = RecordingSink(recorder: try Recorder(url: url))
    pipeline.add(viewer)
    pipeline.add(recording)
    pipeline.requestKeyframe()

    for i in 0..<10 { pipeline.consume(captured(surface, frame: i)) }
    viewer.wantsFrames = false  // the browser tab closes
    for i in 10..<30 { pipeline.consume(captured(surface, frame: i)) }

    let summary = try #require(recording.finish())
    #expect(viewer.received.count == 10, "viewer kept receiving after leaving")
    #expect(summary.framesWritten >= 29,
            "recording stopped with the viewer: only \(summary.framesWritten) frames")
}

@Test("MJPEG frames all stand alone")
func mjpegFramesAreIndependent() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .mjpeg, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = SpySink()
    pipeline.add(sink)

    for i in 0..<4 { pipeline.consume(captured(surface, frame: i)) }
    #expect(sink.received.count == 4)
    #expect(sink.keyframeCount == 4, "every JPEG is independently decodable")
}

@Test("a removed sink stops receiving")
func removeStopsDelivery() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let pipeline = StreamPipeline(codec: .h264, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = SpySink()
    pipeline.add(sink)
    pipeline.consume(captured(surface, frame: 0))
    pipeline.remove(sink)
    pipeline.consume(captured(surface, frame: 1))
    #expect(sink.received.count == 1)
}

@Test("a keyframe request survives an encode that produced nothing")
func keyframeRequestSurvivesAFailedEncode() throws {
    // The request used to be cleared as soon as a frame was due, before the
    // encoder had produced anything. One failed encode swallowed it, and the
    // viewer that asked decoded nothing until the next periodic IDR.
    let surface = try #require(TestSurface.make(width: 64, height: 64))
    let pipeline = StreamPipeline(
        codec: .h264, fps: 1000, maxDimension: 0, quality: 0.6, bitrate: 400_000
    )
    defer { pipeline.invalidate() }

    pipeline.add(SpySink())

    var forced: [Bool] = []
    var failNext = true
    pipeline.encodeOverride = { _, force in
        forced.append(force)
        if failNext { failNext = false; return nil }
        return .jpeg(Data([0xFF, 0xD8, 0xFF, 0xD9]))
    }

    pipeline.requestKeyframe()
    pipeline.consume(CapturedFrame(
        surface: surface, time: CMTime(value: 0, timescale: 600), timeAccuracy: .reported
    ))
    pipeline.consume(CapturedFrame(
        surface: surface, time: CMTime(value: 600, timescale: 600), timeAccuracy: .reported
    ))

    #expect(forced == [true, true],
            "the request was dropped by the encode that failed: \(forced)")
}
