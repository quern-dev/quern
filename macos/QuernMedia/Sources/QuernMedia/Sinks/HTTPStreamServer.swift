import Foundation
import Network

/// Serves encoded frames over HTTP, as a `FrameSink`.
///
/// Owns transport and nothing else. The encoder and the throttle live in
/// `StreamPipeline`, and a recorder is a peer sink — this class used to own
/// all three, which is what made "record without serving" awkward.
public final class HTTPStreamServer: FrameSink {
    public enum StartFailure: Error, CustomStringConvertible {
        case listenerFailed(String)
        case notReady(TimeInterval)

        public var description: String {
            switch self {
            case .listenerFailed(let m): return "listener failed: \(m)"
            case .notReady(let t): return "listener was not ready within \(t)s"
            }
        }
    }

    /// Carries a listener failure out of the state handler, which runs on the
    /// listener's queue while `start()` waits on the caller's.
    private final class Outcome: @unchecked Sendable {
        private let lock = NSLock()
        private var stored: StartFailure?
        var error: StartFailure? {
            get { lock.lock(); defer { lock.unlock() }; return stored }
            set { lock.lock(); defer { lock.unlock() }; stored = newValue }
        }
    }

    /// Caps for an unauthenticated server. Reachable from the network when
    /// `--bind-all` is set, where a peer that connects and never speaks would
    /// otherwise hold a slot indefinitely.
    private static let maxClients = 32
    private static let maxHeadBytes = 8192
    private static let headTimeout: TimeInterval = 10

    private final class Client {
        let connection: NWConnection
        var streaming = false
        /// Request bytes so far. Touched only on the connection queue.
        var head = Data()
        /// Dropped-frame gate. A viewer on wifi cannot absorb 60 fps of JPEG,
        /// and queueing what it cannot take turns "slow" into "minutes
        /// behind". One frame in flight; newer frames are skipped, not queued.
        var inFlight = false

        /// Set when the gate skipped a frame this client needed.
        ///
        /// MJPEG never sets it -- every JPEG decodes alone, so a skip costs one
        /// frame. In H.264 it costs everything up to the next IDR: each later
        /// P-frame references a picture this client never received, so it
        /// renders a corrupt image rather than a stale one. `MaxKeyFrameInterval`
        /// counts frames, not seconds, so on an idle event-driven source the
        /// next IDR can be minutes away. Sends are held back until one arrives.
        var desynced = false

        init(_ connection: NWConnection) { self.connection = connection }
    }

    private let port: NWEndpoint.Port
    private let bindAll: Bool
    private let codec: StreamPipeline.Codec
    private let onKeyframeNeeded: (() -> Void)?

    private var listener: NWListener?
    private let queue = DispatchQueue(label: "quern.media.http")
    private let lock = NSLock()
    private var clients: [ObjectIdentifier: Client] = [:]

    /// Whether the listener has reached `.ready`.
    ///
    /// The postcondition of `start()`, and the thing a test can pin: timing a
    /// connect only reproduces the race on a machine slow enough to lose it.
    public var isListening: Bool {
        guard let listener else { return false }
        if case .ready = listener.state { return true }
        return false
    }

    private let keepalive: TimeInterval
    private var keepaliveTimer: DispatchSourceTimer?
    /// The last MJPEG part written, replayed when the stream goes quiet.
    private var lastPart: Data?
    private var lastSendAt = Date.distantPast

    /// Control requests served. Counted so a test asserts the endpoint did
    /// something, rather than asserting nothing went wrong.
    public var keyframeRequests: Int { lock.lock(); defer { lock.unlock() }; return _keyframeRequests }
    private var _keyframeRequests = 0

    /// How many times a frame has been repeated to keep the stream alive.
    /// Counted so a test can assert it happened, rather than asserting that
    /// nothing went wrong.
    public var keepalivesSent: Int { lock.lock(); defer { lock.unlock() }; return _keepalivesSent }
    private var _keepalivesSent = 0

    /// Clients an IDR has resynced after the gate skipped a frame. Counted so
    /// a test asserts the recovery happened, not merely that nothing crashed.
    public var keyframeResyncs: Int { lock.lock(); defer { lock.unlock() }; return _keyframeResyncs }
    private var _keyframeResyncs = 0

    /// Frames withheld from a desynced client while it waits for an IDR.
    ///
    /// Reported because the state is otherwise invisible: a client whose
    /// keyframe never arrives sits frozen while `framesSent` climbs, which
    /// reads as a healthy stream. Rising here with `keyframeResyncs` flat is
    /// an encoder that is not honouring the request.
    public var framesHeldForKeyframe: Int { lock.lock(); defer { lock.unlock() }; return _framesHeldForKeyframe }
    private var _framesHeldForKeyframe = 0

