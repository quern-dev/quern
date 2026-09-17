import CoreMedia
import Foundation

/// Converts H.264 between the two framings that exist in practice.
///
/// VideoToolbox emits **AVCC**: each NAL unit prefixed with its length as a
/// 4-byte big-endian integer, with SPS/PPS carried out of band in the format
/// description. A raw elementary stream — what a decoder reads off a socket
/// or a pipe — wants **Annex-B**: NALs separated by `00 00 00 01` start
/// codes, with parameter sets inline.
public enum AnnexB {
    public static let startCode = Data([0x00, 0x00, 0x00, 0x01])

    /// Rewrites a length-prefixed AVCC buffer as Annex-B.
    ///
    /// The lengths are assembled a byte at a time rather than loaded as a
    /// `UInt32`, and that is not stylistic. Each NAL advances the cursor by an
    /// arbitrary payload size, so the next length field lands at an arbitrary
    /// offset — a typed load there traps on alignment. The spike crashed on
    /// its first real frame for exactly this reason.
    ///
    /// Returns nil if the buffer is malformed: a truncated length field, a
    /// zero length, or a payload running past the end. Silently emitting a
    /// partial stream would produce a decoder error far from the cause.
    public static func fromAVCC(_ avcc: Data) -> Data? {
        guard !avcc.isEmpty else { return Data() }
        var out = Data(capacity: avcc.count + 8)
        var i = avcc.startIndex

        while i < avcc.endIndex {
            guard avcc.distance(from: i, to: avcc.endIndex) >= 4 else { return nil }
            let length =
                Int(avcc[i]) << 24 | Int(avcc[i + 1]) << 16
                | Int(avcc[i + 2]) << 8 | Int(avcc[i + 3])
            i = avcc.index(i, offsetBy: 4)
            guard length > 0, avcc.distance(from: i, to: avcc.endIndex) >= length else {
                return nil
            }
            out.append(startCode)
            out.append(avcc[i..<avcc.index(i, offsetBy: length)])
            i = avcc.index(i, offsetBy: length)
        }
        return out
    }

    /// Extracts SPS/PPS from a format description as inline Annex-B NALs.
    ///
    /// Re-emitted ahead of every keyframe rather than once at stream start, so
    /// a viewer joining mid-stream can begin decoding at any keyframe instead
    /// of only at the beginning. Costs a few dozen bytes per IDR.
    public static func parameterSets(from fd: CMFormatDescription) -> Data? {
        var count = 0
        guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            fd, parameterSetIndex: 0, parameterSetPointerOut: nil,
            parameterSetSizeOut: nil, parameterSetCountOut: &count,
            nalUnitHeaderLengthOut: nil
        ) == noErr, count > 0 else { return nil }

        var out = Data()
        for index in 0..<count {
            var pointer: UnsafePointer<UInt8>?
            var size = 0
            guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                fd, parameterSetIndex: index, parameterSetPointerOut: &pointer,
                parameterSetSizeOut: &size, parameterSetCountOut: nil,
                nalUnitHeaderLengthOut: nil
            ) == noErr, let pointer else { continue }
            out.append(startCode)
            out.append(Data(bytes: pointer, count: size))
        }
        return out.isEmpty ? nil : out
    }

    /// NAL unit type of the first NAL in an Annex-B buffer, if any.
    /// 5 is an IDR slice, 7 SPS, 8 PPS.
    public static func firstNALType(_ annexB: Data) -> UInt8? {
        guard let range = annexB.range(of: startCode),
              range.upperBound < annexB.endIndex else { return nil }
        return annexB[range.upperBound] & 0x1F
    }
}
