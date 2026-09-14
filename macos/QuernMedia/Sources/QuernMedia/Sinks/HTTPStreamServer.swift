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

        init(_ connection: NWConnection) { self.connection = connection }
    }

    private let port: NWEndpoint.Port
    private let bindAll: Bool
    private let codec: StreamPipeline.Codec
    private let onClientAttached: (() -> Void)?

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

    public private(set) var framesSent = 0
    public private(set) var framesSkipped = 0
    public private(set) var bytesSent = 0

    /// - Parameters:
    ///   - bindAll: listen on every interface instead of loopback. The stream
    ///     is **unauthenticated**, so this is opt-in and loopback is default.
    ///   - onClientAttached: wire this to `StreamPipeline.requestKeyframe()`.
    ///     H.264 frames depend on earlier ones, so a viewer arriving mid-stream
    ///     decodes nothing until an IDR.
    public init(
        port: UInt16,
        bindAll: Bool,
        codec: StreamPipeline.Codec,
        onClientAttached: (() -> Void)? = nil
    ) {
        self.port = NWEndpoint.Port(rawValue: port) ?? 8422
        self.bindAll = bindAll
        self.codec = codec
        self.onClientAttached = onClientAttached
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
        let all = Array(clients.values)
        clients.removeAll()
        lock.unlock()
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
        let targets = clients.values.filter { $0.streaming && !$0.inFlight }
        let skipped = clients.values.filter { $0.streaming && $0.inFlight }.count
        for client in targets { client.inFlight = true }
        framesSent += targets.isEmpty ? 0 : 1
        framesSkipped += skipped
        bytesSent += bytes.count * targets.count
        lock.unlock()

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
            self.route(client, path: HTTPWire.requestPath(head))
        }
    }

    private func route(_ client: Client, path: String) {
        guard HTTPWire.isStreamPath(path) else {
            client.connection.send(
                content: HTTPWire.indexPage(for: codec),
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

        MediaLog.log("[http] viewer attached (\(total) total)")
        // Ask for a keyframe now rather than making this viewer wait for the
        // periodic one.
        onClientAttached?()
    }
}
