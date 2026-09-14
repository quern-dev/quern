import Foundation
import Network

/// Serves encoded frames over HTTP, as a `FrameSink`.
///
/// Owns transport and nothing else. The encoder and the throttle live in
/// `StreamPipeline`, and a recorder is a peer sink — this class used to own
/// all three, which is what made "record without serving" awkward.
public final class HTTPStreamServer: FrameSink {
    private final class Client {
        let connection: NWConnection
        var streaming = false
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

    public func start() throws {
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
        listener.stateUpdateHandler = { state in
            if case .failed(let error) = state {
                MediaLog.log("[http] listener failed: \(error)")
            }
        }
        listener.start(queue: queue)
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
        clients[ObjectIdentifier(conn)] = client
        lock.unlock()

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
        readRequest(client)
    }

    private func readRequest(_ client: Client) {
        client.connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) {
            [weak self] data, _, isComplete, error in
            guard let self else { return }
            guard error == nil, !isComplete, let data,
                  let head = String(data: data, encoding: .utf8) else {
                client.connection.cancel()
                return
            }
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