    /// Clients with a send outstanding. Internal, for tests only.
    ///
    /// Both gates below suppress a send, so a test that cannot tell "still
    /// draining the last frame" from "waiting for an IDR" passes just as
    /// happily against a desync gate that does nothing.
    var sendsInFlight: Int {
        lock.lock()
        defer { lock.unlock() }
        return clients.values.filter(\.inFlight).count
    }

    public var framesSent: Int { lock.lock(); defer { lock.unlock() }; return _framesSent }
    private var _framesSent = 0
    public var framesSkipped: Int { lock.lock(); defer { lock.unlock() }; return _framesSkipped }
    private var _framesSkipped = 0
    public var bytesSent: Int { lock.lock(); defer { lock.unlock() }; return _bytesSent }
    private var _bytesSent = 0

    /// - Parameters:
    ///   - bindAll: listen on every interface instead of loopback. The stream
    ///     is **unauthenticated**, so this is opt-in and loopback is default.
    ///   - onKeyframeNeeded: wire this to `StreamPipeline.requestKeyframe()`.
    ///     H.264 frames depend on earlier ones, so a client with no recent IDR
    ///     decodes nothing. Two things put a client in that state: attaching
    ///     mid-stream, and being skipped by the dropped-frame gate below.
    /// - Parameter keepalive: how long the server may go without sending
    ///   before it repeats its last frame to every viewer.
    ///
    ///   A viewer cannot otherwise tell an idle screen from a dead server.
    ///   Both URLSession timeouts on the client are unbounded, deliberately —
    ///   a 15s inactivity timeout killed previews of idle simulators — so the
    ///   only thing left that detects a dead peer is the peer closing the
    ///   connection. A dropped network or a wedged producer never does that,
    ///   and the window sits on its last frame while the server reports the
    ///   preview as live.
    ///
    ///   MJPEG only. Every JPEG stands alone, so repeating one is valid and a
    ///   browser redraws the same picture. An H.264 stream cannot have frames
    ///   replayed into it, and its consumers are ffplay and the recorder
    ///   rather than the preview window, so they are left alone.
    public init(
        port: UInt16,
        bindAll: Bool,
        codec: StreamPipeline.Codec,
        keepalive: TimeInterval = 5,
        onKeyframeNeeded: (() -> Void)? = nil
    ) {
        self.port = NWEndpoint.Port(rawValue: port) ?? 8422
        self.bindAll = bindAll
        self.codec = codec
        self.keepalive = keepalive
        self.onKeyframeNeeded = onKeyframeNeeded
    }

    public func start(timeout: TimeInterval = 5) throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true

