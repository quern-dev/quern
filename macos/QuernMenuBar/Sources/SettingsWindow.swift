// Settings window — surfaces full local state and the few user controls
// (update channel, launch-at-login, docs link). Built with SwiftUI hosted in
// a plain NSWindow so it works under a bare `swiftc` build (no SwiftPM).

import AppKit
import SwiftUI
import ServiceManagement

/// Observable model the SwiftUI view binds to. The AppDelegate pushes fresh
/// snapshots in as state changes.
final class SettingsModel: ObservableObject {
    @Published var snapshot = QuernSnapshot()
    @Published var loginEnabled = LoginItem.isEnabled()
    @Published var startOnLaunch = StartOnLaunch.isEnabled
    @Published var channel: String = "stable"
    @Published var autoInstallCert: Bool = false
    /// What the version row knows, which is not the same as what it can show.
    ///
    /// Three states, because two were not enough. The row used to fall back to
    /// `current_version` from update-info.json whenever the live read failed --
    /// a value only the *server* ever rewrites, and only when it runs an update
    /// check. On a machine where the CLI could not be found it therefore showed
    /// a version from days earlier, under a heading that says "Server", with
    /// nothing marking it as a cache. It read 0.15.0 for an 0.16.1 install and
    /// was believed, which is the whole problem with a stale value that looks
    /// live.
    enum VersionReading: Equatable {
        /// Not asked yet. The read is a subprocess, so there is a real moment
        /// before the answer arrives, and it should not look like an answer.
        case pending
        case live(String)
        /// Asked, could not tell. Distinct from pending, and from any cache.
        case unavailable

        var display: String {
            switch self {
            case .pending: return "checking…"
            case .live(let version): return version
            case .unavailable: return "unavailable"
            }
        }
    }

    /// Asked for when the window opens rather than read during `body`: the
    /// answer comes from a subprocess, and SwiftUI re-evaluates `body` often
    /// enough that doing it there would spawn one per redraw.
    @Published var version: VersionReading = .pending

    /// How the version is read. Injected for the same reason `Updater` injects
    /// its own: without a seam here the only production caller of
    /// `apply(version:)` was untested, and reinstating a `guard let version
    /// else { return }` inside this function left all 27 tests green -- which
    /// makes `.unavailable` unreachable and parks the row on "checking…"
    /// forever, for exactly the user this was written for.
    var readVersion: (@escaping (String?, String) -> Void) -> Void = Updater.installedVersion

    func refreshInstalledVersion() {
        readVersion { [weak self] version, _ in
            self?.apply(version: version)
        }
    }

    /// The Server section, as label/value pairs.
    ///
    /// Built here rather than inline in `body` so a test can read it. The
    /// defect this file is about was a fallback written into the view -- and a
    /// test of the model alone still passed with it reinstated, measured. The
    /// rule it enforces is that nothing but a live reading may appear in the
    /// version row: update-info.json answers a different question at a
    /// different time, and showing it under "Server" was believed.
    func serverRows(now: Date) -> [(String, String)] {
        let s = snapshot.server
        return [
            ("Status", s.running ? "Running" : "Stopped"),
            ("Address", s.host != nil ? "\(s.host!):\(s.port ?? 0)" : "—"),
            ("Version", version.display),
            ("Uptime", Self.uptime(since: s.startedAt, now: now)),
        ]
    }

    static func uptime(since: Date?, now: Date) -> String {
        guard let since else { return "—" }
        let secs = Int(now.timeIntervalSince(since))
        if secs < 60 { return "\(secs)s" }
        if secs < 3600 { return "\(secs / 60)m" }
        if secs < 86400 { return "\(secs / 3600)h \((secs % 3600) / 60)m" }
        return "\(secs / 86400)d \((secs % 86400) / 3600)h"
    }

    /// The decision, separated from the subprocess that feeds it.
    ///
    /// Keeps the last *live* answer through a transient failure -- the CLI is
    /// briefly unrunnable mid-update, and blanking the field then would be a
    /// worse reading than a slightly old one. That is only true of a value this
    /// app read itself; it is not a licence to show someone else's cache.
    ///
    /// A test that re-implements this rather than calling it proves nothing --
    /// which is what the first version of SettingsModelTests did.
    func apply(version: String?) {
        if let version {
            self.version = .live(version)
            return
        }
        if case .live = self.version {
            return  // hold the last live reading through a transient failure
        }
        self.version = .unavailable
    }

