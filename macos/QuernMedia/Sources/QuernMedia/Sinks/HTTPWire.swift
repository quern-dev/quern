import Foundation

/// The wire formats the HTTP sink speaks, kept separate from the socket code
/// so they can be tested without opening a port.
public enum HTTPWire {
    public static let mjpegBoundary = "quernframe"

    /// Path from a request head, or "/" if it cannot be parsed.
    ///
    /// Deliberately forgiving: this serves a preview to a browser, not an API.
    /// A malformed request should get the index page rather than a diagnostic.
    public static func requestPath(_ head: String) -> String {
        guard let line = head.split(separator: "\r\n").first else { return "/" }
        let parts = line.split(separator: " ")
        guard parts.count >= 2 else { return "/" }
        return String(parts[1])
    }

    /// Exact match, or with a query string. A bare prefix test would also
    /// claim `/streamer`, and quietly serving video from a path nobody meant
    /// to hit is the kind of thing that is discovered much later.
    public static func isStreamPath(_ path: String) -> Bool {
        path == "/stream" || path.hasPrefix("/stream?")
    }

    /// The stream with every frame in its own length-prefixed part.
    ///
    /// For the index page. Under MJPEG it is byte-for-byte `/stream`, which is
    /// already multipart. Under H.264 `/stream` is a bare elementary stream,
    /// and a reader cannot tell where one access unit ends until the next one
    /// starts -- so a still screen, which sends one keyframe and then nothing,
    /// never shows at all. A part's `Content-Length` says where it ends.
    /// `/stream` stays as it was, for tools that read a raw elementary stream.
    public static func isFramesPath(_ path: String) -> Bool {
        path == "/frames" || path.hasPrefix("/frames?")
    }

    /// Method from a request head, verbatim. Empty when unparseable.
    ///
    /// Not uppercased. HTTP methods are case-sensitive (RFC 9110 section 9.1),
    /// so `post` is not `POST` and a server that accepts it is inventing a
    /// dialect. This used to uppercase, which meant the path half of the
    /// control guard was matched byte-exactly while the method half was
    /// normalised -- two different standards inside one check.
    public static func requestMethod(_ head: String) -> String {
        guard let line = head.split(separator: "\r\n").first else { return "" }
        guard let method = line.split(separator: " ").first else { return "" }
        return String(method)
    }

    /// A request path with its query and any trailing slashes removed.
    ///
    /// Not a general URL normaliser: no percent-decoding and no `..`
    /// collapsing, because nothing here resolves a path against a filesystem.
    static func normalizedPath(_ path: String) -> String {
        var p = path
        if let q = p.firstIndex(of: "?") { p = String(p[p.startIndex..<q]) }
        while p.count > 1 && p.hasSuffix("/") { p.removeLast() }
        return p
    }

    /// How a request path relates to the control endpoint.
    public enum ControlMatch: Equatable {
        /// Fire the hook.
        case exact
        /// Meant for the control endpoint and mistyped. Must be refused, and
        /// specifically must not fall through to the index page.
        case nearMiss
        /// Nothing to do with the control endpoint.
        case other
    }

    /// Classify a path against the control endpoint.
    ///
    /// A query string and a trailing slash are accepted, matching the
    /// forgiveness `isStreamPath` already has. They were not, and the
    /// consequence was the failure shape this project keeps rediscovering:
    /// `POST /keyframe?t=1` fell through to the index page and answered
    /// **200 with HTML**, so a caller whose only signal from a 204 endpoint
    /// is the status code read a silently discarded request as success.
    /// `curl -sSf` exited 0 on it. Measured, not theorised.
    ///
    /// A case mismatch is a near miss rather than an exact match, because
    /// paths *are* case-sensitive -- but answering `/KEYFRAME` with the index
    /// page reintroduces the same false success, so it is refused explicitly.
    public static func keyframePathMatch(_ path: String) -> ControlMatch {
        let normalized = normalizedPath(path)
        if normalized == keyframePath { return .exact }
        if normalized.lowercased() == keyframePath { return .nearMiss }
        return .other
    }

    private static let keyframePath = "/keyframe"

    /// Whether this path is the control endpoint, exactly.
    ///
    /// Reachable from the network under `--bind-all`, where the stream is
    /// already unauthenticated. A single unwanted call costs one encode; a
    /// *rate* of them costs more than that, and the earlier version of this
    /// comment claimed the former while the latter was what mattered.
    /// Measured on a booted simulator: ~3,500 requests a second forced 19
    /// frames in 20 to IDR, so a sustained stream of them makes an H.264
    /// stream — and any `--record` file — effectively all-intra. It is
    /// bounded by `--fps` and self-heals the moment the requests stop, and
    /// `--bind-all` is opt-in and already documented as unauthenticated, so
    /// this is a cost to know about rather than a hole. Do not repeat the
    /// "one encode" framing; it is true per call and false per second.
    public static func isKeyframePath(_ path: String) -> Bool {
        keyframePathMatch(path) == .exact
    }

