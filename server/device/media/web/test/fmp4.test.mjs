// node --test server/device/media/web/test/
// Box-level checks of the fMP4 muxer (#449) against a real quern-media stream:
// eight access units captured from an iPhone 17 Pro simulator at recording
// settings (H.264, 900px, two keyframes, six deltas).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  Fmp4Muxer, NAL, codecString, mediaSegment, parseSps, splitNalUnits, unescapeRbsp,
} from "../fmp4.js";

function fixture() {
  const buf = readFileSync(new URL("./simulator-h264.bin", import.meta.url));
  const aus = [];
  for (let i = 0; i < buf.length;) {
    const len = buf.readUInt32BE(i);
    aus.push(new Uint8Array(buf.subarray(i + 4, i + 4 + len)));
    i += 4 + len;
  }
  return aus;
}

/** Boxes in `bytes`, as [{type, start, size, body}], recursing into containers. */
function boxes(bytes, start = 0, end = bytes.length) {
  const out = [];
  const containers = new Set(["moov", "trak", "mdia", "minf", "stbl", "mvex", "moof", "traf", "dinf"]);
  for (let i = start; i + 8 <= end;) {
    const size = ((bytes[i] << 24) >>> 0) + (bytes[i + 1] << 16) + (bytes[i + 2] << 8) + bytes[i + 3];
    const type = String.fromCharCode(...bytes.subarray(i + 4, i + 8));
    assert.ok(size >= 8 && i + size <= end, `box ${type} at ${i} overruns: ${size}`);
    out.push({ type, start: i, size, body: bytes.subarray(i + 8, i + size) });
    if (containers.has(type)) out.push(...boxes(bytes, i + 8, i + size));
    i += size;
  }
  return out;
}

const find = (list, type) => list.find((b) => b.type === type);
const u32at = (b, o) => ((b[o] << 24) >>> 0) + (b[o + 1] << 16) + (b[o + 2] << 8) + b[o + 3];
const types = (au) => splitNalUnits(au).map((u) => u[0] & 0x1f);

test("NAL units split on 3- and 4-byte start codes, without the start code's zero", () => {
  const au = Uint8Array.from([0, 0, 0, 1, 0x67, 1, 2, 0, 0, 1, 0x68, 3, 0, 0, 0, 1, 0x65, 9]);
  assert.deepEqual(splitNalUnits(au).map((u) => [...u]), [[0x67, 1, 2], [0x68, 3], [0x65, 9]]);
});

test("the fixture is what it claims", () => {
  assert.deepEqual(fixture().map(types), [
    [NAL.SPS, NAL.PPS, NAL.SEI, NAL.IDR], [NAL.SPS, NAL.PPS, NAL.SEI, NAL.IDR],
    [1], [1], [1], [1], [1], [1],
  ]);
});

test("emulation-prevention bytes are removed, and only those", () => {
  assert.deepEqual([...unescapeRbsp(Uint8Array.from([0, 0, 3, 1, 0, 0, 3, 0, 3]))],
    [0, 0, 1, 0, 0, 0, 3]);
});

test("the SPS of a real stream gives its dimensions, profile and level", () => {
  const sps = splitNalUnits(fixture()[0]).find((u) => (u[0] & 0x1f) === NAL.SPS);
  const info = parseSps(sps);
  // 900 is not a multiple of 16, so this exercises frame cropping too.
  assert.deepEqual([info.width, info.height, info.profile, info.level], [414, 900, 100, 30]);
  assert.equal(codecString(info), "avc1.64001e");
});

test("nothing comes out before a keyframe with parameter sets", () => {
  const mux = new Fmp4Muxer();
  assert.equal(mux.push(fixture()[3]), null, "a delta frame first cannot start MSE");
  assert.ok(mux.push(fixture()[0]).init);
});

test("one init segment, carrying the parameter sets, until they change", () => {
  const aus = fixture();
  const mux = new Fmp4Muxer();
  const outs = aus.map((au) => mux.push(au));
  assert.equal(outs.filter((o) => o.init).length, 1, "a repeated identical SPS must not re-init");
  const init = boxes(outs[0].init);
  assert.deepEqual(init.filter((b) => b.start === 0 || b.type === "moov").map((b) => b.type),
    ["ftyp", "moov"]);
  for (const t of ["mvhd", "tkhd", "mdhd", "hdlr", "stsd", "trex"]) assert.ok(find(init, t), t);
  const tkhd = find(init, "tkhd").body;
  assert.deepEqual([u32at(tkhd, tkhd.length - 8) / 65536, u32at(tkhd, tkhd.length - 4) / 65536],
    [414, 900]);
  const sps = splitNalUnits(aus[0]).find((u) => (u[0] & 0x1f) === NAL.SPS);
  const stsd = find(init, "stsd").body;
  assert.ok(Buffer.from(stsd).includes(Buffer.from(sps)), "avcC must carry the SPS");

  // A changed SPS -- here a different level byte -- needs a new init segment.
  const changed = Uint8Array.from(aus[0]);
  // The header byte as the stream has it: nal_ref_idc varies, so 0x67 is
  // not a safe guess -- an earlier version searched for it, found nothing,
  // and "changed" an SPS it never touched.
  const at = Buffer.from(changed).indexOf(Buffer.from(sps.subarray(0, 4)));
  assert.ok(at >= 0, "the SPS must be found in its own access unit");
  changed[at + 3] = 0x1f;
  const again = mux.push(changed);
  assert.ok(again.init, "a new SPS must re-init");
  assert.equal(again.codec, "avc1.64001f");
});

test("each fragment points at its own sample, timed and flagged", () => {
  const aus = fixture();
  const mux = new Fmp4Muxer({ frameDuration: 3000 });
  aus.forEach((au, i) => {
    const { fragment, keyframe } = mux.push(au);
    const list = boxes(fragment);
    const moof = find(list, "moof");
    const mdat = find(list, "mdat");
    const trun = find(list, "trun").body;
    const tfdt = find(list, "tfdt").body;
    const mfhd = find(list, "mfhd").body;
    // trun's data offset, from the moof's first byte, lands on the mdat payload.
    assert.equal(moof.start + u32at(trun, 8), mdat.start + 8);
    assert.equal(u32at(trun, 4), 1, "one sample per fragment");
    assert.equal(u32at(trun, 12), 3000, "nominal duration");
    assert.equal(u32at(trun, 16), mdat.size - 8, "sample size is the mdat payload");
    assert.equal(u32at(mfhd, 4), i + 1, "sequence numbers count up from 1");
    assert.equal(u32at(tfdt, 8), i * 3000, "decode times step by the frame duration");
    const flags = u32at(trun, 20);
    assert.equal(keyframe, i < 2);
    assert.equal(flags, keyframe ? 0x02000000 : 0x01010000);
    // The payload is length-prefixed slices only: no SPS, PPS or SEI left in it.
    const payload = mdat.body;
    const kinds = [];
    for (let o = 0; o < payload.length; o += 4 + u32at(payload, o)) kinds.push(payload[o + 4] & 0x1f);
    assert.deepEqual(kinds, keyframe ? [NAL.IDR] : [NAL.SLICE]);
  });
});

test("a decode time past 32 bits is written whole", () => {
  const big = 2 ** 32 + 5;
  const seg = mediaSegment({ sequence: 1, decodeTime: big, duration: 1, sample: Uint8Array.of(0, 0, 0, 1, 0x65), keyframe: true });
  const tfdt = find(boxes(seg), "tfdt").body;
  assert.equal(u32at(tfdt, 4) * 2 ** 32 + u32at(tfdt, 8), big);
});
