import CoreMedia
import Foundation
import Testing
@testable import QuernMedia

// The SPS and PPS quern-media sent for an iPhone 17 Pro simulator at 414x900,
// read off a live /frames stream: High profile, level 3.0, one reference
// frame, and no VUI -- the case #464 is about.
private let liveSPS = Data([0x27, 0x64, 0x00, 0x1E, 0xAC, 0x56, 0x81, 0xA0, 0x73, 0xEA, 0x74])
private let livePPS = Data([0x28, 0xEE, 0x3C, 0xB0])

/// One bit of `bytes`, MSB first; -1 past the end, so a short buffer fails
/// an expectation rather than crashing the test run (which hides the rest).
private func bit(_ bytes: [UInt8], _ p: Int) -> Int {
    p >= 0 && p < bytes.count * 8 ? Int(bytes[p >> 3] >> (7 - UInt8(p & 7))) & 1 : -1
}

/// The VUI fields this rewrite writes, read from bit `at` of an RBSP by a
/// reader written separately from the one under test.
private struct VUI: Equatable {
    var present = 0, flags: [Int] = [], restriction = 0, mvOverBoundaries = 0
    var bytesDenom = 0, bitsDenom = 0, mvH = 0, mvV = 0, reorder = 0, decBuffering = 0
}

private func readVUI(_ rbsp: [UInt8], at start: Int) -> VUI {
    var p = start
    func u() -> Int { defer { p += 1 }; return bit(rbsp, p) }
    func ue() -> Int {
        var z = 0
        while u() == 0 && z < 32 { z += 1 }
        var v = 0
        for _ in 0..<z { v = v << 1 | u() }
        return (1 << z) - 1 + v
    }
    var v = VUI()
    v.present = u()
    v.flags = (0..<8).map { _ in u() }
    v.restriction = u()
    v.mvOverBoundaries = u()
    v.bytesDenom = ue(); v.bitsDenom = ue(); v.mvH = ue(); v.mvV = ue()
    v.reorder = ue(); v.decBuffering = ue()
    return v
}

private func expectedVUI(decBuffering: Int) -> VUI {
    VUI(present: 1, flags: Array(repeating: 0, count: 8), restriction: 1, mvOverBoundaries: 1,
        bytesDenom: 0, bitsDenom: 0, mvH: 15, mvV: 15, reorder: 0, decBuffering: decBuffering)
}

/// Where the VUI flag of a VUI-less SPS sits: just before the stop bit,
/// which is the RBSP's last 1 bit.
private func vuiFlagPosition(_ rbsp: [UInt8]) -> Int {
    var p = rbsp.count * 8 - 1
    while p > 0 && bit(rbsp, p) == 0 { p -= 1 }
    return p - 1
}

private func dimensions(sps: Data, pps: Data) -> CMVideoDimensions? {
    var fd: CMFormatDescription?
    let status = sps.withUnsafeBytes { s in
        pps.withUnsafeBytes { q in
            let pointers = [s.bindMemory(to: UInt8.self).baseAddress!,
                            q.bindMemory(to: UInt8.self).baseAddress!]
            let sizes = [sps.count, pps.count]
            return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                allocator: nil, parameterSetCount: 2, parameterSetPointers: pointers,
                parameterSetSizes: sizes, nalUnitHeaderLength: 4, formatDescriptionOut: &fd)
        }
    }
    guard status == noErr, let fd else { return nil }
    return CMVideoFormatDescriptionGetDimensions(fd)
}

@Test("the live SPS gains a VUI saying there is no reordering, and nothing else changes")
func liveSPSGainsLowDelayVUI() throws {
    let out = LowDelaySPS.rewrite(liveSPS)
    #expect(out != liveSPS)
    let before = LowDelaySPS.unescape(Array(liveSPS))
    let after = LowDelaySPS.unescape(Array(out))
    let at = vuiFlagPosition(before)
    #expect(bit(before, at) == 0, "the original has no VUI")
    #expect((0..<at).allSatisfy { bit(before, $0) == bit(after, $0) },
            "every field before the VUI is kept bit for bit")
    // max_num_ref_frames is 1 in this stream.
    #expect(readVUI(after, at: at) == expectedVUI(decBuffering: 1))
}

