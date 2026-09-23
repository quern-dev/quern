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

/// Attaches a viewer, runs `whileAttached` once it is streaming, and returns
/// everything received until the timeout.
///
/// Unlike `rawGet` this never stops early on a byte count: the point is what
/// arrives *over time*, so it always runs the full window.
private func rawGetWhile(
    path: String, port: UInt16, timeout: TimeInterval,
    whileAttached: @escaping @Sendable () -> Void
) async -> Data {
    await withCheckedContinuation { continuation in
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
            conn.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { c, _, done, err in
                if let c { box.append(c) }
                if done || err != nil { finish() } else { readMore() }
            }
        }
        conn.stateUpdateHandler = { state in
            if case .ready = state {
                let request = "GET \(path) HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                conn.send(content: Data(request.utf8), completion: .contentProcessed { _ in })
                readMore()
                // Give route() time to mark the client streaming before the
                // caller starts pushing frames at it.
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.2) {
                    whileAttached()
                }
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

@Test("a quiet stream repeats its last frame so a viewer can tell idle from dead")
func keepaliveRepeatsTheLastFrame() async throws {
    // Both URLSession timeouts on the client are unbounded, deliberately — a
    // 15s inactivity timeout killed previews of idle simulators. That leaves
    // nothing to detect a dead peer except the peer closing the connection,
    // which a dropped network and a wedged producer never do. The keepalive
    // is what makes silence mean something.
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg, keepalive: 0.3)
    try server.start()
    defer { server.stop() }

    let surface = try #require(TestSurface.make(width: 64, height: 64))
    let encoder = JPEGEncoder(maxDimension: 0, quality: 0.5)
    let jpeg = try #require(encoder.encode(surface))

    // Attach a viewer, send exactly one frame, then go quiet.
    let received = await rawGetWhile(path: "/stream", port: port, timeout: 3) {
        server.receive(.jpeg(jpeg))
    }

    #expect(server.keepalivesSent >= 1, "a quiet stream sent no keepalive")
    let text = String(decoding: received, as: UTF8.self)
    let parts = text.components(separatedBy: "--\(HTTPWire.mjpegBoundary)").count - 1
    #expect(parts >= 2, "expected the frame plus at least one repeat, got \(parts)")
}

@Test("an active stream does not pay for the keepalive")
func keepaliveStaysOutOfTheWayWhenFramesFlow() async throws {
    let port = freePort()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg, keepalive: 0.3)
    try server.start()
    defer { server.stop() }

    let surface = try #require(TestSurface.make(width: 64, height: 64))
    let encoder = JPEGEncoder(maxDimension: 0, quality: 0.5)
    let jpeg = try #require(encoder.encode(surface))

    _ = await rawGetWhile(path: "/stream", port: port, timeout: 1.5) {
        // Keep sending faster than the keepalive interval.
        for _ in 0..<12 {
            server.receive(.jpeg(jpeg))
            Thread.sleep(forTimeInterval: 0.1)
        }
    }

    #expect(server.keepalivesSent == 0,
            "a stream with frames flowing still sent \(server.keepalivesSent) keepalives")
}

@Test("a port already taken is reported, not logged and forgotten")
func startFailsLoudlyOnABoundPort() throws {
    // "A bind failure was only logged, so a server that never came up still
    // looked started" was half the reason start() waits for .ready. Nothing
    // asserted the other half: that the caller is actually told.
    let port = freePort()
    let first = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    try first.start()
    defer { first.stop() }
    #expect(first.isListening)

    let second = HTTPStreamServer(port: port, bindAll: false, codec: .mjpeg)
    defer { second.stop() }
    #expect(throws: HTTPStreamServer.StartFailure.self) {
        try second.start()
    }
    #expect(!second.isListening)
}


/// A viewer that reads only when told to.
///
/// The helpers above read continuously, which is the one thing this cannot
/// do: the dropped-frame gate only engages against a client that has stopped
/// draining its socket.
private final class ThrottledViewer: @unchecked Sendable {
    private let conn: NWConnection
    private let lock = NSLock()
    private var draining = false

