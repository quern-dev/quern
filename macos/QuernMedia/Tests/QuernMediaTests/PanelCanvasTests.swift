import CoreVideo
import Foundation
import IOSurface
import Testing
@testable import QuernMedia

// An iPhone Duo's panels, as CoreSimulator reports them (#538).
private let cover = Panel(screenID: 1, deviceName: "primary")
private let inner = Panel(screenID: 3, deviceName: "primary-1")

@Test("the lit panel is shown, whichever is larger")
func showsTheLitPanel() {
    #expect(PanelChoice.choose([cover, inner], lit: [true, false], last: 3) == 0)
    #expect(PanelChoice.choose([cover, inner], lit: [false, true], last: 1) == 1)
}

@Test("neither or both lit keeps the panel shown last", arguments: [[false, false], [true, true]])
func ambiguousKeepsTheLast(lit: [Bool]) {
    #expect(PanelChoice.choose([cover, inner], lit: lit, last: 3) == 1)
    #expect(PanelChoice.choose([cover, inner], lit: lit, last: 1) == 0)
}

@Test("with nothing shown yet, an ambiguous read shows the cover, which a Duo boots on")
func ambiguousFirstLookIsTheCover() {
    #expect(PanelChoice.choose([inner, cover], lit: [false, false], last: nil) == 1)
}

@Test("a mismatched read chooses nothing rather than guessing")
func mismatchedReadChoosesNothing() {
    #expect(PanelChoice.choose([cover, inner], lit: [true], last: nil) == nil)
    #expect(PanelChoice.choose([], lit: [], last: nil) == nil)
}

@Test("the canvas fits the widest and the tallest panel, in even dimensions")
func canvasFitsEveryPanel() throws {
    let size = try #require(PanelChoice.canvasSize([(1398, 2034), (2007, 2853)]))
    #expect(size.width == 2008 && size.height == 2854)
    #expect(PanelChoice.canvasSize([]) == nil)
}

@Test("a black surface is unlit; one with content is lit")
func litDetection() throws {
    let black = try #require(solid(width: 200, height: 300, bgra: 0xFF00_0000))
    #expect(!surfaceIsLit(black))
    let drawn = try #require(TestSurface.make(width: 200, height: 300))
    #expect(surfaceIsLit(drawn))
}

@Test("a smaller panel is letterboxed into the canvas, with black bars")
func letterboxesASmallerPanel() throws {
    let canvas = try #require(PanelCanvas(width: 400, height: 400))
    let white = try #require(solid(width: 200, height: 400, bgra: 0xFFFF_FFFF))
    let out = try #require(canvas.compose(white))
    #expect(IOSurfaceGetWidth(out) == 400 && IOSurfaceGetHeight(out) == 400)
    #expect(pixel(out, x: 200, y: 200) & 0x00FF_FFFF == 0x00FF_FFFF, "the panel, centred")
    #expect(pixel(out, x: 5, y: 200) & 0x00FF_FFFF == 0, "a bar either side")
    #expect(pixel(out, x: 394, y: 200) & 0x00FF_FFFF == 0)
}

@Test("a fold back to the smaller panel does not show the larger one's edges")
func barsAreClearedWhenThePanelChanges() throws {
    let canvas = try #require(PanelCanvas(width: 400, height: 400))
    let large = try #require(solid(width: 400, height: 400, bgra: 0xFFFF_FFFF))
    let small = try #require(solid(width: 200, height: 400, bgra: 0xFF80_8080))
    // Enough frames that the pool hands back a buffer the large panel drew in.
    for _ in 0..<6 { _ = canvas.compose(large) }
    for _ in 0..<6 {
        let out = try #require(canvas.compose(small))
        #expect(pixel(out, x: 5, y: 200) & 0x00FF_FFFF == 0)
    }
}

/// A BGRA surface filled with one colour.
private func solid(width: Int, height: Int, bgra: UInt32) -> IOSurface? {
    let props: [IOSurfacePropertyKey: Any] = [
        .width: width, .height: height, .bytesPerElement: 4,
        .pixelFormat: kCVPixelFormatType_32BGRA,
    ]
    guard let surface = IOSurface(properties: props) else { return nil }
    surface.lock(options: [], seed: nil)
    defer { surface.unlock(options: [], seed: nil) }
    for y in 0..<height {
        let row = surface.baseAddress.advanced(by: y * surface.bytesPerRow)
            .assumingMemoryBound(to: UInt32.self)
        for x in 0..<width { row[x] = bgra }
    }
    return surface
}

private func pixel(_ surface: IOSurface, x: Int, y: Int) -> UInt32 {
    IOSurfaceLock(surface, .readOnly, nil)
    defer { IOSurfaceUnlock(surface, .readOnly, nil) }
    let row = IOSurfaceGetBaseAddress(surface).advanced(by: y * IOSurfaceGetBytesPerRow(surface))
    return row.assumingMemoryBound(to: UInt32.self)[x]
}