        // requiredLocalEndpoint pins the bind address, and it is mutually
        // exclusive with NWListener's `on:` argument — setting both is EINVAL
        // rather than a narrower bind.
        let listener: NWListener
        if bindAll {
            listener = try NWListener(using: params, on: port)
        } else {
            params.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: port)
            listener = try NWListener(using: params)
        }
        listener.newConnectionHandler = { [weak self] conn in self?.accept(conn) }

        // `listener.start` is asynchronous, so returning as soon as it is
        // called means "asked to listen", not "listening" -- a caller that
        // connects immediately races the bind and reads a closed socket. A
        // bind failure was only logged, too, so a server that never came up
        // still looked started. Waiting for .ready makes the throw the
        // caller's answer to both.
        let ready = DispatchSemaphore(value: 0)
        let outcome = Outcome()
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready:
                ready.signal()
            case .failed(let error):
                MediaLog.log("[http] listener failed: \(error)")
                outcome.error = .listenerFailed("\(error)")
                ready.signal()
            case .cancelled:
                outcome.error = .listenerFailed("cancelled before ready")
                ready.signal()
            default:
                break
            }
        }
        listener.start(queue: queue)

        if ready.wait(timeout: .now() + timeout) == .timedOut {
            listener.cancel()
            throw StartFailure.notReady(timeout)
        }
        if let error = outcome.error {
            listener.cancel()
            throw error
        }

        self.listener = listener

        let host = bindAll ? "0.0.0.0" : "127.0.0.1"
        MediaLog.log("[http] serving \(codec) on http://\(host):\(port.rawValue)/")
        if bindAll {
            MediaLog.log("[http] WARNING: all interfaces, no authentication — "
                + "anyone on this network can watch the screen")
        }
    }

    public func stop() {
        listener?.cancel()
        listener = nil
        lock.lock()
        let timer = keepaliveTimer
        keepaliveTimer = nil
        let all = Array(clients.values)
        clients.removeAll()
        lock.unlock()
        // Cancelled outside the lock: the handler takes it, and a timer
        // cancelled while its handler is mid-flight would otherwise deadlock.
        timer?.cancel()
        for client in all { client.connection.cancel() }
    }

    // MARK: - FrameSink

    public var wantsFrames: Bool {
        lock.lock()
        defer { lock.unlock() }
        return clients.values.contains { $0.streaming }
    }

    public func receive(_ payload: EncodedPayload) {
        let bytes: Data
        switch (codec, payload) {
        case (.mjpeg, .jpeg(let jpeg)):
            bytes = HTTPWire.mjpegPart(jpeg)
        case (.h264, .h264(let out)):
            // An elementary stream needs no envelope: the NAL start codes are
            // the framing.
            bytes = out.annexB
        default:
            return  // codec mismatch: the pipeline was configured differently
        }

        lock.lock()
        let watching = clients.values.filter(\.streaming)
        let stalled = watching.filter(\.inFlight)

        // A skip is what breaks the reference chain, so the mark goes on here
        // rather than where the send is suppressed.
        if codec == .h264 {
            for client in stalled { client.desynced = true }
        }

        let targets = watching.filter { client in
            guard !client.inFlight else { return false }
            return !client.desynced || payload.isKeyframe
        }
        let resynced = targets.filter(\.desynced).count
        let held = watching.filter { $0.desynced && !$0.inFlight }.count - resynced
        for client in targets {
            client.inFlight = true
            client.desynced = false
        }
        _framesSent += targets.isEmpty ? 0 : 1
        _framesSkipped += stalled.count
        _keyframeResyncs += resynced
        _framesHeldForKeyframe += held
        _bytesSent += bytes.count * targets.count
        if codec == .mjpeg { lastPart = bytes }
        lastSendAt = Date()
        let wantKeyframe = watching.contains(where: \.desynced)
        lock.unlock()

        // Outside the lock: the handler calls back into the pipeline, which
        // is the deadlock the rest of this file is careful to avoid.
        if wantKeyframe { onKeyframeNeeded?() }

        for client in targets {
            client.connection.send(content: bytes, completion: .contentProcessed {
                [weak self, weak client] _ in
                guard let self, let client else { return }
                self.lock.lock()
                client.inFlight = false
                self.lock.unlock()
            })
        }
    }

    // MARK: - connections

    private func accept(_ conn: NWConnection) {
        let client = Client(conn)
        lock.lock()
        let atCapacity = clients.count >= Self.maxClients
        if !atCapacity { clients[ObjectIdentifier(conn)] = client }
        lock.unlock()

        guard !atCapacity else {
            MediaLog.log("[http] refusing a connection: \(Self.maxClients) already open")
            conn.cancel()
            return
        }

        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .failed, .cancelled:
                guard let self else { return }
                self.lock.lock()
                self.clients.removeValue(forKey: ObjectIdentifier(conn))
                self.lock.unlock()
            default:
                break
            }
        }
        conn.start(queue: queue)

        // A connection that never sends a request head is dropped. Without
        // this it sits in `clients` forever, and enough of them exhaust the
        // cap above and lock out real viewers.
        queue.asyncAfter(deadline: .now() + Self.headTimeout) { [weak client] in
            guard let client, !client.streaming else { return }
            MediaLog.log("[http] dropping a client that sent no request")
            client.connection.cancel()
        }

        readRequest(client)
    }

    /// Repeats the last frame when the stream has gone quiet.
    ///
    /// Runs only while someone is watching, and only sends when nothing else
    /// has for `keepalive` — so an active stream pays nothing, and an idle one
    /// costs one already-encoded frame every few seconds.
    private func startKeepaliveIfNeeded() {
        lock.lock()
        defer { lock.unlock() }
        guard keepaliveTimer == nil, keepalive > 0, codec == .mjpeg else { return }

        let timer = DispatchSource.makeTimerSource(queue: queue)
        // Checked more often than the interval so the gap between a quiet
        // stream and the repeat is bounded by the interval, not twice it.
        timer.schedule(deadline: .now() + keepalive / 2, repeating: keepalive / 2)
        timer.setEventHandler { [weak self] in self?.sendKeepaliveIfQuiet() }
        keepaliveTimer = timer
        timer.resume()
    }

    private func sendKeepaliveIfQuiet() {
        lock.lock()
        let quiet = Date().timeIntervalSince(lastSendAt) >= keepalive
        let watchers = clients.values.filter { $0.streaming && !$0.inFlight }
        guard quiet, !watchers.isEmpty, let part = lastPart else {
            lock.unlock()
            return
        }
        for client in watchers { client.inFlight = true }
        _keepalivesSent += 1
        _bytesSent += part.count * watchers.count
        lastSendAt = Date()
        lock.unlock()

        for client in watchers {
            client.connection.send(content: part, completion: .contentProcessed {
                [weak self, weak client] _ in
                guard let self, let client else { return }
                self.lock.lock()
                client.inFlight = false
                self.lock.unlock()
            })
        }
    }

    private func readRequest(_ client: Client) {
        client.connection.receive(
            minimumIncompleteLength: 1, maximumLength: Self.maxHeadBytes
        ) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            guard error == nil, !isComplete, let data, !data.isEmpty else {
                client.connection.cancel()
                return
            }

            client.head.append(data)

            // One receive is not one request. TCP is free to split "GET
            // /stream" across segments, and routing on the first segment sent
            // a stream request the index page and then closed the connection.
            guard let end = client.head.range(of: Data("\r\n\r\n".utf8)) else {
                guard client.head.count < Self.maxHeadBytes else {
                    MediaLog.log("[http] request head over \(Self.maxHeadBytes) bytes")
                    client.connection.cancel()
                    return
                }
                self.readRequest(client)
                return
            }

            let head = String(decoding: client.head[..<end.lowerBound], as: UTF8.self)
            client.head = Data()
            self.route(
                client,
                method: HTTPWire.requestMethod(head),
                path: HTTPWire.requestPath(head)
            )
        }
    }

    private func route(_ client: Client, method: String, path: String) {
        // Checked before the stream path, and strictly. A GET here is a
        // mistake worth reporting rather than quietly serving the index page
        // the way an unrecognised path does.
        // A mistyped control path is refused, never answered with the index
        // page. Falling through gave it 200 and an HTML body, which to a
        // caller of a 204 endpoint is indistinguishable from success.
        if HTTPWire.keyframePathMatch(path) == .nearMiss {
            client.connection.send(
                content: HTTPWire.notFoundResponse(),
                completion: .contentProcessed { _ in client.connection.cancel() }
            )
            return
        }

        if HTTPWire.isKeyframePath(path) {
            guard method == "POST" else {
                client.connection.send(
                    content: HTTPWire.methodNotAllowedResponse(),
                    completion: .contentProcessed { _ in client.connection.cancel() }
                )
                return
            }
            // Nothing to ask. The CLI always wires this up, but the
            // initialiser makes it optional, so a caller embedding the server
            // can reach here with no encoder behind it -- and answering 204
            // for a request that did no work is the same false success the
            // near-miss path above exists to prevent, on the happy path.
            guard let onKeyframeNeeded else {
                client.connection.send(
                    content: HTTPWire.noEncoderResponse(),
                    completion: .contentProcessed { _ in client.connection.cancel() }
                )
                return
            }

            lock.lock()
            _keyframeRequests += 1
            let count = _keyframeRequests
            lock.unlock()
            // The same hook a viewer attaching and a desync skip both fire.
            // All three mean one thing to the pipeline -- it owes us an IDR --
            // so this route reuses it rather than adding a second callback
            // wired to the same closure. That also keeps the initialiser to a
            // single closure parameter, which is what makes the trailing-
            // closure form at the call sites unambiguous.
            onKeyframeNeeded()
            MediaLog.log("[http] keyframe requested (\(count) total)")
            client.connection.send(
                content: HTTPWire.noContentResponse(),
                completion: .contentProcessed { _ in client.connection.cancel() }
            )
            return
        }

        guard HTTPWire.isStreamPath(path) else {
            // The index page is a page, so it answers the methods a browser
            // uses and nothing else. `POST /anything` used to get 200 and an
            // HTML body -- harmless for a person typing a URL, and for a
            // caller driving this as an API the same false success the
            // control path above just had to be fixed for.
            guard method == "GET" || method == "HEAD" else {
                client.connection.send(
                    content: HTTPWire.notFoundResponse(),
                    completion: .contentProcessed { _ in client.connection.cancel() }
                )
                return
            }
            // HEAD carries what GET would, minus the body. Handing it the
            // whole page put the body on the wire under a correct
            // Content-Length, which is a malformed response, not a harmless
            // extra.
            let page = method == "HEAD"
                ? HTTPWire.indexHeaders(for: codec)
                : HTTPWire.indexPage(for: codec)
            client.connection.send(
                content: page,
                completion: .contentProcessed { _ in client.connection.cancel() }
            )
            return
        }

        client.connection.send(
            content: HTTPWire.streamHeader(contentType: HTTPWire.contentType(for: codec)),
            completion: .contentProcessed { _ in }
        )
        lock.lock()
        client.streaming = true
        let total = clients.values.filter(\.streaming).count
        lock.unlock()

        startKeepaliveIfNeeded()
        MediaLog.log("[http] viewer attached (\(total) total)")
        // Ask for a keyframe now rather than making this viewer wait for the
        // periodic one.
        onKeyframeNeeded?()
    }
}
