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
    /// `multipart/x-mixed-replace` is why the MJPEG path needs no client-side
    /// code at all -- a browser renders it from a bare `<img>`.
    public static func mjpegPart(_ jpeg: Data) -> Data {
        var part = Data()
        part.append(Data("--\(mjpegBoundary)\r\n".utf8))
        part.append(Data("Content-Type: image/jpeg\r\n".utf8))
        part.append(Data("Content-Length: \(jpeg.count)\r\n\r\n".utf8))
        part.append(jpeg)
        part.append(Data("\r\n".utf8))
        return part
    }

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
        // A raw elementary stream. ffplay and ffprobe read it; a browser does
        // not, which is the price H.264 charges over MJPEG.
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

    private static func indexBody(for codec: StreamPipeline.Codec) -> String {
        let body: String
        switch codec {
        case .mjpeg:
            body = """
            <!doctype html><meta charset=utf-8><title>Quern preview</title>
            <style>body{margin:0;background:#111;display:grid;place-items:center;
            height:100vh}img{max-height:100vh;max-width:100vw}</style>
            <img src="/stream">
            """
        case .h264:
            body = """
            <!doctype html><meta charset=utf-8><title>Quern preview</title>
            <style>body{margin:0;background:#111;color:#ccc;font:14px system-ui;
            display:grid;place-items:center;height:100vh;text-align:center}
            code{color:#8bf}</style>
            <div><p>This stream is raw H.264, which a browser cannot play directly.</p>
            <p><code>ffplay -fflags nobuffer http://127.0.0.1:PORT/stream</code></p></div>
            """
        }
        return body
    }
}
