import Testing
@testable import QuernMedia

/// Feeds a throttle a steady source rate and reports frames actually encoded.
private func deliveredRate(sourceFPS: Double, targetFPS: Double, seconds: Double) -> Double {
    var throttle = FrameThrottle(fps: targetFPS)
    let step = 1.0 / sourceFPS
    var encoded = 0
    var t = 0.0
    while t < seconds {
        if throttle.shouldEncode(at: t) { encoded += 1 }
        t += step
    }
    return Double(encoded) / seconds
}

@Test("a 30 fps target against a 60 fps source delivers 30, not 24")
func doesNotAliasAgainstAFasterSource() {
    // The regression this type exists for. A delay-based throttle skips every
    // second frame here and lands around 24.5.
    let rate = deliveredRate(sourceFPS: 60, targetFPS: 30, seconds: 10)
    #expect(rate > 29.0 && rate <= 30.5, "expected ~30 fps, got \(rate)")
}

@Test("awkward source rates still average out to the target", arguments: [
    (49.0, 30.0), (60.0, 15.0), (59.94, 24.0), (120.0, 30.0), (31.0, 30.0),
])
func averagesToTargetForAwkwardRatios(source: Double, target: Double) {
    let rate = deliveredRate(sourceFPS: source, targetFPS: target, seconds: 20)
    #expect(abs(rate - target) < 1.0, "source \(source) -> target \(target), got \(rate)")
}

@Test("a source slower than the target passes every frame through")
func neverInventsFrames() {
    let rate = deliveredRate(sourceFPS: 10, targetFPS: 60, seconds: 10)
    #expect(abs(rate - 10.0) < 0.5, "expected all 10 fps through, got \(rate)")
}

@Test("fps of zero or less disables throttling")
func zeroDisablesThrottling() {
    // #expect cannot call a mutating member directly, so results are hoisted.
    var throttle = FrameThrottle(fps: 0)
    let a = throttle.shouldEncode(at: 0)
    let b = throttle.shouldEncode(at: 0)
    let c = throttle.shouldEncode(at: 0.0001)
    #expect(a && b && c)
}

@Test("the first frame is always encoded")
func firstFrameIsNeverDropped() {
    var throttle = FrameThrottle(fps: 30)
    let encoded = throttle.shouldEncode(at: 1_234_567.0)
    #expect(encoded)
}

@Test("an idle gap resyncs instead of emitting a catch-up burst")
func idleGapDoesNotBurst() {
    var throttle = FrameThrottle(fps: 30)
    let first = throttle.shouldEncode(at: 0)
    #expect(first)
    // Screen goes quiet for a minute, then frames resume at 60 fps.
    var encoded = 0
    var t = 60.0
    while t < 60.5 {
        if throttle.shouldEncode(at: t) { encoded += 1 }
        t += 1.0 / 60.0
    }
    // Half a second at 30 fps is ~15 frames. A naive deadline loop would
    // fire on every frame until it caught up on 60 seconds of arrears.
    #expect(encoded <= 17, "expected ~15 frames after the gap, got \(encoded)")
}


@Test("a source at exactly the target rate loses no frames")
func matchedRatesPassEverything() {
    // Regression: the deadline accumulates by addition while frame times are
    // computed independently, so without tolerance the two drift and frames
    // fall a hair short of their deadline. This lost 2 frames in 30.
    var throttle = FrameThrottle(fps: 60)
    var encoded = 0
    for i in 0..<600 {
        if throttle.shouldEncode(at: Double(i) / 60.0) { encoded += 1 }
    }
    #expect(encoded == 600, "expected every frame, got \(encoded)")
}

@Test("tolerance does not let a faster source through", arguments: [61.0, 65.0, 90.0])
func toleranceDoesNotLeak(sourceFPS: Double) {
    // The slack must not become a loophole: a source above the target should
    // still be held down to it.
    var throttle = FrameThrottle(fps: 60)
    var encoded = 0
    var t = 0.0
    while t < 10.0 {
        if throttle.shouldEncode(at: t) { encoded += 1 }
        t += 1.0 / sourceFPS
    }
    #expect(Double(encoded) / 10.0 <= 61.0,
            "throttle leaked: \(Double(encoded) / 10.0) fps from a \(sourceFPS) source")
}
