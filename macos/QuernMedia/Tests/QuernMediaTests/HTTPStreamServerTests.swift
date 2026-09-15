import CoreMedia
import Foundation
import Network
import Testing
@testable import QuernMedia

/// A port nothing on this host is using.
///
/// Six tests bind one of these. Picking at random and hoping meant a
/// collision with anything already listening made `start()` throw, and the
/// test then failed for a reason with nothing to do with what it was testing.
private func freePort() -> UInt16 {
    for _ in 0..<64 {
        let candidate = UInt16.random(in: 42_000...46_000)
        if portIsFree(candidate) { return candidate }
    }
    return UInt16.random(in: 42_000...46_000)
}

private func portIsFree(_ port: UInt16) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }

    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    // Deliberately no SO_REUSEADDR: the question is whether this port is
    // usable right now, and reuse would answer yes for one in TIME_WAIT.
    let bound = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return bound == 0
}

private func captured(_ surface: IOSurface, frame i: Int) -> CapturedFrame {
    CapturedFrame(
        surface: surface,
        time: CMTime(value: CMTimeValue(i * 10), timescale: 600),
        timeAccuracy: .reported
    )
}

/// Reads raw bytes off the socket, speaking HTTP by hand.
///
/// Not URLSession: it understands `multipart/x-mixed-replace` and hands back
/// only the decoded part bodies, so the framing this is meant to verify never
/// reaches the caller. A raw connection sees what actually goes over the wire.
private func rawGet(
    path: String, port: UInt16, limit: Int, timeout: TimeInterval = 5
) async -> Data {
    await withCheckedContinuation { continuation in
        let conn = NWConnection(
            host: .ipv4(.loopback),
            port: NWEndpoint.Port(rawValue: port)!,
            using: .tcp
        )
        let box = Box()
        // The receive callback and the timeout below both run on the global
        // queue and both call this. Unsynchronized, each could see `done` as
        // false and resume the same continuation -- which traps the whole
        // test process, not just this test.
        let finish = {
            guard let data = box.claim() else { return }
            conn.cancel()
            continuation.resume(returning: data)
        }

        func readMore() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 16_384) { chunk, _, complete, error in
                if let chunk { box.append(chunk) }
                if box.count >= limit || complete || error != nil {
                    finish()
                } else {
                    readMore()
                }
            }
        }

        conn.stateUpdateHandler = { state in
            if case .ready = state {
                let request = "GET \(path) HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                conn.send(content: Data(request.utf8), completion: .contentProcessed { _ in })
                readMore()
            }
            if case .failed = state { finish() }
        }
        conn.start(queue: .global())
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { finish() }
    }
}

private final class Box: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = Data()
    private var finished = false

    func append(_ chunk: Data) { lock.lock(); storage.append(chunk); lock.unlock() }
    var count: Int { lock.lock(); defer { lock.unlock() }; return storage.count }

    /// Hands the data over exactly once; nil to every later caller. The
    /// single-claim rule is what makes a double resume impossible.
    func claim() -> Data? {
        lock.lock()
        defer { lock.unlock() }
        if finished { return nil }
        finished = true
        return storage
    }
}

@Test("the index page is served to a browser")
func servesIndex() async throws {
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    try server.start()
    defer { server.stop() }

    let page = await rawGet(path: "/", port: port, limit: 4096, timeout: 20)
    let text = String(decoding: page, as: UTF8.self)
    #expect(text.contains("<img src=\"/stream\">"), "got: \(text.prefix(120))")
}

@Test("no viewer means the sink declines frames")
func declinesFramesWithNoViewer() throws {
    // What lets the pipeline skip encoding entirely when nobody is watching.
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    try server.start()
    defer { server.stop() }
    #expect(server.wantsFrames == false)
}

