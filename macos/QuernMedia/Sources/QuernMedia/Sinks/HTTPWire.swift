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

    public static func indexPage(for codec: StreamPipeline.Codec) -> Data {
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
        return Data("""
        HTTP/1.1 200 OK\r
        Content-Type: text/html; charset=utf-8\r
        Content-Length: \(body.utf8.count)\r
        Connection: close\r
        \r
        \(body)
        """.utf8)
    }
}
