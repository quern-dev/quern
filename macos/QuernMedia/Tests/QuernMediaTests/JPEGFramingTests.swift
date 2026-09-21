import Foundation
import Testing
@testable import QuernMedia

private let soi = Data([0xFF, 0xD8])
private let eoi = Data([0xFF, 0xD9])

private func jpeg(_ payload: [UInt8]) -> Data { soi + Data(payload) + eoi }

@Test("a frame split across chunks is reassembled")
func splitFrameIsReassembled() {
    // The case that matters: TCP does not respect frame boundaries, and a
    // parser that only looks inside one chunk yields nothing forever — which
    // is indistinguishable from an idle screen.
    var framing = JPEGFraming()
    let frame = jpeg([0x01, 0x02, 0x03, 0x04])

    #expect(framing.append(frame[0..<3]).isEmpty)
    #expect(framing.append(frame[3..<5]).isEmpty)
    let done = framing.append(frame[5...])
    #expect(done.count == 1)
    #expect(done.first == frame)
}

@Test("several frames in one chunk all come back, in order")
func multipleFramesInOneChunk() {
    var framing = JPEGFraming()
    let a = jpeg([0xAA]), b = jpeg([0xBB]), c = jpeg([0xCC])
    let frames = framing.append(a + b + c)
    #expect(frames == [a, b, c])
    #expect(framing.pendingBytes == 0)
}

@Test("bytes before the first frame are discarded, not accumulated")
func leadingGarbageIsDropped() {
    // A stream can open with multipart headers. Keeping them would mean the
    // buffer only ever grows, and the first frame would carry a prefix that
    // is not a JPEG.
    var framing = JPEGFraming()
    let header = Data("--quernframe\r\nContent-Type: image/jpeg\r\n\r\n".utf8)
    let frame = jpeg([0x42])
    let frames = framing.append(header + frame)
    #expect(frames == [frame], "the header leaked into the frame or blocked it")
    #expect(framing.pendingBytes == 0)
}

@Test("a partial frame is held, and the buffer does not grow past it")
func partialFrameIsHeldWithoutGrowing() {
    var framing = JPEGFraming()
    let noise = Data(repeating: 0x00, count: 500)
    #expect(framing.append(noise).isEmpty)
    #expect(framing.pendingBytes == 0, "leading noise was retained")

    #expect(framing.append(soi + Data([0x01])).isEmpty)
    #expect(framing.pendingBytes == 3, "a partial frame should be held intact")
}

@Test("an EOI with no SOI before it yields nothing")
func trailingEndMarkerAlone() {
    var framing = JPEGFraming()
    #expect(framing.append(eoi).isEmpty)
    #expect(framing.pendingBytes == 0)
}

@Test("a buffer that never completes a frame is capped rather than growing")
func runawayBufferIsCapped() {
    // Without the cap, a peer sending an SOI and then megabytes of nothing
    // holds all of it forever.
    var framing = JPEGFraming(maxBuffer: 1024)
    _ = framing.append(soi + Data(repeating: 0x7F, count: 4096))
    #expect(framing.pendingBytes == 0, "the cap did not discard the runaway buffer")
}

@Test("parsing resumes cleanly after the cap discards a runaway buffer")
func recoversAfterCap() {
    var framing = JPEGFraming(maxBuffer: 1024)
    _ = framing.append(soi + Data(repeating: 0x7F, count: 4096))
    let frame = jpeg([0x09])
    #expect(framing.append(frame) == [frame])
}
