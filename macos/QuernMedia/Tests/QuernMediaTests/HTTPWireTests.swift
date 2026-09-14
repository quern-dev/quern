import Foundation
import Testing
@testable import QuernMedia

@Test("request paths are parsed from the head", arguments: [
    ("GET /stream HTTP/1.1\r\nHost: x\r\n\r\n", "/stream"),
    ("GET / HTTP/1.1\r\n\r\n", "/"),
    ("GET /stream?fps=30 HTTP/1.1\r\n\r\n", "/stream?fps=30"),
])
func parsesPath(head: String, expected: String) {
    #expect(HTTPWire.requestPath(head) == expected)
}

@Test("a malformed request falls back to the index rather than erroring", arguments: [
    "", "garbage", "GET\r\n\r\n",
])
func malformedFallsBackToIndex(head: String) {
    // This serves a preview to a browser, not an API. Something unparseable
    // should land on the index page, not produce a diagnostic.
    #expect(HTTPWire.requestPath(head) == "/")
    #expect(!HTTPWire.isStreamPath(HTTPWire.requestPath(head)))
}

@Test("stream paths are recognised including query strings")
func recognisesStreamPaths() {
    #expect(HTTPWire.isStreamPath("/stream"))
    #expect(HTTPWire.isStreamPath("/stream?x=1"))
    #expect(!HTTPWire.isStreamPath("/"))
    #expect(!HTTPWire.isStreamPath("/streamer"), "a bare prefix test would claim this")
}

@Test("an MJPEG part is framed exactly as the multipart spec wants")
func mjpegPartFraming() throws {
    let jpeg = Data([0xFF, 0xD8, 0xFF, 0xAA, 0xBB, 0xFF, 0xD9])
    let part = HTTPWire.mjpegPart(jpeg)
    let text = String(decoding: part.prefix(80), as: UTF8.self)

    #expect(text.hasPrefix("--\(HTTPWire.mjpegBoundary)\r\n"))
    #expect(text.contains("Content-Type: image/jpeg\r\n"))
    // Content-Length must be the payload only, not the framing -- a browser
    // that trusts it and gets the wrong number desynchronises for good.
    #expect(text.contains("Content-Length: \(jpeg.count)\r\n\r\n"))
    #expect(part.suffix(2) == Data("\r\n".utf8))
}

@Test("the payload survives framing byte for byte")
func mjpegPartPreservesPayload() throws {
    let jpeg = Data((0..<512).map { UInt8($0 % 256) })
    let part = HTTPWire.mjpegPart(jpeg)
    let marker = Data("\r\n\r\n".utf8)
    let bodyStart = try #require(part.range(of: marker)).upperBound
    let body = part[bodyStart..<part.index(bodyStart, offsetBy: jpeg.count)]
    #expect(Data(body) == jpeg)
}

@Test("content types match the codec")
func contentTypes() {
    #expect(HTTPWire.contentType(for: .mjpeg).hasPrefix("multipart/x-mixed-replace"))
    #expect(HTTPWire.contentType(for: .mjpeg).contains(HTTPWire.mjpegBoundary))
    #expect(HTTPWire.contentType(for: .h264) == "video/h264")
}

@Test("the index page tells an H.264 viewer what to do instead")
func h264IndexExplainsItself() {
    // A browser cannot play a raw elementary stream, so the page should say
    // so rather than render a broken video element.
    let page = String(decoding: HTTPWire.indexPage(for: .h264), as: UTF8.self)
    #expect(page.contains("ffplay"))
    #expect(!page.contains("<img"))

    let mjpeg = String(decoding: HTTPWire.indexPage(for: .mjpeg), as: UTF8.self)
    #expect(mjpeg.contains("<img src=\"/stream\">"))
}

@Test("index responses declare an accurate Content-Length", arguments: [
    StreamPipeline.Codec.mjpeg, StreamPipeline.Codec.h264,
])
func indexContentLengthIsCorrect(codec: StreamPipeline.Codec) throws {
    let page = HTTPWire.indexPage(for: codec)
    let text = String(decoding: page, as: UTF8.self)
    let declared = try #require(
        text.split(separator: "\r\n")
            .first(where: { $0.hasPrefix("Content-Length:") })
            .flatMap { Int($0.dropFirst("Content-Length:".count).trimmingCharacters(in: .whitespaces)) }
    )
    let bodyStart = try #require(page.range(of: Data("\r\n\r\n".utf8))).upperBound
    #expect(page.distance(from: bodyStart, to: page.endIndex) == declared)
}
