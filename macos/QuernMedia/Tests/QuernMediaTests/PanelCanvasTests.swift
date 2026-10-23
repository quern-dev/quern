import CoreMedia
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

// MARK: - The tracker: which panels, and which is shown

@Test("two built-in panels among other framebuffers make a multi-panel device")
func findsTheBuiltInPanels() throws {
    // A Duo reports five display descriptors; three are not built-in panels.
    let tracker = try #require(PanelTracker(panels: [inner, nil, cover, nil, nil]))
    #expect(tracker.panels == [cover, inner], "in screenID order")
}

@Test("one built-in panel, or none, is not a multi-panel device", arguments: [
    [Panel?](arrayLiteral: cover, nil, nil), [Panel?](arrayLiteral: nil, nil), [Panel?](),
])
func onePanelIsAPhone(found: [Panel?]) {
    #expect(PanelTracker(panels: found) == nil)
}

@Test("the tracker follows a fold, and says when it changed")
func trackerFollowsAFold() throws {
    var tracker = try #require(PanelTracker(panels: [cover, inner]))
    let black = try #require(solid(width: 140, height: 200, bgra: 0xFF00_0000))
    let lit = try #require(TestSurface.make(width: 200, height: 280))

    let foldedRead = tracker.select([lit, black])
    let folded = try #require(foldedRead)
    #expect(folded.panel == cover && folded.changed && folded.surface === lit)
    #expect(tracker.select([lit, black])?.changed == false)
    let openRead = tracker.select([black, lit])
    let open = try #require(openRead)
    #expect(open.panel == inner && open.changed && open.surface === lit)
    // Mid-fold, neither lit: the panel shown last, not the cover.
    #expect(tracker.select([black, black])?.panel == inner)
}

@Test("a panel with no surface yet is never chosen, and does not size the canvas")
func surfacelessPanelDuringBoot() throws {
    var tracker = try #require(PanelTracker(panels: [cover, inner]))
    let lit = try #require(TestSurface.make(width: 200, height: 280))
    #expect(tracker.select([lit, nil])?.panel == cover)
    #expect(PanelTracker.canvasSize(of: [lit, nil]) == nil,
            "sized from one panel, the canvas could be smaller than the other")
    let other = try #require(TestSurface.make(width: 300, height: 100))
    let size = try #require(PanelTracker.canvasSize(of: [lit, other]))
    #expect(size.width == 300 && size.height == 200, "landscape: longest edge by longest short edge")
}

@Test("the panel shown before a reattach is carried into the next tracker")
func shownSurvivesAReattach() throws {
    var tracker = try #require(PanelTracker(panels: [cover, inner], shown: 3))
    let black = try #require(solid(width: 140, height: 200, bgra: 0xFF00_0000))
    #expect(tracker.select([black, black])?.panel == inner)
}

// MARK: - Upright, in the unfolded panel's shape

@Test("the canvas is the unfolded inner panel turned upright, whatever order the panels come in")
func canvasIsTheUnfoldedShape() throws {
    // A Duo's panels at a tenth of their size: cover 140x204, inner 200x286.
    let cover = try #require(TestSurface.make(width: 140, height: 204))
    let inner = try #require(TestSurface.make(width: 200, height: 286))
    for order in [[cover, inner], [inner, cover]] {
        let size = try #require(PanelTracker.canvasSize(of: order))
        #expect(size.width == 286 && size.height == 200)
    }
}

@Test("each interface orientation turns the panel the way take_screenshot does", arguments: [
    (UInt32(1), PanelRotation.none), (2, .upsideDown), (3, .clockwise), (4, .anticlockwise),
    (0, .none), (7, .none),
])
func rotationFromOrientation(orientation: UInt32, expected: PanelRotation) {
    #expect(PanelRotation(uiOrientation: orientation) == expected)
}

@Test("turning sideways swaps width and height; upside down does not")
func turnedSizes() {
    #expect(PanelRotation.clockwise.apply((200, 286)) == (286, 200))
    #expect(PanelRotation.anticlockwise.apply((200, 286)) == (286, 200))
    #expect(PanelRotation.upsideDown.apply((200, 286)) == (200, 286))
    #expect(PanelRotation.none.apply((200, 286)) == (200, 286))
}

@Test("an unfolded panel is turned clockwise to fill the canvas, its top left becoming top right")
func composeTurnsClockwise() throws {
    // Measured on a Duo: unfolded, the inner panel reports uiOrientation 3
    // and its framebuffer shows the interface 90 degrees anticlockwise.
    let canvas = try #require(PanelCanvas(width: 200, height: 100))
    let panel = try #require(solid(width: 100, height: 200, bgra: 0xFF00_0000))
    paint(panel, x: 0..<20, y: 0..<20, bgra: 0xFFFF_FFFF)
    let out = try #require(canvas.compose(panel, rotation: .clockwise))
    #expect(pixel(out, x: 190, y: 10) & 0x00FF_FFFF == 0x00FF_FFFF, "top left is now top right")
    #expect(pixel(out, x: 10, y: 10) & 0x00FF_FFFF == 0, "and filled the canvas, with no bars")
    #expect(pixel(out, x: 10, y: 90) & 0x00FF_FFFF == 0)
}

