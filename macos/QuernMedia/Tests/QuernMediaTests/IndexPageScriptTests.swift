import Foundation
import JavaScriptCore
import Testing
@testable import QuernMedia

// The index page is the only player for H.264 in a browser, and everything it
// does rests on one parser reading the server's framing correctly. That parser
// is JavaScript, so these run it in JavaScriptCore against bytes made by the
// same `HTTPWire` functions the server sends with -- split at every awkward
// boundary TCP is free to produce.

private func pageScript() throws -> String {
    let html = HTTPWire.indexHTML
    let start = try #require(html.range(of: "<script>")).upperBound
    let end = try #require(html.range(of: "</script>")).lowerBound
    return String(html[start..<end])
}

/// A context with the page script loaded and the two browser APIs the parser
/// touches. Fails the test on any script exception rather than reading it as
/// an empty result.
private final class Page {
    let context: JSContext
    private(set) var exception: String?

    init() throws {
        context = try #require(JSContext())
        context.exceptionHandler = { [weak self] _, value in
            self?.exception = value?.toString() ?? "unknown"
        }
        context.evaluateScript("""
            class TextDecoder {
              decode(bytes) {
                let s = "";
                for (const b of bytes) s += String.fromCharCode(b);
                return s;
              }
            }
            function reader(chunks) {
              let i = 0;
              return {
                read: async () => i < chunks.length
                  ? { value: new Uint8Array(chunks[i++]), done: false }
                  : { value: undefined, done: true },
              };
            }
            async function collect(chunks) {
              const parts = new Parts(reader(chunks));
              const out = [];
              for (;;) {
                const part = await parts.next();
                if (!part) return out;
                out.push({ type: part.type, body: Array.from(part.body) });
              }
            }
            """)
        context.evaluateScript(try pageScript())
        #expect(exception == nil, "the page script failed to load: \(exception ?? "")")
    }

    /// Feeds `wire` to the parser in chunks of `chunkSize` and returns the
    /// parts it produced, as (type, body).
    func parts(_ wire: Data, chunkSize: Int) throws -> [(String, Data)] {
        var chunks: [[UInt8]] = []
        var i = wire.startIndex
        while i < wire.endIndex {
            let j = wire.index(i, offsetBy: chunkSize, limitedBy: wire.endIndex) ?? wire.endIndex
            chunks.append(Array(wire[i..<j]))
            i = j
        }
        context.setObject(chunks, forKeyedSubscript: "chunks" as NSString)
        context.evaluateScript("""
            globalThis.result = undefined;
            collect(chunks).then(
              r => { globalThis.result = r; },
              e => { globalThis.result = "error: " + e; },
            );
            """)
        // JavaScriptCore drains the promise queue before evaluateScript
        // returns, so the result is already there.
        let result = try #require(context.objectForKeyedSubscript("result"))
        #expect(!result.isUndefined, "the parser never finished")
        #expect(!result.isString, "the parser threw: \(result.toString() ?? "")")
        let array = result.toArray() as? [[String: Any]] ?? []
        return array.map { part in
            let body = (part["body"] as? [NSNumber] ?? []).map { $0.uint8Value }
            return (part["type"] as? String ?? "", Data(body))
        }
    }
}

private func payload(_ count: Int, seed: UInt8) -> Data {
    Data((0..<count).map { UInt8(truncatingIfNeeded: $0 &* 31 &+ Int(seed)) })
}

@Test("the page reads every part whole, however the bytes arrive", arguments: [1, 7, 1460, 1 << 20])
func pageParsesPartsAtAnyChunking(chunkSize: Int) throws {
    let page = try Page()
    // A payload that is itself full of CRLFs and boundary-like bytes, and one
    // larger than the parser's starting buffer, so growth is exercised.
    let tricky = Data("\r\n\r\n--quernframe\r\nContent-Length: 9\r\n\r\n".utf8)
    let big = payload(200_000, seed: 3)
    let small = payload(5, seed: 9)
    // And an empty part last -- the H.264 keepalive -- whose header ends at
    // the very end of the bytes, so the search has to look at the final four.
    let wire = HTTPWire.h264Part(tricky) + HTTPWire.mjpegPart(big) + HTTPWire.h264Part(small)
        + HTTPWire.h264Part(Data())

    let parts = try page.parts(wire, chunkSize: chunkSize)

    #expect(parts.count == 4)
    #expect(parts.map(\.0) == ["video/h264", "image/jpeg", "video/h264", "video/h264"])
    #expect(parts.map(\.1) == [tricky, big, small, Data()])
    #expect(page.exception == nil)
}