@Test("CoreMedia accepts the rewritten SPS and reads the same picture size")
func coreMediaAcceptsIt() throws {
    let original = try #require(dimensions(sps: liveSPS, pps: livePPS))
    let rewritten = try #require(dimensions(sps: LowDelaySPS.rewrite(liveSPS), pps: livePPS),
                                 "CoreMedia refused the rewritten SPS")
    #expect(original.width == rewritten.width && original.height == rewritten.height)
    #expect(original.width > 0 && original.height > 0)
}

@Test("an SPS that already has a VUI is passed through: rewriting is idempotent")
func idempotent() {
    let once = LowDelaySPS.rewrite(liveSPS)
    #expect(LowDelaySPS.rewrite(once) == once)
}

@Test("anything that is not a whole, understood SPS is passed through unchanged")
func passesThroughWhatItDoesNotUnderstand() {
    #expect(LowDelaySPS.rewrite(livePPS) == livePPS, "not an SPS")
    #expect(LowDelaySPS.rewrite(Data()) == Data())
    for n in 1..<liveSPS.count {
        let cut = liveSPS.prefix(n)
        #expect(LowDelaySPS.rewrite(cut) == cut, "truncated to \(n) bytes")
    }
    // Bits after the VUI flag that are not just the stop bit: not understood.
    var junk = liveSPS
    junk.append(0xFF)
    #expect(LowDelaySPS.rewrite(junk) == junk)
}

/// A synthetic SPS taking the branches VideoToolbox's never does. Returns the
/// NAL and the bit where its VUI flag is written.
private func syntheticSPS(scaling: Bool, pocType: Int, frameMbsOnly: Bool, cropping: Bool,
                          maxRefFrames: Int, crop: [Int] = [0, 2, 0, 3], profile: Int = 100,
                          chroma: Int = 1, separatePlanes: Bool = false) -> (Data, Int) {
    var w = BitWriter()
    w.u(8, 0x67); w.u(8, profile); w.u(8, 0); w.u(8, 31)  // header, profile, constraints, 3.1
    w.ue(0)                                            // sps id
    // Only the high profiles carry chroma format, bit depth and scaling.
    let high = [100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135].contains(profile)
    if high {
        w.ue(chroma)
        if chroma == 3 { w.u(1, separatePlanes ? 1 : 0) }
        w.ue(0); w.ue(0); w.u(1, 0)                    // 8-bit, no bypass
        w.u(1, scaling ? 1 : 0)
    }
    if high && scaling {
        for i in 0..<(chroma != 3 ? 8 : 12) {
            let present = i % 3 == 0
            w.u(1, present ? 1 : 0)
            // Real deltas, then one that ends the list early (next == 0), so
            // both arms of the reader's loop run.
            // Lists 6 and 9 are 8x8 lists run to their full 64 entries, so a
            // reader that took every list as 4x4, or read 8 lists for 4:4:4,
            // would end in the wrong place.
            if present {
                writeScalingList(&w, size: i < 6 ? 16 : 64,
                                 deltas: i == 6 || i == 9 ? [1] : [3, -2, -9])
            }
        }
    }
    w.ue(0)                                            // log2_max_frame_num_minus4
    w.ue(pocType)
    if pocType == 0 { w.ue(2) }
    if pocType == 1 {
        w.u(1, 0); writeSe(&w, 2); writeSe(&w, -2)
        w.ue(2); writeSe(&w, 1); writeSe(&w, -1)      // two offset_for_ref_frame
    }
    w.ue(maxRefFrames)
    w.u(1, 0)                                          // gaps
    w.ue(19); w.ue(14)                                 // 320 x 240 (frame) in MBs
    w.u(1, frameMbsOnly ? 1 : 0)
    if !frameMbsOnly { w.u(1, 0) }
    w.u(1, 1)                                          // direct_8x8
    w.u(1, cropping ? 1 : 0)
    if cropping { for c in crop { w.ue(c) } }
    let at = w.bitCount
    w.u(1, 0)                                          // no VUI
    w.u(1, 1)                                          // stop bit
    return (Data(LowDelaySPS.escape(w.bytes)), at)
}

