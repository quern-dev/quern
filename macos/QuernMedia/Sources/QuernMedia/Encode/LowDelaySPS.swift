import Foundation

/// Tells a decoder the stream has no frame reordering, by giving the SPS a
/// VUI that says so (#464).
///
/// VideoToolbox's SPS carries no VUI at all. Without its
/// `bitstream_restriction`, a decoder must assume `max_num_reorder_frames` is
/// as large as the level allows -- several frames -- and may hold output back
/// until that many have followed. Safari's WebCodecs decoder does: on a still
/// screen, where nothing follows a keyframe, it showed no picture at all
/// (6 of 6 trials), and in motion it lags by those frames. The stream has no
/// B-frames -- real-time encoding with frame reordering off -- so
/// `max_num_reorder_frames = 0` is simply true of it.
///
/// Only the live stream's parameter sets are rewritten. A recording takes its
/// own from the sample buffer's format description and never sees this.
///
/// An SPS that already has a VUI, or that does not parse, is returned
/// unchanged: editing an existing VUI is more than this needs, and a stream
/// with a slow first picture is better than one with an SPS this broke.
public enum LowDelaySPS {
    /// `sps` -- one NAL unit, header byte first, no start code -- with a VUI
    /// declaring no reordering; or `sps` itself when it is not an SPS, already
    /// has a VUI, or cannot be parsed.
    public static func rewrite(_ sps: Data) -> Data {
        (try? rewritten(sps)) ?? sps
    }

    struct Malformed: Error {}

    static func rewritten(_ sps: Data) throws -> Data? {
        let rbsp = unescape(Array(sps))
        guard let header = rbsp.first, header & 0x1F == 7 else { return nil }
        var r = BitReader(rbsp)
        _ = try r.u(8)                                    // NAL header
        let profile = try r.u(8)
        _ = try r.u(8)                                    // constraint flags
        _ = try r.u(8)                                    // level_idc
        _ = try r.ue()                                    // seq_parameter_set_id
        if [100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135].contains(profile) {
            let chroma = try r.ue()
            if chroma == 3 { _ = try r.u(1) }             // separate_colour_plane_flag
            _ = try r.ue()                                // bit_depth_luma_minus8
            _ = try r.ue()                                // bit_depth_chroma_minus8
            _ = try r.u(1)                                // qpprime_y_zero_transform_bypass
            if try r.u(1) == 1 {                          // seq_scaling_matrix_present_flag
                for i in 0..<(chroma != 3 ? 8 : 12) {
                    if try r.u(1) == 1 { try skipScalingList(&r, size: i < 6 ? 16 : 64) }
                }
            }
        }
        _ = try r.ue()                                    // log2_max_frame_num_minus4
        let pocType = try r.ue()
        if pocType == 0 {
            _ = try r.ue()                                // log2_max_poc_lsb_minus4
        } else if pocType == 1 {
            _ = try r.u(1)                                // delta_pic_order_always_zero
            _ = try r.se()                                // offset_for_non_ref_pic
            _ = try r.se()                                // offset_for_top_to_bottom_field
            for _ in 0..<(try r.ue()) { _ = try r.se() }  // offset_for_ref_frame[]
        }
        let maxRefFrames = try r.ue()
        _ = try r.u(1)                                    // gaps_in_frame_num_allowed
        _ = try r.ue()                                    // pic_width_in_mbs_minus1
        _ = try r.ue()                                    // pic_height_in_map_units_minus1
        if try r.u(1) == 0 { _ = try r.u(1) }             // frame_mbs_only, mb_adaptive
        _ = try r.u(1)                                    // direct_8x8_inference
        if try r.u(1) == 1 {                              // frame_cropping_flag
            for _ in 0..<4 { _ = try r.ue() }
        }
        let vuiAt = r.position
        guard try r.u(1) == 0 else { return nil }         // a VUI already: leave it
        // What follows must be the RBSP trailing bits alone: a stop bit and
        // zeros. Anything else is an SPS this does not understand.
        guard try r.u(1) == 1, r.remainingAreZero else { throw Malformed() }

        var w = BitWriter()
        w.copy(rbsp, bits: vuiAt)
        w.u(1, 1)                                         // vui_parameters_present_flag
        for _ in 0..<8 { w.u(1, 0) }                      // aspect ratio .. pic_struct: absent
        w.u(1, 1)                                         // bitstream_restriction_flag
        w.u(1, 1)                                         // motion_vectors_over_pic_boundaries
        w.ue(0)                                           // max_bytes_per_pic_denom: no limit
        w.ue(0)                                           // max_bits_per_mb_denom: no limit
        w.ue(15)                                          // log2_max_mv_length_horizontal
        w.ue(15)                                          // log2_max_mv_length_vertical
        w.ue(0)                                           // max_num_reorder_frames
        w.ue(max(maxRefFrames, 1))                        // max_dec_frame_buffering
        w.u(1, 1)                                         // rbsp_stop_one_bit
        return Data(escape(w.bytes))
    }