    /// The control endpoint exists but nothing is wired to serve it.
    ///
    /// 503 rather than 204: answering "no content" for a request that did no
    /// work is the same false success a near-miss path used to give, and this
    /// one is on the endpoint's own happy path.
    public static func noEncoderResponse() -> Data {
        Data("HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
            .appending("Connection: close\r\n\r\n").utf8)
    }

    public static func notFoundResponse() -> Data {
        Data("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
            .appending("Connection: close\r\n\r\n").utf8)
    }

    public static func noContentResponse() -> Data {
        Data("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n".utf8)
    }

    public static func methodNotAllowedResponse() -> Data {
        Data("HTTP/1.1 405 Method Not Allowed\r\nAllow: POST\r\n"
            .appending("Connection: close\r\n\r\n").utf8)
    }

    /// One MJPEG part: boundary, headers, payload.
    ///
    /// `multipart/x-mixed-replace` is why `/stream` needs no client-side code
    /// at all -- a browser renders it from a bare `<img>`.
    public static func mjpegPart(_ jpeg: Data) -> Data {
        part(jpeg, contentType: "image/jpeg")
    }

    /// One H.264 access unit as a part, for `/frames`.
    public static func h264Part(_ annexB: Data) -> Data {
        part(annexB, contentType: "video/h264")
    }

    private static func part(_ payload: Data, contentType: String) -> Data {
        var part = Data()
        part.append(Data("--\(mjpegBoundary)\r\n".utf8))
        part.append(Data("Content-Type: \(contentType)\r\n".utf8))
        part.append(Data("Content-Length: \(payload.count)\r\n\r\n".utf8))
        part.append(payload)
        part.append(Data("\r\n".utf8))
        return part
    }

    /// `/frames` is multipart for either codec; each part names its own type.
    public static let framesContentType =
        "multipart/x-mixed-replace; boundary=\(mjpegBoundary)"

    public static func streamHeader(contentType: String) -> Data {
        Data("""
        HTTP/1.1 200 OK\r
        Content-Type: \(contentType)\r
        Cache-Control: no-store\r
        Connection: close\r
        \r\n
        """.utf8)
    }

    public static func contentType(for codec: StreamPipeline.Codec) -> String {
        switch codec {
        case .mjpeg: return "multipart/x-mixed-replace; boundary=\(mjpegBoundary)"
        // A raw elementary stream, for tools that read one. The page reads
        // `/frames` instead, which delimits each access unit.
        case .h264: return "video/h264"
        }
    }

    /// The index page's response headers, with no body.
    ///
    /// For `HEAD`, which must carry the headers a `GET` would — including the
    /// real `Content-Length` — and no body. Handing the whole page to `send`
    /// put the body on the wire, which is what this used to do.
    public static func indexHeaders(for codec: StreamPipeline.Codec) -> Data {
        Data(indexHead(for: codec).utf8)
    }

    private static func indexHead(for codec: StreamPipeline.Codec) -> String {
        """
        HTTP/1.1 200 OK\r
        Content-Type: text/html; charset=utf-8\r
        Content-Length: \(indexBody(for: codec).utf8.count)\r
        Connection: close\r
        \r

        """
    }

    public static func indexPage(for codec: StreamPipeline.Codec) -> Data {
        Data((indexHead(for: codec) + indexBody(for: codec)).utf8)
    }

    /// One page for both codecs. It reads `/frames` and dispatches on each
    /// part's type, so it keeps working if the process behind the port is
    /// restarted with the other codec.
    ///
    /// Script rather than `<img src="/stream">`, which could not tell a
    /// still screen from a stream that had ended: the image kept its last
    /// frame, with no message and no reconnect, after `quern-media` was gone.
    /// The script sees the stream end, says so, and reconnects. H.264 plays
    /// through WebCodecs, which takes Annex B as-is. This page used to send
    /// H.264 viewers to `ffplay`, with a literal `PORT` in the command: a
    /// third player beside the preview app and this page, and one that
    /// stalled on a still screen. Where `VideoDecoder` is missing the page
    /// now says what to use instead.
    private static func indexBody(for codec: StreamPipeline.Codec) -> String {
        indexHTML
    }