private func writeSe(_ w: inout BitWriter, _ v: Int) { w.ue(v > 0 ? 2 * v - 1 : -2 * v) }

/// A scaling list as H.264 7.3.2.1.1.1 reads one: a delta while next != 0.
private func writeScalingList(_ w: inout BitWriter, size: Int, deltas: [Int]) {
    var last = 8, next = 8, k = 0
    for _ in 0..<size {
        if next != 0 {
            let d = k < deltas.count ? deltas[k] : 0
            k += 1
            writeSe(&w, d)
            next = (last + d + 256) % 256
        }
        last = next == 0 ? last : next
    }
}

@Test("the branches VideoToolbox never takes parse to the right VUI position",
      arguments: [
        (true, 0, true, false, 1),
        (false, 1, true, false, 2),
        (false, 2, false, true, 4),
        (true, 1, false, true, 0),
      ])
func syntheticBranches(scaling: Bool, pocType: Int, frameMbsOnly: Bool, cropping: Bool,
                       refs: Int) throws {
    let (sps, at) = syntheticSPS(scaling: scaling, pocType: pocType, frameMbsOnly: frameMbsOnly,
                                 cropping: cropping, maxRefFrames: refs)
    let before = LowDelaySPS.unescape(Array(sps))
    #expect(vuiFlagPosition(before) == at, "test builder: VUI position")
    let out = LowDelaySPS.rewrite(sps)
    #expect(out != sps, "rewritten")
    let after = LowDelaySPS.unescape(Array(out))
    #expect((0..<at).allSatisfy { bit(before, $0) == bit(after, $0) })
    // max_dec_frame_buffering is at least 1 and at least max_num_ref_frames.
    #expect(readVUI(after, at: at) == expectedVUI(decBuffering: max(refs, 1)))
}

@Test("Baseline, Main and 4:4:4 SPSes are rewritten too",
      arguments: [
        (66, 1, false, false),    // Baseline: no chroma branch at all
        (77, 1, false, false),    // Main: the same
        (244, 3, true, true),     // High 4:4:4, separate planes, 12 scaling lists
        (244, 3, false, true),    // High 4:4:4, joint planes
      ])
func otherProfiles(profile: Int, chroma: Int, separatePlanes: Bool, scaling: Bool) throws {
    let (sps, at) = syntheticSPS(scaling: scaling, pocType: 0, frameMbsOnly: true,
                                 cropping: false, maxRefFrames: 1, profile: profile,
                                 chroma: chroma, separatePlanes: separatePlanes)
    let before = LowDelaySPS.unescape(Array(sps))
    #expect(vuiFlagPosition(before) == at, "test builder: VUI position")
    let out = LowDelaySPS.rewrite(sps)
    #expect(out != sps, "rewritten, not passed through")
    let after = LowDelaySPS.unescape(Array(out))
    #expect((0..<at).allSatisfy { bit(before, $0) == bit(after, $0) })
    #expect(readVUI(after, at: at) == expectedVUI(decBuffering: 1))
}

// Real VideoToolbox output for Main profile at 320x240 (the encoder asks for
// High, but a regression that stopped rewriting Main would bring #464 back
// for anyone whose encoder settled on it).
private let mainSPS = Data([0x27, 0x4D, 0x00, 0x0D, 0xAB, 0x40, 0xA0, 0xFC, 0x80])

@Test("a real Main-profile SPS is rewritten, and CoreMedia accepts it")
func realMainProfile() throws {
    let out = LowDelaySPS.rewrite(mainSPS)
    #expect(out != mainSPS)
    let before = LowDelaySPS.unescape(Array(mainSPS))
    let after = LowDelaySPS.unescape(Array(out))
    let at = vuiFlagPosition(before)
    #expect((0..<at).allSatisfy { bit(before, $0) == bit(after, $0) })
    #expect(readVUI(after, at: at) == expectedVUI(decBuffering: 1))
    let original = try #require(dimensions(sps: mainSPS, pps: livePPS))
    let rewritten = try #require(dimensions(sps: out, pps: livePPS))
    #expect(original.width == rewritten.width && original.height == rewritten.height)
}

