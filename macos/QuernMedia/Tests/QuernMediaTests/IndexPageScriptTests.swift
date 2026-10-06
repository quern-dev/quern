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
    let wire = HTTPWire.h264Part(tricky) + HTTPWire.mjpegPart(big) + HTTPWire.h264Part(small)

    let parts = try page.parts(wire, chunkSize: chunkSize)

    #expect(parts.count == 3)
    #expect(parts.map(\.0) == ["video/h264", "image/jpeg", "video/h264"])
    #expect(parts.map(\.1) == [tricky, big, small])
    #expect(page.exception == nil)
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