@Test("an attached viewer receives real MJPEG frames")
func streamsMJPEGEndToEnd() async throws {
    let port = freePort()
    var keyframeRequests = 0
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg) {
        keyframeRequests += 1
    }
    try server.start()
    defer { server.stop() }

    let surface = try #require(TestSurface.make(width: 160, height: 120))
    let pipeline = StreamPipeline(codec: .mjpeg, fps: 60, maxDimension: 0)
    defer { pipeline.invalidate() }
    pipeline.add(server)

    // Feed frames continuously while a client reads, since the sink only
    // accepts frames once a viewer has actually attached.
    let feeder = Task.detached {
        for i in 0..<400 {
            pipeline.consume(captured(surface, frame: i))
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
    }
    defer { feeder.cancel() }

    let received = await rawGet(path: "/stream", port: port, limit: 40_000, timeout: 30)
    feeder.cancel()

    let text = String(decoding: received.prefix(200), as: UTF8.self)
    #expect(text.contains("--\(HTTPWire.mjpegBoundary)"), "no multipart boundary in the stream")
    #expect(text.contains("Content-Type: image/jpeg"))
    // A JPEG start-of-image marker proves real pixels went down the socket,
    // not just headers.
    #expect(received.range(of: Data([0xFF, 0xD8, 0xFF])) != nil, "no JPEG payload")
    #expect(keyframeRequests >= 1, "attaching a viewer should ask for a keyframe")
}

@Test("codec mismatch is ignored rather than sent as garbage")
func ignoresMismatchedPayloads() throws {
    // An H.264 server handed a JPEG payload should drop it. Writing the wrong
    // codec into a stream a decoder is already parsing corrupts it for good.
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .h264)
    try server.start()
    defer { server.stop() }
    server.receive(.jpeg(Data([0xFF, 0xD8, 0xFF])))
    #expect(server.bytesSent == 0)
}

@Test("stopping twice is safe")
func stopIsIdempotent() throws {
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    try server.start()
    server.stop()
    server.stop()
}


@Test("start does not return until the port is actually accepting")
func startMeansListening() async throws {
    // NWListener.start is asynchronous. Returning before .ready made every
    // caller race the bind, which a developer machine wins and a loaded CI
    // runner does not -- it read an empty response from a socket nothing was
    // on yet. No sleep here on purpose: the connect is the assertion.
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    try server.start()
    defer { server.stop() }

    // Asserted on the listener's own state rather than on a timed connect:
    // a connect only fails where the race is lost, so on a fast machine it
    // passes just as happily against the bug.
    #expect(server.isListening, "start() returned before the listener was ready")

    let page = await rawGet(path: "/", port: port, limit: 4096, timeout: 20)
    #expect(!page.isEmpty, "start() returned before the listener was accepting")
}


@Test("a request head split across packets still routes to the stream")
func splitRequestHeadIsReassembled() async throws {
    // One receive is not one request: TCP may deliver "GET /stream" in
    // pieces, and routing on the first piece served the index page and closed
    // the connection -- a viewer that asked for video got HTML.
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    try server.start()
    defer { server.stop() }

    let received: Data = await withCheckedContinuation { continuation in
        let conn = NWConnection(
            host: .ipv4(.loopback), port: NWEndpoint.Port(rawValue: port)!, using: .tcp
        )
        let box = Box()
        let finish = {
            guard let data = box.claim() else { return }
            conn.cancel()
            continuation.resume(returning: data)
        }
        func readMore() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 8192) { chunk, _, done, err in
                if let chunk { box.append(chunk) }
                // The response header alone clears this. Anything larger
                // waits for the fallback below, because a stream with no
                // source attached sends nothing after its header.
                if box.count >= 64 || done || err != nil { finish() } else { readMore() }
            }
        }
        conn.stateUpdateHandler = { state in
            if case .ready = state {
                // Deliberately split mid-path, with a gap between the halves.
                conn.send(content: Data("GET /str".utf8), completion: .contentProcessed { _ in
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.2) {
                        let rest = "eam HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                        conn.send(content: Data(rest.utf8), completion: .contentProcessed { _ in })
                        readMore()
                    }
                })
            }
            if case .failed = state { finish() }
        }
        conn.start(queue: .global())
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) { finish() }
    }

    let text = String(decoding: received, as: UTF8.self)
    #expect(text.contains("multipart/x-mixed-replace"),
            "a split head was routed somewhere other than the stream: \(text.prefix(120))")
}
