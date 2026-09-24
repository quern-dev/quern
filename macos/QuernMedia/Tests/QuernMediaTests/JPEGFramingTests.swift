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

    // A lone trailing 0xFF is kept, not dropped: it may be the first half of
    // an SOI split across chunks. Asserting 0 here is what locked in a bug
    // that lost a whole frame per unlucky split.
    #expect(framing.append(Data([0xFF])).isEmpty)
    #expect(framing.pendingBytes == 1, "a trailing marker byte was discarded")

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


@Test("an SOI split across the chunk boundary does not lose the frame")
func startMarkerSplitAcrossChunks() {
    // The unlucky split: a chunk ends between the FF and the D8. Dropping the
    // stranded FF resynced at the *following* frame, so one frame vanished
    // with nothing to indicate it — the parser's own failure mode, quiet.
    var framing = JPEGFraming()
    let first = jpeg([0x11])
    let second = jpeg([0x22])
    let stream = first + second

    let split = first.count + 1   // mid-SOI of the second frame
    let a = framing.append(stream[0..<split])
    let b = framing.append(stream[split...])

    #expect(a == [first])
    #expect(b == [second], "the frame after a split start marker was lost")
}

@Test("a cap smaller than a frame is raised rather than starving every frame")
func tinyCapIsClamped() {
    var framing = JPEGFraming(maxBuffer: 0)
    let frame = jpeg([0x01, 0x02, 0x03])
    #expect(framing.append(frame[0..<3]).isEmpty)
    #expect(framing.append(frame[3...]) == [frame],
            "a zero cap discarded a frame spanning two chunks")
}

@Test("garbage before a partial frame is dropped while the frame is kept")
func leadingGarbageBeforeAPartialFrame() {
    // The trim exists so a stream that opens with multipart headers does not
    // carry them in the buffer forever. Deleting it left all the other tests
    // green, because none of them sends garbage and an incomplete frame in
    // the same chunk.
    var framing = JPEGFraming()
    let header = Data("--quernframe\r\nContent-Type: image/jpeg\r\n\r\n".utf8)
    let partial = soi + Data([0x01, 0x02])

    #expect(framing.append(header + partial).isEmpty)
    #expect(framing.pendingBytes == partial.count,
            "the header was retained alongside the partial frame")

    let rest = Data([0x03]) + eoi
    #expect(framing.append(rest) == [partial + rest])
}

@Test("the cap counts its discards rather than dropping them silently")
func capIsCounted() {
    var framing = JPEGFraming(maxBuffer: 64)
    #expect(framing.timesCapped == 0)
    _ = framing.append(soi + Data(repeating: 0x7F, count: 4096))
    #expect(framing.timesCapped == 1, "a discard went unrecorded")
}
