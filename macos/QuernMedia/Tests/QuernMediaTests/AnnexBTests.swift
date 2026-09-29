import Foundation
import Testing
@testable import QuernMedia

/// Builds an AVCC buffer from raw NAL payloads.
private func avcc(_ payloads: [[UInt8]]) -> Data {
    var d = Data()
    for p in payloads {
        let n = UInt32(p.count)
        d.append(contentsOf: [
            UInt8((n >> 24) & 0xFF), UInt8((n >> 16) & 0xFF),
            UInt8((n >> 8) & 0xFF), UInt8(n & 0xFF),
        ])
        d.append(contentsOf: p)
    }
    return d
}

private func splitAnnexB(_ d: Data) -> [[UInt8]] {
    var out: [[UInt8]] = []
    var i = d.startIndex
    while let r = d[i...].range(of: AnnexB.startCode) {
        let next = d[r.upperBound...].range(of: AnnexB.startCode)?.lowerBound ?? d.endIndex
        out.append(Array(d[r.upperBound..<next]))
        i = next
    }
    return out
}

@Test("a single NAL round-trips")
func singleNAL() {
    let payload: [UInt8] = [0x65, 0xAA, 0xBB, 0xCC]
    let out = AnnexB.fromAVCC(avcc([payload]))
    #expect(out != nil)
    #expect(splitAnnexB(out!) == [payload])
}

@Test("odd payload lengths do not trap or corrupt the walk")
func oddLengthsDoNotMisalign() {
    // The regression. Every NAL advances the cursor by its payload size, so
    // after a 1-, 3- or 5-byte NAL the next length field is unaligned. Loading
    // it as a UInt32 traps; the spike crashed on its first real frame here.
    let payloads: [[UInt8]] = [
        [0x67, 0x01],                    // 2 bytes  -> next length at offset 6
        [0x68],                          // 1 byte   -> offset 11
        [0x65, 0xDE, 0xAD, 0xBE, 0xEF],  // 5 bytes  -> offset 20
        [0x41, 0x11, 0x22],              // 3 bytes
    ]
    let out = AnnexB.fromAVCC(avcc(payloads))
    #expect(out != nil)
    #expect(splitAnnexB(out!) == payloads)
}

@Test("a large NAL is not truncated")
func largeNAL() {
    let payload = [UInt8](repeating: 0x5A, count: 70_000)
    let out = AnnexB.fromAVCC(avcc([[0x65] + payload]))
    #expect(out != nil)
    #expect(splitAnnexB(out!).first?.count == payload.count + 1)
}

@Test("malformed input is rejected rather than partially converted", arguments: [
    Data([0x00, 0x00, 0x04]),                          // truncated length field
    Data([0x00, 0x00, 0x00, 0x08, 0x65, 0x01]),        // length runs past the end
    Data([0x00, 0x00, 0x00, 0x00]),                    // zero-length NAL
])
func rejectsMalformed(input: Data) {
    // Emitting a partial stream would surface as a decoder error far from
    // the cause, so this fails loudly instead.
    #expect(AnnexB.fromAVCC(input) == nil)
}

@Test("empty input yields empty output")
func emptyIsEmpty() {
    #expect(AnnexB.fromAVCC(Data()) == Data())
}

@Test("NAL type is read from the first unit")
func readsNALType() {
    #expect(AnnexB.firstNALType(AnnexB.fromAVCC(avcc([[0x65, 0x00]]))!) == 5)  // IDR
    #expect(AnnexB.firstNALType(AnnexB.fromAVCC(avcc([[0x67, 0x00]]))!) == 7)  // SPS
    #expect(AnnexB.firstNALType(Data()) == nil)
}