    private static func skipScalingList(_ r: inout BitReader, size: Int) throws {
        var last = 8
        var next = 8
        for _ in 0..<size {
            if next != 0 { next = (last + (try r.se()) + 256) % 256 }
            last = next == 0 ? last : next
        }
    }

    /// NAL payload to RBSP: drops each emulation_prevention_three_byte.
    static func unescape(_ nal: [UInt8]) -> [UInt8] {
        var out: [UInt8] = []
        out.reserveCapacity(nal.count)
        var zeros = 0
        for b in nal {
            if zeros >= 2 && b == 0x03 { zeros = 0; continue }
            out.append(b)
            zeros = b == 0 ? zeros + 1 : 0
        }
        return out
    }

    /// RBSP to NAL payload: a 0x03 wherever two zeros precede a byte <= 3.
    static func escape(_ rbsp: [UInt8]) -> [UInt8] {
        var out: [UInt8] = []
        out.reserveCapacity(rbsp.count + 4)
        var zeros = 0
        for b in rbsp {
            if zeros >= 2 && b <= 0x03 { out.append(0x03); zeros = 0 }
            out.append(b)
            zeros = b == 0 ? zeros + 1 : 0
        }
        return out
    }
}

/// Reads u(n), ue(v) and se(v) as H.264 7.2 defines them, throwing past the end.
struct BitReader {
    private let bytes: [UInt8]
    private(set) var position = 0

    init(_ bytes: [UInt8]) { self.bytes = bytes }

    mutating func u(_ n: Int) throws -> Int {
        var v = 0
        for _ in 0..<n {
            guard position < bytes.count * 8 else { throw LowDelaySPS.Malformed() }
            let bit = (bytes[position >> 3] >> (7 - UInt8(position & 7))) & 1
            v = v << 1 | Int(bit)
            position += 1
        }
        return v
    }

    mutating func ue() throws -> Int {
        var zeros = 0
        while try u(1) == 0 {
            zeros += 1
            guard zeros < 32 else { throw LowDelaySPS.Malformed() }
        }
        return (1 << zeros) - 1 + (try u(zeros))
    }

    mutating func se() throws -> Int {
        let k = try ue()
        return k % 2 == 1 ? (k + 1) / 2 : -(k / 2)
    }

    var remainingAreZero: Bool {
        var p = position
        while p < bytes.count * 8 {
            if (bytes[p >> 3] >> (7 - UInt8(p & 7))) & 1 == 1 { return false }
            p += 1
        }
        return true
    }
}

/// Writes u(n) and ue(v), padding the last byte with zeros.
struct BitWriter {
    private(set) var bytes: [UInt8] = []
    private var used = 8                     // bits used in the last byte

    /// Bits written so far.
    var bitCount: Int { bytes.count * 8 - (8 - used) }

    mutating func u(_ n: Int, _ value: Int) {
        for i in stride(from: n - 1, through: 0, by: -1) {
            if used == 8 { bytes.append(0); used = 0 }
            if (value >> i) & 1 == 1 { bytes[bytes.count - 1] |= 0x80 >> UInt8(used) }
            used += 1
        }
    }

    mutating func ue(_ value: Int) {
        let v = value + 1
        let width = Int.bitWidth - v.leadingZeroBitCount
        u(width - 1, 0)
        u(width, v)
    }

    /// The first `bits` bits of `source`, as they are.
    mutating func copy(_ source: [UInt8], bits: Int) {
        for p in 0..<bits { u(1, Int((source[p >> 3] >> (7 - UInt8(p & 7))) & 1)) }
    }
}
