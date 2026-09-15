import Foundation
import Testing
@testable import QuernMedia

@Test("scale target preserves aspect and forces even dimensions", arguments: [
    (1206, 2622, 900, 414, 900),   // iPhone 16 Pro simulator
    (828, 1792, 900, 416, 900),    // iPhone 11 over USB
    (1668, 2420, 900, 620, 900),   // iPad Pro 11"
    (400, 300, 0, 400, 300),       // 0 = native, untouched
    (100, 100, 900, 100, 100),     // already smaller than the cap
])
func scaleTarget(w: Int, h: Int, maxDim: Int, expectW: Int, expectH: Int) {
    let t = ScaleTarget.fit(width: w, height: h, maxDimension: maxDim)
    #expect(t.width == expectW && t.height == expectH,
            "\(w)x\(h) cap \(maxDim) -> \(t.width)x\(t.height)")
    #expect(t.width % 2 == 0 && t.height % 2 == 0, "dimensions must be even")
}

@Test("odd source dimensions still produce even output")
func oddInputsRoundToEven() {
    let t = ScaleTarget.fit(width: 1001, height: 777, maxDimension: 0)
    #expect(t.width % 2 == 0 && t.height % 2 == 0)
}

@Test("encodes a synthetic surface to a real JPEG, no device required")
func encodesSyntheticSurface() throws {
    let surface = try #require(TestSurface.make(width: 828, height: 1792))
    let encoder = JPEGEncoder(maxDimension: 900, quality: 0.6)
    defer { encoder.invalidate() }

    let data = try #require(encoder.encode(surface), "encoder returned nothing")
    #expect(TestSurface.isJPEG(data), "output is not JPEG")

    let size = try #require(TestSurface.imageSize(data))
    #expect(size.width == 416 && size.height == 900,
            "expected the scaled size, got \(size.width)x\(size.height)")
    // A uniform or empty frame would still be "a JPEG"; the drawn detail
    // means a real encode is comfortably above this.
    #expect(data.count > 2000, "suspiciously small for a detailed frame: \(data.count)")
}

@Test("maxDimension 0 encodes at native size")
func nativeSizeWhenUncapped() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = JPEGEncoder(maxDimension: 0, quality: 0.6)
    defer { encoder.invalidate() }
    let data = try #require(encoder.encode(surface))
    let size = try #require(TestSurface.imageSize(data))
    #expect(size.width == 320 && size.height == 240)
}

@Test("the session survives repeated frames and adapts when the size changes")
func sessionReuseAndResize() throws {
    let encoder = JPEGEncoder(maxDimension: 200, quality: 0.5)
    defer { encoder.invalidate() }

    let small = try #require(TestSurface.make(width: 400, height: 300))
    for _ in 0..<5 {
        let data = try #require(encoder.encode(small))
        let size = try #require(TestSurface.imageSize(data))
        #expect(size.width == 200 && size.height == 150)
    }
    // A source can change resolution mid-stream — a device rotating, or a
    // different simulator. The session has to be rebuilt, not reused.
    let tall = try #require(TestSurface.make(width: 300, height: 600))
    let tallData = try #require(encoder.encode(tall))
    let tallSize = try #require(TestSurface.imageSize(tallData))
    #expect(tallSize.width == 100 && tallSize.height == 200)
}

@Test("invalidate is safe to call twice")
func invalidateIsIdempotent() {
    let encoder = JPEGEncoder(maxDimension: 100, quality: 0.5)
    encoder.invalidate()
    encoder.invalidate()
}
