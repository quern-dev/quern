import AVFoundation
import CoreMedia
import Foundation
import Testing
@testable import QuernMedia

private func tempURL() -> URL {
    FileManager.default.temporaryDirectory
        .appendingPathComponent("quern-rec-\(UUID().uuidString).mp4")
}

private func captured(_ surface: IOSurface, at t: Double) -> CapturedFrame {
    CapturedFrame(
        surface: surface,
        time: CMTime(seconds: t, preferredTimescale: 600),
        timeAccuracy: .reported
    )
}

/// Reads a written file back the way a player would, rather than trusting the
/// writer's own accounting.
private func assetDuration(_ url: URL) async throws -> Double {
    let asset = AVURLAsset(url: url)
    return try await CMTimeGetSeconds(asset.load(.duration))
}

@Test("writes a real mp4 that reads back at wall-clock duration")
func recordsWallClockDuration() async throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    let url = tempURL()
    defer { try? FileManager.default.removeItem(at: url) }
    let recorder = try Recorder(url: url)

    // Five seconds of wall time at 30 fps.
    var t = 0.0
    while t < 5.0 {
        let out = try #require(encoder.encode(captured(surface, at: t)))
        recorder.append(out.frame)
        t += 1.0 / 30.0
    }
    let summary = try #require(
        recorder.finish(), "finish reported: \(String(describing: recorder.failure))"
    )
    #expect(summary.framesWritten > 140, "expected ~150 frames, got \(summary.framesWritten)")
    #expect(summary.framesDropped == 0)

    let duration = try await assetDuration(url)
    #expect(abs(duration - 5.0) < 0.2, "expected ~5s on disk, got \(duration)")
}

@Test("an idle gap survives into the file as elapsed time")
func idleGapIsPreserved() async throws {
    // The property a test timeline depends on. A frame-counter PTS would
    // collapse the pause and the recording would no longer match the run.
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    let url = tempURL()
    defer { try? FileManager.default.removeItem(at: url) }
    let recorder = try Recorder(url: url)

    for i in 0..<30 {  // one second of activity
        let out = try #require(encoder.encode(captured(surface, at: Double(i) / 30)))
        recorder.append(out.frame)
    }
    for i in 0..<30 {  // ...six seconds of nothing, then activity resumes
        let out = try #require(encoder.encode(captured(surface, at: 7.0 + Double(i) / 30)))
        recorder.append(out.frame)
    }
    _ = recorder.finish()

    let duration = try await assetDuration(url)
    #expect(duration > 6.5, "the gap was collapsed: duration \(duration)")
    #expect(duration < 8.5, "duration ran long: \(duration)")
}

@Test("recording refuses to start on a non-keyframe")
func mustOpenOnAKeyframe() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    let url = tempURL()
    defer { try? FileManager.default.removeItem(at: url) }
    let recorder = try Recorder(url: url)

    // Produce a non-keyframe by encoding past the session's opening IDR, then
    // offer only that to a fresh recorder.
    _ = encoder.encode(captured(surface, at: 0))
    var nonKey: EncodedFrame?
    for i in 1...5 where nonKey == nil {
        if let out = encoder.encode(captured(surface, at: Double(i) / 30)),
           !out.frame.isKeyframe { nonKey = out.frame }
    }
    let interFrame = try #require(nonKey, "encoder produced no non-keyframe to test with")

    #expect(recorder.append(interFrame) == false,
            "a file opening on a non-keyframe is undecodable")
    #expect(recorder.finish() == nil, "nothing was recorded, so there is no summary")
}

@Test("finish before anything was appended does not produce a file summary")
func finishWithNoFramesIsSafe() throws {
    let url = tempURL()
    defer { try? FileManager.default.removeItem(at: url) }
    let recorder = try Recorder(url: url)
    #expect(recorder.finish() == nil)
    #expect(recorder.finish() == nil, "finish must be idempotent")
}

@Test("appending after finish is refused rather than crashing")
func appendAfterFinishIsRefused() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    let url = tempURL()
    defer { try? FileManager.default.removeItem(at: url) }
    let recorder = try Recorder(url: url)

    let first = try #require(encoder.encode(captured(surface, at: 0)))
    recorder.append(first.frame)
    _ = recorder.finish()

    let later = try #require(encoder.encode(captured(surface, at: 1), forceKeyframe: true))
    #expect(recorder.append(later.frame) == false)
}


@Test("a finish that times out reports no summary, and says why")
func finishTimeoutIsNotSilentSuccess() throws {
    // The file has no moov atom until finishWriting completes, so a Summary
    // here would describe a recording that cannot be opened. Measured on CI,
    // where the write lost a race it always won on a developer machine.
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    let url = tempURL()
    defer { try? FileManager.default.removeItem(at: url) }
    let recorder = try Recorder(url: url)

    let out = try #require(encoder.encode(captured(surface, at: 0)))
    #expect(recorder.append(out.frame))

    // Completion is withheld rather than raced. A zero-second deadline
    // against the real writer is decided by whichever wins, and on a fast
    // machine that is the writer -- so the test passed without exercising
    // the timeout at all.
    recorder.finishWritingOverride = { _ in }

    #expect(recorder.finish(timeout: 0.2) == nil)
    guard case .finishTimedOut = try #require(recorder.failure) else {
        Issue.record("expected finishTimedOut, got \(String(describing: recorder.failure))")
        return
    }
}
