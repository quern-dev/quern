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
    @Published var channel: String = "stable"

    func apply(_ snap: QuernSnapshot) {
        snapshot = snap
        if let c = snap.update.channel { channel = c }
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
                grid("Server", [
                    ("Status", s.running ? "Running" : "Stopped"),
                    ("Address", s.host != nil ? "\(s.host!):\(s.port ?? 0)" : "—"),
                    ("Version", Updater.installedVersion() ?? u.currentVersion ?? "—"),
                    ("Uptime", uptimeString(s.startedAt)),
                ])
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

            GroupBox("Updates") {
                VStack(alignment: .leading, spacing: 8) {
                    Picker("Channel", selection: $model.channel) {
                        Text("Stable").tag("stable")
                        Text("Beta").tag("beta")
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 220)
                    .onChange(of: model.channel) { newValue in
                        // Only write when the user actually moved the picker.
                        // `apply()` assigns this too, whenever a fresh
                        // snapshot lands, and writing back on that path shells
                        // out to `quern set-channel` with the value already on
                        // disk. That is not a no-op: setting the channel
                        // clears the cached update check, so merely opening
                        // Settings on a beta machine wiped the update hint.
                        guard newValue != model.snapshot.update.channel else { return }
                        QuernCLI.setChannel(newValue)
                    }
                    .accessibilityLabel("Update channel")
                    .accessibilityValue(model.channel)
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

            HStack {
                Button("Documentation") {
                    NSWorkspace.shared.open(URL(string: "https://quern.dev")!)
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

    private func uptimeString(_ since: Date?) -> String {
        guard let since else { return "—" }
        let secs = Int(Date().timeIntervalSince(since))
        if secs < 60 { return "\(secs)s" }
        if secs < 3600 { return "\(secs / 60)m" }
        if secs < 86400 { return "\(secs / 3600)h \((secs % 3600) / 60)m" }
        return "\(secs / 86400)d \((secs % 86400) / 3600)h"
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
        NSApp.activate(ignoringOtherApps: true)
        window?.center()
        window?.makeKeyAndOrderFront(nil)
    }

    func update(_ snap: QuernSnapshot) {
        model.apply(snap)
    }
}