    static let indexHTML = #"""
    <!doctype html><meta charset=utf-8><title>Quern preview</title>
    <meta name=viewport content="width=device-width">
    <style>
    html,body{margin:0;height:100%;background:#111;color:#ccc;font:14px system-ui}
    body{display:grid;place-items:center;overflow:hidden}
    canvas{display:block;max-width:100vw;max-height:100vh}
    #status{position:fixed;left:0;right:0;bottom:0;padding:8px 12px;
    background:#000c;text-align:center}
    #status:empty{display:none}
    #stats{position:fixed;top:0;right:0;padding:4px 8px;background:#000c;
    font:12px ui-monospace,monospace;white-space:pre}
    #stats:empty{display:none}
    </style>
    <canvas id=screen width=0 height=0></canvas>
    <div id=status>Connecting…</div>
    <div id=stats></div>
    <script>
    "use strict";
    // Null outside a page, so a test can load this script and exercise the
    // parser without a DOM.
    const page = typeof document === "object" ? {
      canvas: document.getElementById("screen"),
      ctx: document.getElementById("screen").getContext("2d"),
      status: document.getElementById("status"),
      stats: document.getElementById("stats"),
    } : null;

    // `?stats` shows what the viewer is getting: frames drawn per second, the
    // longest gap between two of them, and how many H.264 frames are queued
    // in the decoder. A lurch is a long gap; a growing queue is the decoder
    // falling behind rather than the stream arriving late.
    const stats = page && new URLSearchParams(location.search).has("stats")
      ? { drawn: 0, lastDraw: 0, maxGap: 0 } : null;
    if (stats) {
      setInterval(() => {
        const queue = h264.decoder ? h264.decoder.decodeQueueSize : "-";
        page.stats.textContent = stats.drawn + " fps  max gap " + Math.round(stats.maxGap)
          + " ms  decode queue " + queue;
        stats.drawn = 0;
        stats.maxGap = 0;
      }, 1000);
    }
    // MJPEG repeats its last frame every 5s when the screen is still, so a
    // longer silence means the stream is gone. H.264 has no such repeat, and
    // silence there is only a still screen, so the watchdog stops once the
    // first H.264 frame arrives.
    const STALL_MS = 12000;
    const FIRST_FRAME_MS = 15000;

    function say(text) { page.status.textContent = text; }
    const sleep = ms => new Promise(r => setTimeout(r, ms));

    // Reads `--boundary` / headers / Content-Length body parts off a stream.
    class Parts {
      constructor(reader) {
        this.reader = reader;
        this.buf = new Uint8Array(1 << 16);
        this.start = 0;
        this.end = 0;
      }
      async fill() {
        const { value, done } = await this.reader.read();
        if (done) return false;
        if (this.end + value.length > this.buf.length) {
          const live = this.buf.subarray(this.start, this.end);
          const size = Math.max(this.buf.length, (live.length + value.length) * 2);
          const next = new Uint8Array(size);
          next.set(live);
          this.buf = next;
          this.start = 0;
          this.end = live.length;
        }
        this.buf.set(value, this.end);
        this.end += value.length;
        return true;
      }
      headerEnd() {
        const b = this.buf;
        for (let i = this.start; i + 4 <= this.end; i++) {
          if (b[i] === 13 && b[i + 1] === 10 && b[i + 2] === 13 && b[i + 3] === 10) {
            return i - this.start;
          }
        }
        return -1;
      }
      async next() {
        let h;
        while ((h = this.headerEnd()) < 0) {
          if (this.end - this.start > 4096) throw new Error("malformed part header");
          if (!(await this.fill())) return null;
        }
        const head = new TextDecoder().decode(this.buf.subarray(this.start, this.start + h));
        const len = /content-length:\s*(\d+)/i.exec(head);
        const type = /content-type:\s*([^\r\n;]+)/i.exec(head);
        if (!len) throw new Error("part without a Content-Length");
        const from = h + 4;
        const length = Number(len[1]);
        while (this.end - this.start < from + length) {
          if (!(await this.fill())) return null;
        }
        const body = this.buf.slice(this.start + from, this.start + from + length);
        this.start += from + length;
        return { type: type ? type[1].trim().toLowerCase() : "", body };
      }
    }

    let shown = false;
    function draw(source, width, height) {
      if (page.canvas.width !== width || page.canvas.height !== height) {
        page.canvas.width = width;
        page.canvas.height = height;
      }
      page.ctx.drawImage(source, 0, 0);
      if (!shown) { shown = true; say(""); }
      if (stats) {
        const now = performance.now();
        if (stats.lastDraw) stats.maxGap = Math.max(stats.maxGap, now - stats.lastDraw);
        stats.lastDraw = now;
        stats.drawn += 1;
      }
    }