@Test("an empty part whose header ends the bytes so far is still delivered")
func pageDeliversAnEmptyPartAtTheEnd() throws {
    // The keepalive on a still screen: its header is the last thing to
    // arrive until the next one, seconds later. A search that stops one byte
    // short of the end holds it back until then -- or forever, if the stream
    // ends there.
    let page = try Page()
    let first = payload(40, seed: 4)
    var wire = HTTPWire.mjpegPart(first) + HTTPWire.h264Part(Data())
    wire.removeLast(2)  // the trailing CRLF, which arrives with the next part

    let parts = try page.parts(wire, chunkSize: 1460)

    #expect(parts.count == 2)
    #expect(parts.last?.1 == Data())
}

@Test("a stream cut off mid-part yields only the parts that arrived whole")
func pageDropsATruncatedPart() throws {
    // What the page sees when quern-media goes away: the reader ends. A part
    // cut short must not be handed to a decoder as if it were a frame.
    let page = try Page()
    let whole = payload(300, seed: 1)
    var wire = HTTPWire.mjpegPart(whole) + HTTPWire.mjpegPart(payload(300, seed: 2))
    wire.removeLast(100)

    let parts = try page.parts(wire, chunkSize: 64)

    #expect(parts.count == 1)
    #expect(parts.first?.1 == whole)
}

@Test("the page names the decoder from the stream's SPS")
func pageDerivesTheCodecString() throws {
    let page = try Page()
    // SPS for High profile (0x64), no constraint flags, level 5.0 (0x32),
    // after the start code and the NAL header (0x67), followed by an IDR.
    page.context.evaluateScript("""
        const au = new Uint8Array([0, 0, 0, 1, 0x67, 0x64, 0x00, 0x32, 0xac,
                                   0, 0, 1, 0x68, 0xee,
                                   0, 0, 0, 1, 0x65, 0x88, 0x84]);
        const units = nalUnits(au);
        const types = units.map(i => au[i] & 0x1f);
        globalThis.codec = avcCodec(au, units, types);
        globalThis.types = types.join(",");
        globalThis.none = avcCodec(au, [], []);
        """)
    #expect(page.exception == nil)
    #expect(page.context.objectForKeyedSubscript("types")?.toString() == "7,8,5",
            "three- and four-byte start codes should both be found")
    #expect(page.context.objectForKeyedSubscript("codec")?.toString() == "avc1.640032")
    #expect(page.context.objectForKeyedSubscript("none")?.isNull == true,
            "an access unit with no SPS must not invent a codec")
}

// MARK: - The player, on virtual time

/// A browser, as far as the page touches one: timers on a virtual clock,
/// `fetch` answering from a script the test writes, `AbortController`, and a
/// DOM of three elements. Installed before the page script, because the page
/// starts playing the moment it sees a `document`.
///
/// Everything runs inside one `evaluateScript`: no real timer is ever waited
/// on, so a scenario of a minute of page time takes milliseconds.
private let browserStub = #"""
let now = 0;
let timers = [];
let timerID = 0;
globalThis.setTimeout = (fn, ms) => {
  const id = ++timerID;
  timers.push({ id, at: now + (ms || 0), fn });
  return id;
};
globalThis.clearTimeout = id => { timers = timers.filter(t => t.id !== id); };
globalThis.setInterval = () => 0;
globalThis.performance = { now: () => now };
async function settle() { for (let i = 0; i < 200; i++) await null; }
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    await settle();
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const next = timers[0];
    if (!next || next.at > end) break;
    timers.shift();
    now = next.at;
    next.fn();
  }
  now = end;
  await settle();
}

class AbortController {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener: (_, fn) => listeners.push(fn),
    };
    this.abort = () => {
      if (this.signal.aborted) return;
      this.signal.aborted = true;
      listeners.forEach(fn => fn());
    };
  }
}

