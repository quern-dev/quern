// Fragmented MP4 from H.264 access units, for Media Source Extensions (#449).
//
// The viewer page receives quern-media's /frames: one Annex B access unit per
// part. Where the browser has WebCodecs it decodes those directly. Where it
// does not -- Chrome on a plain-HTTP LAN origin, measured in the #449 spike --
// MSE can play H.264 only inside fragmented MP4, and this does that wrapping.
//
// Written here rather than vendoring a muxer library, by the maintainer's
// call: the job is narrow -- one H.264 video track, no audio, input already
// split into access units -- and a few hundred lines we own beat a dependency.
// The viewer can still load another muxer for an A/B comparison; see the page.
//
// Usage:
//   const mux = new Fmp4Muxer();
//   for each access unit:
//     const out = mux.push(annexB);       // null until the first keyframe
//     if (out.init) sourceBuffer.appendBuffer(out.init);   // codec: out.codec
//     sourceBuffer.appendBuffer(out.fragment);
//
// Timing is nominal: every sample lasts `frameDuration`. Real gaps between
// frames froze the last frames of a change after a still screen in the
// spike, so the stream's media time advances per frame, not per second, and
// the page keeps playback near the live edge itself.

const NAL_SLICE = 1;
const NAL_IDR = 5;
const NAL_SEI = 6;
const NAL_SPS = 7;
const NAL_PPS = 8;
const NAL_AUD = 9;

/** NAL units of one Annex B access unit, start codes removed. */
export function splitNalUnits(au) {
  const units = [];
  let start = -1;
  let i = 0;
  while (i + 2 < au.length) {
    if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1) {
      if (start >= 0) units.push(trimTrailingZeros(au.subarray(start, i)));
      i += 3;
      start = i;
    } else {
      i += 1;
    }
  }
  if (start >= 0 && start < au.length) units.push(au.subarray(start));
  return units.filter((u) => u.length > 0);
}

// A 4-byte start code is a zero then a 3-byte one: the zero belongs to the
// start code, not to the end of the previous unit.
function trimTrailingZeros(unit) {
  let end = unit.length;
  while (end > 0 && unit[end - 1] === 0) end -= 1;
  return unit.subarray(0, end);
}

/** The NAL unit payload with emulation-prevention bytes (00 00 03) removed. */
export function unescapeRbsp(unit) {
  const out = new Uint8Array(unit.length);
  let n = 0;
  let zeros = 0;
  for (let i = 0; i < unit.length; i += 1) {
    const b = unit[i];
    if (zeros >= 2 && b === 3) {
      zeros = 0;
      continue;
    }
    out[n++] = b;
    zeros = b === 0 ? zeros + 1 : 0;
  }
  return out.subarray(0, n);
}

class BitReader {
  constructor(bytes) {
    this.bytes = bytes;
    this.pos = 0;
  }

  bit() {
    if (this.pos >= this.bytes.length * 8) throw new Error("SPS ended early");
    const v = (this.bytes[this.pos >> 3] >> (7 - (this.pos & 7))) & 1;
    this.pos += 1;
    return v;
  }

  bits(n) {
    let v = 0;
    for (let i = 0; i < n; i += 1) v = (v * 2) + this.bit();
    return v;
  }

  ue() {
    let zeros = 0;
    while (this.bit() === 0) {
      zeros += 1;
      if (zeros > 31) throw new Error("malformed Exp-Golomb code in SPS");
    }
    return (2 ** zeros) - 1 + this.bits(zeros);
  }

  se() {
    const k = this.ue();
    return k % 2 === 1 ? (k + 1) / 2 : -(k / 2);
  }
}

const HIGH_PROFILES = new Set([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135]);