@Test("the encoder never reorders frames, which the rewritten SPS promises")
func encoderDoesNotReorder() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    _ = try #require(encoder.encode(CapturedFrame(
        surface: surface, time: CMTime(seconds: 0, preferredTimescale: 600),
        timeAccuracy: .reported)))
    #expect(encoder.sessionAllowsFrameReordering() == false)
}

@Test("emulation prevention is removed on the way in and restored on the way out")
func emulationPrevention() {
    #expect(LowDelaySPS.escape([0, 0, 1, 0, 0, 0, 0, 0, 4]) == [0, 0, 3, 1, 0, 0, 3, 0, 0, 3, 0, 4])
    #expect(LowDelaySPS.unescape([0, 0, 3, 1, 0, 0, 3, 0, 0, 3, 0, 4]) == [0, 0, 1, 0, 0, 0, 0, 0, 4])
    #expect(LowDelaySPS.escape([0, 0, 3]) == [0, 0, 3, 3], "a 0x03 is escaped too")
    #expect(LowDelaySPS.escape([0, 0, 4]) == [0, 0, 4], "only bytes <= 3 are escaped")
}

/// Whether a NAL payload holds a sequence emulation prevention forbids:
/// two zeros then 0, 1 or 2. Two zeros then 3 is the escape itself.
private func hasUnescapedRun(_ d: [UInt8]) -> Bool {
    var zeros = 0
    for b in d {
        if zeros >= 2 && b <= 2 { return true }
        zeros = zeros >= 2 && b == 3 ? 0 : (b == 0 ? zeros + 1 : 0)
    }
    return false
}

@Test("an SPS that needs emulation prevention is read through it and written with it")
func emulationPreventionThroughARewrite() throws {
    // Cropping offsets with long Exp-Golomb prefixes put runs of zero bytes
    // in the RBSP, so the NAL carries 0x03 bytes going in, and the rewrite
    // must strip them to parse and restore them coming out.
    let (sps, at) = syntheticSPS(scaling: false, pocType: 0, frameMbsOnly: true, cropping: true,
                                 maxRefFrames: 1, crop: [1 << 26, 1 << 27, 1 << 26, 1 << 28])
    let before = LowDelaySPS.unescape(Array(sps))
    try #require(sps.count > before.count, "fixture: the NAL must carry emulation prevention")
    let out = LowDelaySPS.rewrite(sps)
    #expect(out != sps, "rewritten")
    #expect(!hasUnescapedRun(Array(out)), "the output is escaped")
    let after = LowDelaySPS.unescape(Array(out))
    #expect((0..<at).allSatisfy { bit(before, $0) == bit(after, $0) })
    #expect(readVUI(after, at: at) == expectedVUI(decBuffering: 1))
}

@Test("the live stream carries the rewritten SPS; the recording's format description does not")
func encoderStreamsTheRewrittenSPS() throws {
    let surface = try #require(TestSurface.make(width: 320, height: 240))
    let encoder = H264Encoder(maxDimension: 0, bitrate: 800_000, expectedFPS: 30)
    defer { encoder.invalidate() }
    let out = try #require(encoder.encode(CapturedFrame(
        surface: surface, time: CMTime(seconds: 0, preferredTimescale: 600),
        timeAccuracy: .reported)))
    let fd = try #require(CMSampleBufferGetFormatDescription(out.frame.sample))
    var pointer: UnsafePointer<UInt8>?
    var size = 0
    #expect(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
        fd, parameterSetIndex: 0, parameterSetPointerOut: &pointer, parameterSetSizeOut: &size,
        parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil) == noErr)
    let encoderSPS = Data(bytes: try #require(pointer), count: size)
    // The first NAL of the keyframe's Annex-B is the SPS.
    let body = out.annexB.dropFirst(AnnexB.startCode.count)
    let end = body.range(of: AnnexB.startCode)?.lowerBound ?? body.endIndex
    let streamed = Data(body[body.startIndex..<end])
    #expect(streamed == LowDelaySPS.rewrite(encoderSPS))
    #expect(streamed != encoderSPS, "VideoToolbox's SPS has no VUI, so the stream's must differ")
}
