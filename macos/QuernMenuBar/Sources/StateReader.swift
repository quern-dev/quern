// Reads Quern's on-disk state from ~/.quern and notifies on change.
//
// The menu bar deliberately uses the unauthenticated JSON files as its
// source of truth (no bearer token / HTTP needed):
//   • state.json        — written while the daemon runs, deleted on stop
//   • update-info.json   — the cached "update available" hint (24h refresh)
//   • active-device.json — the active device UDID and name
//   • config.json        — user preferences; the update channel lives here
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

struct ProxyPolicy {
    var autoInstallCert = false
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
    /// DeviceType from server/models.py: "simulator", "device",
    /// "android_emulator", "android_device". Absent when the server has not
    /// cached a type for this UDID, or when it predates the field.
    var kind: String?
}

struct QuernSnapshot {
    var server = ServerState()
    var update = UpdateInfo()
    var device = ActiveDevice()
    var proxy = ProxyPolicy()
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
        snap.proxy = ProxyPolicy(autoInstallCert: Self.readAutoInstallCert())
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
            s.startedAt = Self.parseISO8601(started)
        }
        // state.json exists only while the daemon is up, but a stale file can
        // linger after a crash — confirm the PID is actually alive.
        if let pid = s.pid, pid > 0 {
            s.running = (kill(pid_t(pid), 0) == 0) || (errno == EPERM)
        }
        return s
    }


    /// Parse an ISO-8601 timestamp with or without fractional seconds.
    ///
    /// A bare `ISO8601DateFormatter()` rejects fractional seconds, and quern
    /// writes them: `2026-09-09T00:21:34.042778+00:00`. So `startedAt` was
    /// always nil and uptime silently never appeared — the menu header already
    /// asked for it. Enabling `.withFractionalSeconds` alone would invert the
    /// bug, since that variant rejects timestamps *without* them, so both are
    /// tried.
    static func parseISO8601(_ value: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = withFraction.date(from: value) { return d }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: value)
    }

    private static func readUpdateInfo() -> UpdateInfo {
        var u = UpdateInfo()
        u.channel = readChannel()
        guard let d = json("update-info.json") else { return u }
        u.updateAvailable = d["update_available"] as? Bool ?? false
        u.currentVersion = d["current_version"] as? String
        u.latestVersion = d["latest_version"] as? String
        u.message = d["message"] as? String
        return u
    }

    /// The update channel, read from config.json — where the server keeps it.
    ///
    /// Not from update-info.json, which only caches the *result* of a check.
    /// That file is deleted when the channel changes (the cached verdict was
    /// measured against the old channel's pointer branch and would be wrong
    /// for a day), so reading the channel from it showed the default right
    /// after a switch — exactly when the user was looking.
    ///
    /// Mirrors `get_update_channel()`: an unrecognised value falls back to the
    /// default rather than being displayed, so a hand-edited typo in
    /// config.json cannot put the picker in a state the user can't get out of.
    static let validChannels = ["stable", "beta"]
    static let defaultChannel = "stable"

    /// Whether Quern installs the mitmproxy CA by itself when capture needs
    /// it. Read from config.json rather than the server, so it is correct
    /// even when the daemon is stopped -- the same reason the channel is.
    ///
    /// Anything other than a real boolean reads as off. A malformed config
    /// should mean "ask me", never consent to installing a root CA.
    private static func readAutoInstallCert() -> Bool {
        guard let d = json("config.json"), let raw = d["auto_install_cert"] else {
            return false
        }
        // A literal JSON `true`, not merely something truthy.
        //
        // JSONSerialization hands back NSNumber for both booleans and numbers,
        // and `as? Bool` accepts a numeric 1 -- measured: `{"x": 1}` reads as
        // true. The server requires a real boolean (`is True` in
        // server/config.py), so without this check a config holding 1 would
        // show the policy enabled here while the server refused capture,
        // which is a worse failure than either behaviour alone.
        guard CFGetTypeID(raw as CFTypeRef) == CFBooleanGetTypeID() else { return false }
        return (raw as? Bool) == true
    }

    private static func readChannel() -> String {
        guard let d = json("config.json"),
              let raw = d["update_channel"] as? String,
              validChannels.contains(raw)
        else { return defaultChannel }
        return raw
    }

    private static func readActiveDevice() -> ActiveDevice {
        var a = ActiveDevice()
        guard let d = json("active-device.json") else { return a }
        a.udid = d["udid"] as? String
        a.name = (d["name"] as? String) ?? (d["localized_name"] as? String)
        a.kind = d["type"] as? String
        return a
    }
}