/** What the init segment needs from a sequence parameter set (H.264 7.3.2.1.1). */
export function parseSps(spsUnit) {
  const r = new BitReader(unescapeRbsp(spsUnit));
  r.bits(8); // forbidden_zero_bit, nal_ref_idc, nal_unit_type
  const profile = r.bits(8);
  const constraints = r.bits(8);
  const level = r.bits(8);
  r.ue(); // seq_parameter_set_id
  let chromaFormat = 1;
  let separateColourPlanes = 0;
  if (HIGH_PROFILES.has(profile)) {
    chromaFormat = r.ue();
    if (chromaFormat === 3) separateColourPlanes = r.bit();
    r.ue(); // bit_depth_luma_minus8
    r.ue(); // bit_depth_chroma_minus8
    r.bit(); // qpprime_y_zero_transform_bypass_flag
    if (r.bit()) { // seq_scaling_matrix_present_flag
      const lists = chromaFormat === 3 ? 12 : 8;
      for (let i = 0; i < lists; i += 1) {
        if (r.bit()) skipScalingList(r, i < 6 ? 16 : 64);
      }
    }
  }
  r.ue(); // log2_max_frame_num_minus4
  const pocType = r.ue();
  if (pocType === 0) {
    r.ue(); // log2_max_pic_order_cnt_lsb_minus4
  } else if (pocType === 1) {
    r.bit(); // delta_pic_order_always_zero_flag
    r.se(); // offset_for_non_ref_pic
    r.se(); // offset_for_top_to_bottom_field
    const cycle = r.ue();
    for (let i = 0; i < cycle; i += 1) r.se();
  }
  r.ue(); // max_num_ref_frames
  r.bit(); // gaps_in_frame_num_value_allowed_flag
  const widthMbs = r.ue() + 1;
  const heightMapUnits = r.ue() + 1;
  const frameMbsOnly = r.bit();
  if (!frameMbsOnly) r.bit(); // mb_adaptive_frame_field_flag
  r.bit(); // direct_8x8_inference_flag
  let crop = { left: 0, right: 0, top: 0, bottom: 0 };
  if (r.bit()) {
    crop = { left: r.ue(), right: r.ue(), top: r.ue(), bottom: r.ue() };
  }
  // Crop units depend on the chroma format (Table 6-1, equations 7-19..7-22).
  const arrayType = separateColourPlanes ? 0 : chromaFormat;
  const cropX = arrayType === 0 ? 1 : (arrayType === 3 ? 1 : 2);
  const cropY = (arrayType === 0 ? 1 : (arrayType === 1 ? 2 : 1)) * (2 - frameMbsOnly);
  const width = (widthMbs * 16) - ((crop.left + crop.right) * cropX);
  const height = ((2 - frameMbsOnly) * heightMapUnits * 16) - ((crop.top + crop.bottom) * cropY);
  return { profile, constraints, level, width, height };
}

function skipScalingList(r, size) {
  let last = 8;
  let next = 8;
  for (let j = 0; j < size; j += 1) {
    if (next !== 0) next = (last + r.se() + 256) % 256;
    last = next === 0 ? last : next;
  }
}

/** The RFC 6381 codec string MediaSource wants, e.g. avc1.64001f. */
export function codecString(sps) {
  const hex = (b) => b.toString(16).padStart(2, "0");
  return `avc1.${hex(sps.profile)}${hex(sps.constraints)}${hex(sps.level)}`;
}

// ---- boxes ---------------------------------------------------------------

function u32(n) {
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
}

function u16(n) {
  return [(n >>> 8) & 255, n & 255];
}

function u64(n) {
  const hi = Math.floor(n / 2 ** 32);
  return [...u32(hi), ...u32(n >>> 0)];
}

