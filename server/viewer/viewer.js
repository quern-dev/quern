// quern's LAN viewer (#449): plays a recording's live stream.
//
// The decoder is chosen by what the browser has, measured, not assumed:
// WebCodecs where VideoDecoder exists (iPad Safari has it even on plain HTTP),
// else Media Source Extensions through quern's own fMP4 muxer (Chrome on a LAN
// origin), else a message. ?muxer=quern or ?muxer=jmuxer forces an MSE path,
// for A/B testing; jmuxer is only there when the server was given a copy.
import { Fmp4Muxer, NAL, codecString, parseSps, splitNalUnits } from "./fmp4.js";

const params = new URLSearchParams(location.search);
const forced = params.get("muxer");
const canvas = document.getElementById("canvas");
const video = document.getElementById("video");
const statusEl = document.getElementById("status");
const FRAME_US = 33333;
const MAX_LAG_S = 0.3;

const say = (text) => { statusEl.textContent = text; };
function offAir(reason) {
  document.body.classList.add("offair");
  document.getElementById("reason").textContent = reason || "";
}
function onAir() { document.body.classList.remove("offair"); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const stats = params.has("stats") ? { frames: 0, last: 0 } : null;

// ---- decoders: one per connection, so a reconnect starts clean ----------

function webCodecsDecoder() {
  const ctx = canvas.getContext("2d");
  canvas.style.display = "block";
  video.style.display = "none";
  let decoder = null;
  let ts = 0;
  return {
    name: "WebCodecs",
    async push(au) {
      const units = splitNalUnits(au);
      const key = units.some((u) => (u[0] & 0x1f) === NAL.IDR);
      const sps = units.find((u) => (u[0] & 0x1f) === NAL.SPS);
      if (!decoder) {
        if (!key || !sps) return;                  // wait for a keyframe
        const config = { codec: codecString(parseSps(sps)), optimizeForLatency: true };
        const { supported } = await VideoDecoder.isConfigSupported(config);
        if (!supported) throw new Error(`this browser can't decode ${config.codec}`);
        decoder = new VideoDecoder({
          output: (frame) => {
            if (canvas.width !== frame.displayWidth) canvas.width = frame.displayWidth;
            if (canvas.height !== frame.displayHeight) canvas.height = frame.displayHeight;
            ctx.drawImage(frame, 0, 0);
            frame.close();
            shown();
          },
          error: (e) => { decoder = null; say(`decode failed (${e.message}); waiting for a keyframe`); },
        });
        decoder.configure(config);
      }
      ts += FRAME_US;
      decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: ts, data: au }));
    },
    close() { try { decoder && decoder.close(); } catch { /* already closed */ } },
  };
}

function mseDecoder() {
  const MS = window.MediaSource || window.ManagedMediaSource;
  canvas.style.display = "none";
  video.style.display = "block";
  const ms = new MS();
  video.disableRemotePlayback = true;               // iPhone Safari's MMS needs it
  video.src = URL.createObjectURL(ms);
  const opened = new Promise((r) => ms.addEventListener("sourceopen", r, { once: true }));
  const mux = new Fmp4Muxer({ frameDuration: 90000 * FRAME_US / 1e6 });
  const queue = [];
  let sb = null;
  let activeCodec = null;
  const pump = () => {
    while (sb && !sb.updating && queue.length) {
      const item = queue.shift();
      if (item.changeType) {
        if (typeof sb.changeType === "function") sb.changeType(`video/mp4; codecs="${item.changeType}"`);
        continue;
      }
      try { sb.appendBuffer(item.data); } catch (e) { say(`append failed: ${e.message}`); }
      return;
    }
  };
  const live = setInterval(() => {
    if (!video.buffered.length) return;
    const end = video.buffered.end(video.buffered.length - 1);
    if (end - video.currentTime > MAX_LAG_S) video.currentTime = end - 0.05;
    if (video.paused) video.play().catch(() => {});
  }, 250);
  if (video.requestVideoFrameCallback) {
    const on = () => { shown(); video.requestVideoFrameCallback(on); };
    video.requestVideoFrameCallback(on);
  }
  return {
    name: "MSE (quern)",
    async push(au) {
      await opened;
      const out = mux.push(au);
      if (!out) return;
      if (out.init) {
        if (!sb) {
          sb = ms.addSourceBuffer(`video/mp4; codecs="${out.codec}"`);
          activeCodec = out.codec;
          sb.mode = "segments";
          sb.addEventListener("updateend", pump);
        } else if (out.codec !== activeCodec) {
          queue.push({ changeType: out.codec });
          activeCodec = out.codec;
        }
        queue.push({ data: out.init });
      }
      queue.push({ data: out.fragment });
      pump();
    },
    close() { clearInterval(live); try { URL.revokeObjectURL(video.src); } catch { /* fine */ } },
  };
}