    func apply(_ snap: QuernSnapshot) {
        snapshot = snap
        if let c = snap.update.channel { channel = c }
        autoInstallCert = snap.proxy.autoInstallCert
    }
}

/// Whether launching the app should start the daemon.
///
/// Lives in UserDefaults rather than ~/.quern/config.json because the server
/// has no use for it -- it describes what this app does, like the login-item
/// registration next to it, not how Quern behaves. Defaults to on: you opened
/// the Quern app, and a menu that greets you with "stopped" and a button to
/// press is a step that did not need to exist.
///
/// It is a setting rather than unconditional behaviour because the app also
/// registers itself as a login item, so "on launch" includes every login. That
/// is a daemon running because you installed a menu bar app, which is worth
/// being able to decline. Starting the server opens the HTTP listener and a
/// crash-report watcher; syslog and OSLog capture stay off, and the proxy and
/// its certificate stay behind their own consent gates.
enum StartOnLaunch {
    static let key = "quern.startServerOnLaunch"

    static func registerDefault() {
        UserDefaults.standard.register(defaults: [key: true])
    }

    static var isEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: key) }
        set { UserDefaults.standard.set(newValue, forKey: key) }
    }
}

/// Wraps SMAppService.mainApp registration. A stable code-signing identity
/// (Developer ID) is what keeps this registration durable across launches.
enum LoginItem {
    static func isEnabled() -> Bool {
        if #available(macOS 13.0, *) {
            return SMAppService.mainApp.status == .enabled
        }
        return false
    }

    @discardableResult
    static func setEnabled(_ enabled: Bool) -> Bool {
        guard #available(macOS 13.0, *) else { return false }
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled {
                    try SMAppService.mainApp.register()
                }
            } else {
                try SMAppService.mainApp.unregister()
            }
            return true
        } catch {
            NSLog("Login item toggle failed: \(error.localizedDescription)")
            return false
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: SettingsModel

    private var s: ServerState { model.snapshot.server }
    private var u: UpdateInfo { model.snapshot.update }
    private var d: ActiveDevice { model.snapshot.device }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Quern").font(.title2).bold()

            GroupBox("Server") {
                grid("Server", model.serverRows(now: Date()))
            }

            GroupBox("Proxy") {
                grid("Proxy", [
                    ("Status", s.proxyStatus?.capitalized ?? (s.proxyEnabled ? "Enabled" : "Disabled")),
                    ("Port", s.proxyPort.map(String.init) ?? "—"),
                ])
            }

            GroupBox("Active device") {
                grid("Active device", [
                    ("Name", d.name ?? "—"),
                    ("UDID", d.udid ?? "—"),
                ])
            }

            GroupBox("Network capture") {
                VStack(alignment: .leading, spacing: 8) {
                    Toggle(isOn: $model.autoInstallCert) {
                        Text("Install the capture certificate automatically")
                    }
                    .onChange(of: model.autoInstallCert) { newValue in
                        // Same guard as the channel picker below: apply()
                        // assigns this on every fresh snapshot, and writing
                        // back on that path would shell out on each refresh.
                        guard newValue != model.snapshot.proxy.autoInstallCert else { return }
                        QuernCLI.setAutoInstallCert(newValue)
                    }
                    .accessibilityLabel("Install the capture certificate automatically")

                    // Says what it costs, not just what it does. This installs
                    // a root certificate authority, which is a longer-lived
                    // commitment than enabling capture, and the whole reason
                    // the setting is surfaced here rather than left in a file.
                    Text(model.autoInstallCert
                         ? "Quern will install its certificate on a device when capture needs it."
                         : "Quern will ask before installing its certificate on a device.")
                        .foregroundColor(.secondary)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GroupBox("Updates") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        // Same 90pt label column as grid(), so this row lines
                        // up with every other label in the window. A Picker
                        // renders its own label instead, which indented the
                        // control and left it as the one row not flush left.
                        Text("Channel")
                            .foregroundColor(.secondary)
                            .frame(width: 90, alignment: .leading)
                        Picker("Channel", selection: $model.channel) {
                            Text("Stable").tag("stable")
                            Text("Beta").tag("beta")
                        }
                        .labelsHidden()
                        .pickerStyle(.segmented)
                        .frame(maxWidth: 220)
                        .onChange(of: model.channel) { newValue in
                            // Only write when the user actually moved the
                            // picker. `apply()` assigns this too, whenever a
                            // fresh snapshot lands, and writing back on that
                            // path shells out to `quern set-channel` with the
                            // value already on disk. That is not a no-op:
                            // setting the channel clears the cached update
                            // check, so merely opening Settings on a beta
                            // machine wiped the update hint.
                            guard newValue != model.snapshot.update.channel else { return }
                            QuernCLI.setChannel(newValue)
                        }
                        .accessibilityLabel("Update channel")
                        .accessibilityValue(model.channel)
                        Spacer()
                    }
                    if u.updateAvailable, let latest = u.latestVersion {
                        Text("Update available: v\(latest)")
                            .foregroundColor(.secondary).font(.callout)
                    } else {
                        Text("Up to date").foregroundColor(.secondary).font(.callout)
                    }
                }
            }

            // No .accessibilityLabel here, and not for want of trying. In an
            // NSHostingController the modifier reaches Text but is dropped by
            // Toggle, Button and GroupBox's label -- measured: the checkbox
            // ends up with no AXTitle and no AXDescription attribute at all,
            // and neither .accessibilityLabel nor .accessibilityElement
            // (children: .combine) changes that. So VoiceOver announces this
            // as a bare "checkbox". Left unfixed rather than papered over with
            // a modifier that does nothing.
            Toggle("Launch at login", isOn: $model.loginEnabled)
                .onChange(of: model.loginEnabled) { newValue in
                    if !LoginItem.setEnabled(newValue) {
                        // Revert the toggle if the OS refused.
                        model.loginEnabled = LoginItem.isEnabled()
                    }
                }

            Toggle("Start the server when Quern launches", isOn: $model.startOnLaunch)
                .onChange(of: model.startOnLaunch) { newValue in
                    StartOnLaunch.isEnabled = newValue
                }

            HStack {
                Button("Documentation") {
                    NSWorkspace.shared.open(URL(string: "https://quern.dev/docs")!)
                }
                Spacer()
            }
        }
        .padding(20)
        .frame(width: 420)
    }

    /// A label/value table. `section` is not shown -- it exists only to
    /// disambiguate the accessible names.
    ///
    /// Each row is exposed as a single element rather than two loose strings,
    /// because read individually they lose their pairing: the Server and Proxy
    /// boxes both contain a "Status" and a "Running", so a screen reader
    /// walking the window heard the same two words twice with nothing to say
    /// which was which. Qualifying with the section makes each name unique.
    private func grid(_ section: String, _ rows: [(String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(rows, id: \.0) { row in
                HStack(alignment: .top) {
                    Text(row.0)
                        .foregroundColor(.secondary)
                        .frame(width: 90, alignment: .leading)
                        .accessibilityLabel("\(section) \(row.0)")
                    Text(row.1).textSelection(.enabled)
                    Spacer()
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

}

/// Owns the settings NSWindow and keeps it alive while shown.
final class SettingsWindowController {
    let model = SettingsModel()
    private var window: NSWindow?

    func show() {
        if window == nil {
            let hosting = NSHostingController(rootView: SettingsView(model: model))
            let win = NSWindow(contentViewController: hosting)
            win.title = "Quern Settings"
            win.styleMask = [.titled, .closable, .miniaturizable]
            win.isReleasedWhenClosed = false
            window = win
        }
        model.refreshInstalledVersion()
        NSApp.activate(ignoringOtherApps: true)
        window?.center()
        window?.makeKeyAndOrderFront(nil)
    }

    func update(_ snap: QuernSnapshot) {
        model.apply(snap)
    }
}