    // One JPEG decoding at a time; a newer one replaces any still waiting.
    let pendingJPEG = null;
    let decodingJPEG = false;
    function showJPEG(bytes) {
      pendingJPEG = bytes;
      if (!decodingJPEG) pumpJPEG();
    }
    async function pumpJPEG() {
      decodingJPEG = true;
      while (pendingJPEG) {
        const bytes = pendingJPEG;
        pendingJPEG = null;
        try {
          const image = await createImageBitmap(new Blob([bytes], { type: "image/jpeg" }));
          draw(image, image.width, image.height);
          image.close();
        } catch (e) {
          console.warn("undecodable JPEG", e);
        }
      }
      decodingJPEG = false;
    }

    class Unsupported extends Error {}

    function nalUnits(au) {
      const units = [];
      for (let i = 0; i + 3 < au.length; i++) {
        if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1) {
          units.push(i + 3);
          i += 2;
        }
      }
      return units;
    }
    const hex = n => n.toString(16).padStart(2, "0");
    // The WebCodecs name for the stream, from its SPS: profile, constraint
    // flags and level, as in "avc1.640032".
    function avcCodec(au, units, types) {
      const at = units[types.indexOf(7)];
      if (at === undefined) return null;
      return "avc1." + hex(au[at + 1]) + hex(au[at + 2]) + hex(au[at + 3]);
    }

    const h264 = {
      decoder: null,
      timestamp: 0,
      reset() {
        if (this.decoder && this.decoder.state !== "closed") this.decoder.close();
        this.decoder = null;
      },
      async push(au) {
        const units = nalUnits(au);
        const types = units.map(i => au[i] & 0x1f);
        const key = types.includes(5);
        if (!this.decoder) {
          // Everything before the first IDR references pictures this viewer
          // never had, so it waits. The server sends one on attach.
          if (!key) return;
          const codec = avcCodec(au, units, types);
          if (!codec) return;
          if (typeof VideoDecoder === "undefined") throw new Unsupported();
          const config = { codec, optimizeForLatency: true };
          const { supported } = await VideoDecoder.isConfigSupported(config);
          if (!supported) throw new Unsupported();
          this.decoder = new VideoDecoder({
            output: frame => {
              draw(frame, frame.displayWidth, frame.displayHeight);
              frame.close();
            },
            error: e => {
              console.warn("H.264 decode failed; waiting for a keyframe", e);
              this.reset();
              fetch("/keyframe", { method: "POST" }).catch(() => {});
            },
          });
          this.decoder.configure(config);
        }
        this.timestamp += 33333;
        this.decoder.decode(new EncodedVideoChunk({
          type: key ? "key" : "delta", timestamp: this.timestamp, data: au,
        }));
      },
    };

    function showUnsupported() {
      say("This browser cannot decode H.264. Open this page in a current "
        + "Safari or Chrome, or run quern-media without --h264.");
    }

    async function playOnce() {
      const abort = new AbortController();
      let stalled = false;
      let watchdog = 0;
      const watch = ms => {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => { stalled = true; abort.abort(); }, ms);
      };
      watch(FIRST_FRAME_MS);
      try {
        const response = await fetch("/frames", { cache: "no-store", signal: abort.signal });
        if (!response.ok || !response.body) return "quern-media answered " + response.status + ".";
        const parts = new Parts(response.body.getReader());
        for (;;) {
          const part = await parts.next();
          if (!part) return "The stream ended.";
          if (part.type === "image/jpeg") {
            watch(STALL_MS);
            showJPEG(part.body);
          } else if (part.type === "video/h264") {
            clearTimeout(watchdog);
            await h264.push(part.body);
          }
        }
      } catch (e) {
        if (e instanceof Unsupported) throw e;
        if (stalled) return shown ? "No frames for 12 seconds." : "No picture arrived.";
        return "Cannot reach quern-media.";
      } finally {
        clearTimeout(watchdog);
        abort.abort();
        h264.reset();
      }
    }

    async function run() {
      for (let failures = 0; ; failures++) {
        let reason;
        try {
          reason = await playOnce();
        } catch (e) {
          if (e instanceof Unsupported) { showUnsupported(); return; }
          reason = String(e);
        }
        shown = false;
        say(reason + " Reconnecting…");
        await sleep(Math.min(5000, 500 * 2 ** Math.min(failures, 4)));
      }
    }
    if (page) run();
    </script>
    """#

}