@Test("the folded cover sits in the unfolded shape with bars either side")
func coverInTheUnfoldedShape() throws {
    let canvas = try #require(PanelCanvas(width: 286, height: 200))
    let cover = try #require(solid(width: 140, height: 204, bgra: 0xFFFF_FFFF))
    let out = try #require(canvas.compose(cover, rotation: .none))
    #expect(pixel(out, x: 143, y: 100) & 0x00FF_FFFF == 0x00FF_FFFF)
    #expect(pixel(out, x: 20, y: 100) & 0x00FF_FFFF == 0, "a bar on the left")
    #expect(pixel(out, x: 265, y: 100) & 0x00FF_FFFF == 0, "and on the right")
}

// MARK: - Lit: what the grid catches

@Test("a dark screen whose only content is a small mark on the grid is lit", arguments: [
    UInt32(0xFF00_0014),  // blue 20
    UInt32(0xFF00_1400),  // green 20
    UInt32(0xFF14_0000),  // red 20
])
func smallMarkInOneChannelIsLit(colour: UInt32) throws {
    // 320x320: grid point (gx, gy) = (0, 0) is pixel (10, 10).
    let surface = try #require(solid(width: 320, height: 320, bgra: 0xFF00_0000))
    paint(surface, x: 8..<13, y: 8..<13, bgra: colour)
    #expect(surfaceIsLit(surface))
}

@Test("a level of 8 or less is black, as an unlit panel's noise is")
func nearBlackIsUnlit() throws {
    let surface = try #require(solid(width: 320, height: 320, bgra: 0xFF08_0808))
    #expect(!surfaceIsLit(surface))
}

@Test("a mark between grid points is not seen: the grid is a sample, not a scan")
func offGridMarkIsMissed() throws {
    let surface = try #require(solid(width: 320, height: 320, bgra: 0xFF00_0000))
    paint(surface, x: 0..<3, y: 0..<3, bgra: 0xFFFF_FFFF)
    #expect(!surfaceIsLit(surface))
}

@Test("a surface it cannot read reads as lit: cannot tell, not black")
func unreadableFormatReadsLit() throws {
    let props: [IOSurfacePropertyKey: Any] = [
        .width: 64, .height: 64, .bytesPerElement: 2,
        .pixelFormat: kCVPixelFormatType_16Gray,
    ]
    let surface = try #require(IOSurface(properties: props))
    #expect(surfaceIsLit(surface))
}

// MARK: - Letterboxing is paid only by a frame that is encoded

private final class Wanting: FrameSink {
    var wantsFrames: Bool
    init(_ wants: Bool) { wantsFrames = wants }
    func receive(_ payload: EncodedPayload) {}
}

@Test("a frame nobody wants, or the throttle drops, is never letterboxed")
func preparationIsDeferred() throws {
    let surface = try #require(TestSurface.make(width: 200, height: 280))
    var prepared = 0
    func frame(_ i: Int) -> CapturedFrame {
        CapturedFrame(surface: surface, time: CMTime(value: CMTimeValue(i * 10), timescale: 600),
                      timeAccuracy: .arrival, prepare: { prepared += 1; return surface })
    }
    let pipeline = StreamPipeline(codec: .mjpeg, fps: 15, maxDimension: 0)
    defer { pipeline.invalidate() }
    let sink = Wanting(false)
    pipeline.add(sink)
    for i in 0..<8 { pipeline.consume(frame(i)) }
    #expect(prepared == 0, "an idle preview must stay free on a Duo")

    sink.wantsFrames = true
    for i in 8..<16 { pipeline.consume(frame(i)) }   // 60 fps offered, 15 encoded
    #expect(prepared == pipeline.framesEncoded && prepared < 8)
}

@Test("the encoded frame is the prepared one, or the raw one when preparing fails")
func resolvedFrame() throws {
    let raw = try #require(TestSurface.make(width: 200, height: 280))
    let boxed = try #require(TestSurface.make(width: 300, height: 300))
    let t = CMTime(value: 1, timescale: 600)
    #expect(CapturedFrame(surface: raw, time: t, timeAccuracy: .arrival,
                          prepare: { boxed }).resolved().surface === boxed)
    #expect(CapturedFrame(surface: raw, time: t, timeAccuracy: .arrival,
                          prepare: { nil }).resolved().surface === raw,
            "no frames at all reads as a still screen")
    #expect(CapturedFrame(surface: raw, time: t, timeAccuracy: .arrival).resolved().surface === raw)
}

private func paint(_ surface: IOSurface, x: Range<Int>, y: Range<Int>, bgra: UInt32) {
    surface.lock(options: [], seed: nil)
    defer { surface.unlock(options: [], seed: nil) }
    for row in y {
        let p = surface.baseAddress.advanced(by: row * surface.bytesPerRow)
            .assumingMemoryBound(to: UInt32.self)
        for col in x { p[col] = bgra }
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
