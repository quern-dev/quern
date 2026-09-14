import CoreMedia
import Foundation
import Testing
@testable import QuernMedia

private func frame(_ surface: IOSurface, atSeconds t: Double) -> CapturedFrame {
    CapturedFrame(
        surface: surface,
        time: CMTime(seconds: t, preferredTimescale: 600),
        timeAccuracy: .reported
    )
}

@Test("encodes a synthetic surface to H.264, no device required")
func encodesToH264() throws {
    let surface = try #require(TestSurface.make(width: 640, height: 480))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 1_000_000, expectedFPS: 30)
    defer { encoder.invalidate() }

    let out = try #require(encoder.encode(frame(surface, atSeconds: 0)))
    #expect(out.annexB.starts(with: AnnexB.startCode), "not Annex-B framed")
    #expect(out.frame.isKeyframe, "the first frame of a session must be an IDR")
}

@Test("a keyframe carries its parameter sets inline")
func keyframeCarriesParameterSets() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }

    let out = try #require(encoder.encode(frame(surface, atSeconds: 0)))
    // SPS first, so a viewer joining on this frame can configure a decoder
    // without a side channel.
    #expect(AnnexB.firstNALType(out.annexB) == 7, "expected SPS to lead a keyframe")
}

@Test("keyframes come on demand, which is the whole point")
func forceKeyframeWorks() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }

    _ = encoder.encode(frame(surface, atSeconds: 0))  // session opens on an IDR

    // Ordinary frames should not be keyframes, or "on demand" means nothing.
    var sawNonKeyframe = false
    for i in 1...5 {
        let out = try #require(encoder.encode(frame(surface, atSeconds: Double(i) / 30)))
        if !out.frame.isKeyframe { sawNonKeyframe = true }
    }
    #expect(sawNonKeyframe, "every frame was a keyframe; the interval is not being honoured")

    let forced = try #require(
        encoder.encode(frame(surface, atSeconds: 0.2), forceKeyframe: true)
    )
    #expect(forced.frame.isKeyframe, "ForceKeyFrame did not produce an IDR")
    #expect(AnnexB.firstNALType(forced.annexB) == 7,
            "a forced keyframe must also re-send parameter sets")
}

@Test("presentation timestamps come from the frame, not a counter")
func timestampsFollowTheFrame() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }

    // Deliberately irregular, including a long gap. A frame-counter PTS would
    // space these evenly and erase the pause -- the bug that made the first
    // recordings play at the wrong speed.
    let times: [Double] = [10.0, 10.033, 10.066, 16.5, 16.533]
    var got: [Double] = []
    for t in times {
        let out = try #require(encoder.encode(frame(surface, atSeconds: t)))
        got.append(CMTimeGetSeconds(out.frame.time))
    }
    for (expected, actual) in zip(times, got) {
        #expect(abs(expected - actual) < 0.002,
                "expected PTS \(expected), got \(actual)")
    }
    // The 6.4s idle must survive into the encoded timeline.
    #expect(got[3] - got[2] > 6.0, "the gap was collapsed")
}

@Test("respects the scale cap")
func scalesDown() throws {
    let surface = try #require(TestSurface.make(width: 1206, height: 2622))
    let encoder = H264Encoder(maxDimension: 900, bitrate: 1_000_000, expectedFPS: 30)
    defer { encoder.invalidate() }

    let out = try #require(encoder.encode(frame(surface, atSeconds: 0)))
    let fd = try #require(CMSampleBufferGetFormatDescription(out.frame.sample))
    let dims = CMVideoFormatDescriptionGetDimensions(fd)
    #expect(dims.width == 414 && dims.height == 900,
            "expected 414x900, got \(dims.width)x\(dims.height)")
}

@Test("interframe coding is actually happening")
func laterFramesAreSmallerThanKeyframes() throws {
    let surface = try #require(TestSurface.make(width: 640, height: 480))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 1_000_000, expectedFPS: 30)
    defer { encoder.invalidate() }

    let key = try #require(encoder.encode(frame(surface, atSeconds: 0)))
    var laterSizes: [Int] = []
    for i in 1...4 {
        let out = try #require(encoder.encode(frame(surface, atSeconds: Double(i) / 30)))
        if !out.frame.isKeyframe { laterSizes.append(out.annexB.count) }
    }
    // Identical content frame to frame, so the residual should be tiny. If a
    // later frame rivals the keyframe, prediction is not working and the
    // bitrate win over MJPEG evaporates.
    let smallest = try #require(laterSizes.min())
    #expect(smallest < key.annexB.count / 4,
            "expected a small residual, got \(smallest) against a \(key.annexB.count) keyframe")
}