    init(port: UInt16) {
        conn = NWConnection(
            host: .ipv4(.loopback), port: NWEndpoint.Port(rawValue: port)!, using: .tcp
        )
        conn.stateUpdateHandler = { [conn] state in
            guard case .ready = state else { return }
            let request = "GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            conn.send(content: Data(request.utf8), completion: .contentProcessed { _ in })
        }
        conn.start(queue: .global())
    }

    /// Starts reading, and keeps reading.
    func drain() {
        lock.lock()
        let already = draining
        draining = true
        lock.unlock()
        guard !already else { return }
        readMore()
    }

    private func readMore() {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { [weak self] _, _, done, err in
            guard let self, !done, err == nil else { return }
            self.readMore()
        }
    }

    func stop() { conn.cancel() }
}

private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    func bump() { lock.lock(); n += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return n }
}

/// An H.264 payload of a given size. The server reads `annexB` and
/// `isKeyframe` and nothing else, so the sample buffer can be empty.
private func h264(bytes: Int, keyframe: Bool) throws -> EncodedPayload {
    var made: CMSampleBuffer?
    _ = CMSampleBufferCreate(
        allocator: kCFAllocatorDefault, dataBuffer: nil, dataReady: true,
        makeDataReadyCallback: nil, refcon: nil, formatDescription: nil,
        sampleCount: 0, sampleTimingEntryCount: 0, sampleTimingArray: nil,
        sampleSizeEntryCount: 0, sampleSizeArray: nil, sampleBufferOut: &made
    )
    let sample = try #require(made)
    return .h264(H264Output(
        frame: EncodedFrame(sample: sample, time: .zero, isKeyframe: keyframe),
        annexB: Data(repeating: 0x41, count: bytes)
    ))
}

private func waitFor(
    _ what: String, timeout: TimeInterval = 10, _ condition: () -> Bool
) async {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if condition() { return }
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    Issue.record("timed out waiting for \(what)")
}

@Test("an H.264 client that missed a frame gets nothing until the next keyframe")
func h264SkipHoldsUntilKeyframe() async throws {
    // A skipped H.264 frame is not one lost picture. Every P-frame after it
    // references a picture the client never received, so it renders a corrupt
    // image rather than a stale one -- and MaxKeyFrameInterval counts frames,
    // not seconds, so on an idle event-driven source the next IDR can be a
    // very long way off.
    let port = freePort()
    let requests = Counter()
    let server = HTTPStreamServer(port: port, bindAll: false, codec: .h264) {
        requests.bump()
    }
    try server.start()
    defer { server.stop() }

    let viewer = ThrottledViewer(port: port)
    defer { viewer.stop() }
    await waitFor("the viewer to attach") { server.wantsFrames }
    let afterAttach = requests.value

    // Fill the socket. Nothing is reading, so the send stays outstanding and
    // the next frame is skipped. Bounded rather than polled to a deadline: an
    // unbounded loop here queues megabytes a tick when the gate misbehaves.
    let big = try h264(bytes: 1 << 21, keyframe: true)
    for _ in 0..<16 where server.framesSkipped == 0 {
        server.receive(big)
        try await Task.sleep(nanoseconds: 50_000_000)
    }
    #expect(server.framesSkipped > 0, "the viewer kept draining; the gate was never exercised")
    #expect(requests.value > afterAttach, "a skip should ask the encoder for a keyframe")

    // Let it catch up. Without this the assertions below pass on the
    // in-flight gate alone and say nothing about the desync one.
    viewer.drain()
    await waitFor("the outstanding send to complete") { server.sendsInFlight == 0 }
    #expect(server.sendsInFlight == 0, "the viewer never caught up")

    let baseline = server.bytesSent
    server.receive(try h264(bytes: 1024, keyframe: false))
    #expect(server.bytesSent == baseline, "a desynced client was sent an undecodable P-frame")
    #expect(server.keyframeResyncs == 0)
    #expect(server.framesHeldForKeyframe == 1, "the withheld frame was not reported")

    server.receive(try h264(bytes: 1024, keyframe: true))
    #expect(server.bytesSent > baseline, "a keyframe should have resynced the client")
    #expect(server.keyframeResyncs == 1)
}