function concat(parts) {
  const len = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(len);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

function box(type, ...payload) {
  const body = concat(payload.map((p) => (p instanceof Uint8Array ? p : Uint8Array.from(p))));
  const out = new Uint8Array(8 + body.length);
  out.set(u32(out.length), 0);
  for (let i = 0; i < 4; i += 1) out[4 + i] = type.charCodeAt(i);
  out.set(body, 8);
  return out;
}

function fullBox(type, version, flags, ...payload) {
  return box(type, [version, (flags >>> 16) & 255, (flags >>> 8) & 255, flags & 255], ...payload);
}

const MATRIX = [
  ...u32(0x00010000), ...u32(0), ...u32(0),
  ...u32(0), ...u32(0x00010000), ...u32(0),
  ...u32(0), ...u32(0), ...u32(0x40000000),
];

function avcC(spsUnit, ppsUnit) {
  return box("avcC",
    [1, spsUnit[1], spsUnit[2], spsUnit[3], 0xff, 0xe1, ...u16(spsUnit.length)],
    spsUnit,
    [1, ...u16(ppsUnit.length)],
    ppsUnit);
}

/** ftyp + moov for one H.264 track. */
export function initSegment({ spsUnit, ppsUnit, width, height, timescale, trackId = 1 }) {
  const ftyp = box("ftyp", [..."iso5".split("").map((c) => c.charCodeAt(0)), ...u32(512),
    ...["iso5", "iso6", "avc1", "mp41"].flatMap((b) => b.split("").map((c) => c.charCodeAt(0)))]);
  const mvhd = fullBox("mvhd", 0, 0, u32(0), u32(0), u32(timescale), u32(0),
    u32(0x00010000), u16(0x0100), new Uint8Array(10), MATRIX, new Uint8Array(24), u32(trackId + 1));
  const tkhd = fullBox("tkhd", 0, 3, u32(0), u32(0), u32(trackId), u32(0), u32(0),
    new Uint8Array(8), u16(0), u16(0), u16(0), u16(0), MATRIX,
    u32(width * 0x10000), u32(height * 0x10000));
  const mdhd = fullBox("mdhd", 0, 0, u32(0), u32(0), u32(timescale), u32(0), u16(0x55c4), u16(0));
  const hdlr = fullBox("hdlr", 0, 0, u32(0), [..."vide".split("").map((c) => c.charCodeAt(0))],
    new Uint8Array(12), [..."quern".split("").map((c) => c.charCodeAt(0)), 0]);
  const vmhd = fullBox("vmhd", 0, 1, new Uint8Array(8));
  const dinf = box("dinf", fullBox("dref", 0, 0, u32(1), fullBox("url ", 0, 1)));
  const avc1 = box("avc1", new Uint8Array(6), u16(1), new Uint8Array(16),
    u16(width), u16(height), u32(0x00480000), u32(0x00480000), u32(0), u16(1),
    new Uint8Array(32), u16(0x0018), u16(0xffff), avcC(spsUnit, ppsUnit));
  const stbl = box("stbl",
    fullBox("stsd", 0, 0, u32(1), avc1),
    fullBox("stts", 0, 0, u32(0)),
    fullBox("stsc", 0, 0, u32(0)),
    fullBox("stsz", 0, 0, u32(0), u32(0)),
    fullBox("stco", 0, 0, u32(0)));
  const minf = box("minf", vmhd, dinf, stbl);
  const mdia = box("mdia", mdhd, hdlr, minf);
  const trak = box("trak", tkhd, mdia);
  const mvex = box("mvex", fullBox("trex", 0, 0, u32(trackId), u32(1), u32(0), u32(0), u32(0)));
  return concat([ftyp, box("moov", mvhd, trak, mvex)]);
}

// Sample flags (ISO/IEC 14496-12 8.8.3.1): sample_depends_on = 2 and not a
// non-sync sample for a keyframe; depends_on = 1 and is_non_sync_sample for
// the rest.
const SYNC_FLAGS = 0x02000000;
const DELTA_FLAGS = 0x01010000;

/** moof + mdat holding one sample. */
export function mediaSegment({ sequence, decodeTime, duration, sample, keyframe, trackId = 1 }) {
  // tfhd: default-base-is-moof, so trun's data offset counts from the moof.
  const tfhd = fullBox("tfhd", 0, 0x020000, u32(trackId));
  const tfdt = fullBox("tfdt", 1, 0, u64(decodeTime));
  // trun with data-offset, sample-duration, sample-size and sample-flags.
  const trunFlags = 0x000001 | 0x000100 | 0x000200 | 0x000400;
  const trunSize = 12 + 4 + 4 + 12;      // header, count, offset, one sample
  const traf = box("traf", tfhd, tfdt,
    fullBox("trun", 0, trunFlags, u32(1), u32(0), u32(duration), u32(sample.length),
      u32(keyframe ? SYNC_FLAGS : DELTA_FLAGS)));
  const moofSize = 8 + 16 + traf.length;  // moof header + mfhd + traf
  const moof = box("moof", fullBox("mfhd", 0, 0, u32(sequence)), traf);
  // Patch trun's data offset: from the moof's first byte to the mdat payload.
  const offsetAt = moof.length - traf.length + (traf.length - trunSize) + 12 + 4;
  moof.set(u32(moofSize + 8), offsetAt);
  return concat([moof, box("mdat", sample)]);
}

/** Annex B access units in, fragmented MP4 out. */
export class Fmp4Muxer {
  constructor({ timescale = 90000, frameDuration = 3000 } = {}) {
    this.timescale = timescale;
    this.frameDuration = frameDuration;
    this.sequence = 0;
    this.decodeTime = 0;
    this.config = null;      // { spsKey, ppsKey } of the init segment in force
    this.codec = null;
  }

  /**
   * One access unit. Returns null until a keyframe carrying SPS and PPS has
   * arrived -- MSE cannot start without them -- then
   * `{ fragment, keyframe, init?, codec? }`, with `init` and `codec` present
   * on the first fragment and whenever the parameter sets change (a rotation
   * changes the dimensions, which needs a new init segment).
   */
  push(accessUnit) {
    const units = splitNalUnits(accessUnit);
    const sps = units.find((u) => (u[0] & 0x1f) === NAL_SPS);
    const pps = units.find((u) => (u[0] & 0x1f) === NAL_PPS);
    const keyframe = units.some((u) => (u[0] & 0x1f) === NAL_IDR);
    let init;
    if (sps && pps) {
      const key = `${hexOf(sps)}/${hexOf(pps)}`;
      if (!this.config || this.config.key !== key) {
        const info = parseSps(sps);
        init = initSegment({ spsUnit: sps, ppsUnit: pps, width: info.width,
          height: info.height, timescale: this.timescale });
        this.codec = codecString(info);
        this.config = { key, width: info.width, height: info.height };
      }
    }
    if (!this.config) return null;
    // Parameter sets live in avcC; SEI and delimiters are not needed to
    // decode. What remains is length-prefixed, as an avc1 sample expects.
    const payload = units.filter((u) => {
      const t = u[0] & 0x1f;
      return t !== NAL_SPS && t !== NAL_PPS && t !== NAL_AUD && t !== NAL_SEI;
    });
    if (payload.length === 0) return null;
    const sample = concat(payload.flatMap((u) => [Uint8Array.from(u32(u.length)), u]));
    this.sequence += 1;
    const fragment = mediaSegment({ sequence: this.sequence, decodeTime: this.decodeTime,
      duration: this.frameDuration, sample, keyframe });
    this.decodeTime += this.frameDuration;
    const out = { fragment, keyframe };
    if (init) {
      out.init = init;
      out.codec = this.codec;
    }
    return out;
  }
}

function hexOf(bytes) {
  let s = "";
  for (const b of bytes) s += b.toString(16).padStart(2, "0");
  return s;
}

export const NAL = { SLICE: NAL_SLICE, IDR: NAL_IDR, SEI: NAL_SEI, SPS: NAL_SPS, PPS: NAL_PPS, AUD: NAL_AUD };