function element() {
  const classes = new Set();
  return {
    textContent: "", width: 0, height: 0,
    classList: { toggle: (c, on) => { on ? classes.add(c) : classes.delete(c); },
                 contains: c => classes.has(c) },
    getContext: () => ({ drawImage: () => { globalThis.draws = (globalThis.draws || 0) + 1; } }),
    append() {},
  };
}
const elements = { screen: element(), status: element(), stats: element() };
globalThis.document = { getElementById: id => elements[id], body: element() };
globalThis.location = { search: "", host: "127.0.0.1:8422" };
globalThis.URLSearchParams = class { constructor(s) { this.s = s; } has(k) { return this.s.includes(k); } };
globalThis.isSecureContext = true;
globalThis.Blob = class { constructor(parts) { this.parts = parts; } };
globalThis.TextDecoder = class {
  decode(b) { let s = ""; for (const c of b) s += String.fromCharCode(c); return s; }
};
// Resolves a virtual tick later, so a frame can still be decoding when its
// stream ends -- the race the generation counter exists for.
globalThis.createImageBitmap = () => new Promise(resolve =>
  setTimeout(() => resolve({ width: 4, height: 8, close() {} }), 20));

const bytes = s => Array.from(s, c => c.charCodeAt(0));
function part(type, body) {
  return bytes("--quernframe\r\nContent-Type: " + type + "\r\nContent-Length: "
    + body.length + "\r\n\r\n").concat(body, bytes("\r\n"));
}

// Each fetch takes the next scripted answer. An answer is a function of the
// abort signal returning what fetch resolves to, or throwing to refuse.
const answers = [];
const fetches = [];
globalThis.fetch = async (url, options) => {
  fetches.push(now);
  const answer = answers.shift() || refused;
  return answer(options.signal);
};
function refused() { throw new TypeError("Failed to fetch"); }

// A multipart response whose reader yields `steps`: an array of bytes is a
// chunk, `{ wait: ms }` delays the next step, "end" ends the stream, and
// "hang" waits until aborted.
function streaming(steps, type) {
  return signal => {
    let i = 0;
    return {
      ok: true, status: 200,
      headers: { get: () => type || "multipart/x-mixed-replace; boundary=quernframe" },
      body: { getReader: () => ({
        read: () => {
          let step = steps[i++];
          if (step && step.wait) {
            const ms = step.wait;
            // A real reader rejects the moment its fetch is aborted.
            return new Promise((resolve, reject) => {
              setTimeout(resolve, ms);
              signal.addEventListener("abort", () => reject(new Error("aborted")));
            }).then(() => {
              step = steps[i++];
              return step === undefined || step === "end"
                ? { done: true } : { value: new Uint8Array(step), done: false };
            });
          }
          if (step === undefined || step === "end") return Promise.resolve({ done: true });
          if (step === "hang") {
            return new Promise((_, reject) =>
              signal.addEventListener("abort", () => reject(new Error("aborted"))));
          }
          return Promise.resolve({ value: new Uint8Array(step), done: false });
        },
      }) },
    };
  };
}