async function jmuxerDecoder() {
  if (!window.JMuxer) {
    // A <script src>, not fetched text run inline: the page's CSP allows
    // scripts from this origin and nothing inline.
    await new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "jmuxer.js";
      s.onload = resolve;
      s.onerror = () => reject(new Error("jmuxer is not configured on this server (viewer.jmuxer_path)"));
      document.head.append(s);
    });
  }
  canvas.style.display = "none";
  video.style.display = "block";
  const jm = new window.JMuxer({ node: video, mode: "video", flushingTime: 0,
    maxDelay: MAX_LAG_S * 1000, clearBuffer: true, fps: 30 });
  if (video.requestVideoFrameCallback) {
    const on = () => { shown(); video.requestVideoFrameCallback(on); };
    video.requestVideoFrameCallback(on);
  }
  return { name: "MSE (jmuxer)", async push(au) { jm.feed({ video: au, duration: FRAME_US / 1000 }); },
    close() { try { jm.destroy(); } catch { /* fine */ } } };
}

async function makeDecoder() {
  const hasMse = !!(window.MediaSource || window.ManagedMediaSource);
  if (forced === "jmuxer") return jmuxerDecoder();
  if (forced === "quern") {
    if (!hasMse) throw new Error("this browser has no Media Source Extensions");
    return mseDecoder();
  }
  if (typeof VideoDecoder !== "undefined") return webCodecsDecoder();
  if (hasMse) return mseDecoder();
  throw new Error("this browser can decode neither WebCodecs nor MSE H.264");
}

let picture = false;
function shown() {
  if (!picture) { picture = true; onAir(); say(`live · ${current ? current.name : ""}`); }
  if (stats) {
    stats.frames += 1;
  }
}
if (stats) {
  setInterval(() => {
    const n = stats.frames; stats.frames = 0;
    if (picture) say(`live · ${current ? current.name : ""} · ${n / 2} fps`);
  }, 2000);
}

// ---- one connection ------------------------------------------------------

let current = null;

// A picture now, even on a still screen, where nothing is encoded until it
// changes. quern-media sends a new viewer one keyframe, and Chrome's MSE
// presents nothing from a buffer holding a single frame (measured: frames 0
// for as long as the screen stayed still), so ask until there is a picture.
// The server allows one a second per recording, shared by everyone watching:
// 429 says when to ask again.
// One loop per connection: a reconnect ends the last one's.
let connection = 0;
async function askForKeyframe() {
  const mine = ++connection;
  for (let i = 0; i < 5 && !picture && mine === connection; i += 1) {
    let res;
    try { res = await fetch("keyframe", { method: "POST" }); } catch { return; }
    if (res.status !== 204 && res.status !== 429) return;   // not filming, or gone
    const after = res.status === 429 ? Number(res.headers.get("retry-after")) || 1 : 1.1;
    await sleep(1000 * after);
  }
}

async function playOnce() {
  picture = false;
  const res = await fetch("frames", { cache: "no-store" });
  if (res.status === 404) return { stop: true, reason: "this link has expired or been revoked" };
  if (!res.ok || !res.body) {
    const why = (await res.text().catch(() => "")).trim();
    return { reason: why || `the server answered ${res.status}` };
  }
  current = await makeDecoder();
  askForKeyframe();
  const reader = res.body.getReader();
  let buf = new Uint8Array(0);
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return { reason: "the stream ended" };
      const joined = new Uint8Array(buf.length + value.length);
      joined.set(buf); joined.set(value, buf.length); buf = joined;
      for (;;) {
        let h = -1;
        for (let i = 0; i + 3 < buf.length; i += 1) {
          if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) { h = i; break; }
        }
        if (h < 0) break;
        const m = /content-length:\s*(\d+)/i.exec(new TextDecoder().decode(buf.subarray(0, h)));
        if (!m) return { reason: "the stream is malformed" };
        const len = Number(m[1]);
        if (buf.length < h + 4 + len + 2) break;
        const au = buf.slice(h + 4, h + 4 + len);
        buf = buf.slice(h + 4 + len + 2);
        if (len) await current.push(au);          // an empty part is a keepalive
      }
    }
  } finally {
    current.close();
  }
}

(async () => {
  let failures = 0;
  for (;;) {
    let outcome;
    try {
      outcome = await playOnce();
    } catch (e) {
      outcome = { reason: e && e.message ? e.message : String(e) };
    }
    const wasLive = picture;
    offAir(outcome.reason);
    if (outcome.stop) { say(outcome.reason); return; }
    failures = wasLive ? 0 : failures + 1;
    const wait = Math.min(5000, 1000 * 2 ** Math.max(failures - 1, 0));
    say(`${outcome.reason} · reconnecting`);
    await sleep(wait);
  }
})();
