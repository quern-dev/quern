// Reads Quern's on-disk state from ~/.quern and notifies on change.
//
// The menu bar deliberately uses the unauthenticated JSON files as its
// source of truth (no bearer token / HTTP needed):
//   • state.json        — written while the daemon runs, deleted on stop
//   • update-info.json   — the cached "update available" hint (24h refresh)
//   • active-device.json — the active device UDID
//
// Field names mirror server/lifecycle/state.py exactly.

import Foundation

struct ServerState {
    var running = false
    var pid: Int?
    var host: String?
    var port: Int?
    var proxyEnabled = false
    var proxyStatus: String?
    var proxyPort: Int?
    var startedAt: Date?
}

struct UpdateInfo {
    var updateAvailable = false
    var currentVersion: String?
    var latestVersion: String?
    var message: String?
    var channel: String?
}

struct ActiveDevice {
    var udid: String?
    var name: String?
}

struct QuernSnapshot {
    var server = ServerState()
    var update = UpdateInfo()
    var device = ActiveDevice()
}

final class StateReader {
    static let quernDir = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent(".quern", isDirectory: true)

    /// Called on the main thread whenever a fresh snapshot is read.
    var onChange: ((QuernSnapshot) -> Void)?

    private(set) var snapshot = QuernSnapshot()
    private var timer: Timer?
    private var dirSource: DispatchSourceFileSystemObject?
    private var dirFD: Int32 = -1

    func start() {
        refresh()
        // Backbone: a steady poll so we never miss a transition even if the
        // directory watcher misfires.
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        watchDirectory()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        dirSource?.cancel()
        dirSource = nil
    }

    // MARK: - Directory watch (responsiveness on top of the poll)

    private func watchDirectory() {
        let path = Self.quernDir.path
        dirFD = open(path, O_EVTONLY)
        guard dirFD >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: dirFD,
            eventMask: [.write, .rename, .delete],
            queue: .main
        )
        source.setEventHandler { [weak self] in self?.refresh() }
        source.setCancelHandler { [weak self] in
            if let fd = self?.dirFD, fd >= 0 { close(fd) }
            self?.dirFD = -1
        }
        source.resume()
        dirSource = source
    }

    // MARK: - Reading

    func refresh() {
        var snap = QuernSnapshot()
        snap.server = Self.readServerState()
        snap.update = Self.readUpdateInfo()
        snap.device = Self.readActiveDevice()
        snapshot = snap
        onChange?(snap)
    }

    private static func json(_ name: String) -> [String: Any]? {
        let url = quernDir.appendingPathComponent(name)
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj
    }

    private static func readServerState() -> ServerState {
        var s = ServerState()
        guard let d = json("state.json") else { return s }
        s.pid = d["pid"] as? Int
        s.host = d["server_host"] as? String
        s.port = d["server_port"] as? Int
        s.proxyEnabled = d["proxy_enabled"] as? Bool ?? false
        s.proxyStatus = d["proxy_status"] as? String
        s.proxyPort = d["proxy_port"] as? Int
        if let started = d["started_at"] as? String {
            s.startedAt = ISO8601DateFormatter().date(from: started)
        }
        // state.json exists only while the daemon is up, but a stale file can
        // linger after a crash — confirm the PID is actually alive.
        if let pid = s.pid, pid > 0 {
            s.running = (kill(pid_t(pid), 0) == 0) || (errno == EPERM)
        }
        return s
    }

    private static func readUpdateInfo() -> UpdateInfo {
        var u = UpdateInfo()
        guard let d = json("update-info.json") else { return u }
        u.updateAvailable = d["update_available"] as? Bool ?? false
        u.currentVersion = d["current_version"] as? String
        u.latestVersion = d["latest_version"] as? String
        u.message = d["message"] as? String
        u.channel = d["channel"] as? String
        return u
    }

    private static func readActiveDevice() -> ActiveDevice {
        var a = ActiveDevice()
        guard let d = json("active-device.json") else { return a }
        a.udid = d["udid"] as? String
        a.name = (d["name"] as? String) ?? (d["localized_name"] as? String)
        return a
    }
}