function status() { return elements.status.textContent; }
// Not `offAir`: the page defines that, and a script-scope function of the
// same name is replaced by it.
function isOffAir() { return document.body.classList.contains("offair"); }
"""#

private final class Player {
    let context: JSContext
    private(set) var exception: String?

    /// Loads the stub, then `answers` (JavaScript pushing onto `answers`),
    /// then the page script, which starts playing.
    init(answers: String) throws {
        context = try #require(JSContext())
        context.exceptionHandler = { [weak self] _, value in
            self?.exception = value?.toString() ?? "unknown"
        }
        context.evaluateScript(browserStub)
        context.evaluateScript(answers)
        context.evaluateScript(try pageScript())
        #expect(exception == nil, "setup failed: \(exception ?? "")")
    }

    /// Runs an async scenario to completion and returns what it resolved to.
    func run(_ body: String) -> JSValue? {
        context.evaluateScript("""
            globalThis.outcome = undefined;
            (async () => { \(body) })().then(
              r => { globalThis.outcome = r; },
              e => { globalThis.outcome = "scenario threw: " + e; });
            """)
        #expect(exception == nil, "the scenario failed: \(exception ?? "")")
        return context.objectForKeyedSubscript("outcome")
    }
}

@Test("a frame still decoding when its stream ends does not clear OFF AIR")
func aLateFrameDoesNotPutTheStreamBackOnAir() throws {
    // The last JPEG and the end of the stream arrive together. The page goes
    // off air at once; the JPEG finishes decoding a moment later and used to
    // be drawn, which cleared OFF AIR and the status and showed a dead frame
    // as live until the next reconnect failed.
    let player = try Player(answers: """
        answers.push(streaming([part("image/jpeg", [0xff, 0xd8, 1, 2]), "end"]));
        """)
    let outcome = player.run("""
        await advance(1);
        const right = { offAir: isOffAir(), status: status() };
        await advance(100);
        return { offAir: isOffAir(), status: status(), right, draws: globalThis.draws || 0 };
        """)
    let result = try #require(outcome?.toDictionary() as? [String: Any])
    let right = try #require(result["right"] as? [String: Any])
    #expect(right["offAir"] as? Bool == true, "the end of the stream did not go off air")
    #expect(result["offAir"] as? Bool == true, "a frame from the dead stream put it back on air")
    #expect((result["status"] as? String)?.contains("ended") == true, "got: \(result["status"] ?? "")")
    #expect(result["draws"] as? Int == 0, "the dead stream's frame was drawn")
}

@Test("after a stream that worked, the next reconnect is quick again")
func backoffResetsAfterAWorkingStream() throws {
    // Five refusals push the delay to its 5s cap. A stream that then showed
    // a picture must reset it, or every later restart waits 5 seconds.
    let player = try Player(answers: """
        for (let i = 0; i < 5; i++) answers.push(refused);
        answers.push(streaming([part("image/jpeg", [0xff, 0xd8, 1]), "hang"]));
        """)
    let outcome = player.run("""
        await advance(20000);            // the refusals, then the stream
        const drew = globalThis.draws || 0;
        // Step until the stalled stream is given up on, then time the next try.
        while (!status().includes("No frames") && now < 60000) await advance(50);
        const stalledAt = now;
        const before = fetches.length;
        await advance(10000);
        const next = fetches.slice(before)[0];
        return { drew, gap: next === undefined ? -1 : next - stalledAt };
        """)
    let result = try #require(outcome?.toDictionary() as? [String: Any])
    #expect(result["drew"] as? Int == 1, "the working stream never drew its frame")
    let gap = try #require(result["gap"] as? Double)
    #expect(gap >= 0 && gap <= 600, "the reconnect after a working stream waited \(gap)ms")
}

@Test("parts that never become a picture are a stream that is not working")
func noPictureMeansReconnect() throws {
    // H.264 delta frames with no keyframe decode to nothing. They arrive
    // every 4s here, inside the 12s liveness window, so only the first-frame
    // deadline can end it; without that the page sat on "Connecting…" forever.
    let player = try Player(answers: """
        const steps = [];
        for (let i = 0; i < 10; i++) steps.push({ wait: 4000 }, part("video/h264", [0, 0, 0, 1, 0x41, 9]));
        steps.push("hang");
        answers.push(streaming(steps));
        """)
    let outcome = player.run("""
        await advance(15100);
        return { status: status(), offAir: isOffAir(), fetches: fetches.length };
        """)
    let result = try #require(outcome?.toDictionary() as? [String: Any])
    #expect((result["status"] as? String)?.contains("No picture arrived") == true,
            "got: \(result["status"] ?? "")")
    #expect(result["offAir"] as? Bool == true)
}

@Test("a simulator that is not running reads as off air, with its state")
func unavailableSourceSaysWhy() throws {
    let player = try Player(answers: """
        answers.push(() => ({
          ok: false, status: 503, headers: { get: () => "text/plain" },
          text: async () => "the simulator is shutdown\\n",
        }));
        """)
    let outcome = player.run("""
        await advance(1);
        return { status: status(), offAir: isOffAir() };
        """)
    let result = try #require(outcome?.toDictionary() as? [String: Any])
    #expect((result["status"] as? String)?.hasPrefix("Off air: the simulator is shutdown.") == true,
            "got: \(result["status"] ?? "")")
    #expect(result["offAir"] as? Bool == true)
}

@Test("an older quern-media that answers /frames with its index page is named as such")
func anOlderServerIsNamed() throws {
    // It serves the index HTML for any path it does not know, so the page
    // used to report "Cannot reach quern-media" while reaching it fine.
    let player = try Player(answers: """
        answers.push(streaming([bytes("<!doctype html>"), "end"], "text/html; charset=utf-8"));
        """)
    let outcome = player.run("""
        await advance(1);
        return status();
        """)
    #expect(outcome?.toString().contains("does not serve /frames") == true,
            "got: \(outcome?.toString() ?? "")")
}
