// Status-bar controller: owns the NSStatusItem, rebuilds the menu from the
// latest snapshot, and wires menu actions to the CLI/updater.

import AppKit
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let reader = StateReader()
    private let settings = SettingsWindowController()
    private let updater = Updater()
    private var snapshot = QuernSnapshot()
    private var updateStatusText: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureStatusButton()

        let menu = NSMenu()
        menu.delegate = self          // rebuilt lazily on each open
        statusItem.menu = menu

        reader.onChange = { [weak self] snap in
            guard let self else { return }
            self.snapshot = snap
            self.refreshStatusButton()
            self.settings.update(snap)
        }
        reader.start()

        registerLoginItemOnFirstLaunch()
    }

    // MARK: - Status button

    private func configureStatusButton() {
        guard let button = statusItem.button else { return }
        button.image = Self.statusImage(running: false, updateAvailable: false)
        button.image?.isTemplate = true
        button.appearsDisabled = true
        button.toolTip = "Quern is stopped"
    }

    private func refreshStatusButton() {
        guard let button = statusItem.button else { return }
        let running = snapshot.server.running
        let updateAvailable = snapshot.update.updateAvailable
        button.image = Self.statusImage(running: running, updateAvailable: updateAvailable)
        // Always a template. The bundled icon is pure black with alpha, so
        // rendering it untemplated would paint solid black -- fine on a light
        // menu bar, invisible on a dark one. The SF Symbol fallback has the
        // same problem, which is why this is unconditional rather than a
        // property of which image was loaded.
        button.image?.isTemplate = true
        // State instead comes from the standard status-item idiom: dimmed when
        // the daemon is down. It reads correctly in both appearances, which a
        // colour swap does not.
        button.appearsDisabled = !running
        if updateAvailable {
            let version = snapshot.update.latestVersion.map { " (v\($0))" } ?? ""
            button.toolTip = "Quern — update available\(version)"
        } else {
            button.toolTip = running ? "Quern is running" : "Quern is stopped"
        }
    }

    /// Prefer the bundled template icon; fall back to an SF Symbol so the app
    /// is still usable if the asset is missing from the bundle.
    ///
    /// The bundled icon is one image for every state, so it does not vary the
    /// way the symbol fallback does. That is deliberate: a menu bar full of
    /// glyphs is easier to scan when each app keeps a constant silhouette, and
    /// the states it dropped are all still shown -- running and stopped by the
    /// dimming above, an available update by its own menu item.
    private static func statusImage(running: Bool, updateAvailable: Bool) -> NSImage? {
        if let url = Bundle.main.url(forResource: "StatusIcon", withExtension: "png"),
           let img = NSImage(contentsOf: url) {
            img.size = NSSize(width: 18, height: 18)
            return img
        }
        let symbol: String
        if updateAvailable { symbol = "arrow.down.circle.fill" }
        else if running { symbol = "circle.grid.cross.fill" }
        else { symbol = "circle.grid.cross" }
        let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "Quern")
        return img
    }

    // MARK: - Menu construction

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let s = snapshot.server
        let u = snapshot.update

        // Header — running/stopped + uptime.
        let header = NSMenuItem(
            title: s.running ? "Quern is running\(uptimeSuffix(s.startedAt))" : "Quern is stopped",
            action: nil, keyEquivalent: ""
        )
        header.isEnabled = false
        menu.addItem(header)

        if s.running {
            menu.addItem(info("Active device: \(activeDeviceLabel)"))
            menu.addItem(info("Proxy: \(proxyDescription(s))"))
        }
        if let status = updateStatusText {
            menu.addItem(info(status))
        }

        menu.addItem(.separator())

        // Lifecycle (monitor + manual control — never owns the daemon).
        if s.running {
            menu.addItem(action("Stop Server", #selector(stopServer)))
            menu.addItem(action("Restart Server", #selector(restartServer)))
        } else {
            menu.addItem(action("Start Server", #selector(startServer)))
        }

        // The Ollama parallel: only appears once an update is staged.
        if u.updateAvailable {
            let title = u.latestVersion.map { "Restart to Update — v\($0)" } ?? "Restart to Update"
            menu.addItem(action(title, #selector(restartToUpdate)))
        }

        menu.addItem(.separator())
        // Only offered when the bundle is actually there. quern writes it into
        // ~/.quern/bin on setup, so a menu bar running against an install that
        // predates it would otherwise show an item that does nothing.
        if Self.screenMirrorApp != nil {
            menu.addItem(action("Screen Mirror…", #selector(openScreenMirror)))
        }
        menu.addItem(action("Documentation", #selector(openDocs)))

        // Settings sits alone deliberately. macOS attaches an SF Symbol gear
        // to this item on its own -- renaming it and dropping the comma
        // shortcut both failed to stop it, and the image is re-derived rather
        // than stored, so assigning nil does nothing either. A menu reserves
        // an icon gutter per section, so while the gear shared a section it
        // indented Screen Mirror and Documentation with it. Given the icon
        // cannot be removed, it gets its own section instead of a fight.
        menu.addItem(.separator())
        menu.addItem(action("Settings…", #selector(openSettings), key: ","))
        menu.addItem(.separator())

        // Quit, with an Option-revealed "Quit and Stop Server" alternate.
        let quit = action("Quit Quern", #selector(quitApp), key: "q")
        menu.addItem(quit)
        let quitStop = action("Quit and Stop Server", #selector(quitAndStop), key: "q")
        quitStop.keyEquivalentModifierMask = [.command, .option]
        quitStop.isAlternate = true
        menu.addItem(quitStop)
    }

    // MARK: - Menu item helpers

    private func info(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func action(_ title: String, _ selector: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = self
        return item
    }

    /// The name, never the full UDID. A 36-character UDID set the width of
    /// the entire dropdown, and every other row inherited it -- the name is
    /// both friendlier and far narrower. The server fills the name into the
    /// active-device sidecar on every resolve, so the fallback is rare: an
    /// older server, or a device set by UDID before the name cache warmed.
    /// It shows a short prefix rather than nothing, so the row still tells
    /// two simulators apart. Settings keeps the full UDID on its own row.
    private var activeDeviceLabel: String {
        var label: String
        if let name = snapshot.device.name, !name.isEmpty {
            label = name
        } else if let udid = snapshot.device.udid, !udid.isEmpty {
            label = String(udid.prefix(8))
        } else {
            return "none"
        }
        if let kind = deviceKindSuffix { label += " (\(kind))" }
        return label
    }

    /// Simulator or real hardware, which a name alone does not settle -- a
    /// simulator carries the same "iPhone 16 Pro" as the phone on the desk.
    /// The platform is left implied by the device name rather than spelled
    /// out, so an Android emulator reads "(Emulator)" and not "(Android
    /// Emulator)". An unrecognised or missing value adds no qualifier: the
    /// sidecar omits the type when the server has not cached one, and a
    /// wrong guess here is worse than silence.
    private var deviceKindSuffix: String? {
        switch snapshot.device.kind {
        case "simulator": return "Simulator"
        case "device", "android_device": return "Device"
        case "android_emulator": return "Emulator"
        default: return nil
        }
    }

    private func proxyDescription(_ s: ServerState) -> String {
        let status = s.proxyStatus ?? (s.proxyEnabled ? "running" : "stopped")
        if let port = s.proxyPort, status == "running" { return "running :\(port)" }
        return status
    }

    private func uptimeSuffix(_ since: Date?) -> String {
        guard let since else { return "" }
        let secs = Int(Date().timeIntervalSince(since))
        if secs < 60 { return " · up \(secs)s" }
        if secs < 3600 { return " · up \(secs / 60)m" }
        if secs < 86400 { return " · up \(secs / 3600)h" }
        return " · up \(secs / 86400)d"
    }

    // MARK: - Actions

    // These three discarded the exit status and only refreshed. A `quern` that
    // could not be found, or a start that failed, then produced no visible
    // effect whatsoever -- the menu reopened saying "Quern is stopped", which
    // is exactly what it said before, so the click read as a dead menu item
    // rather than as a failure. `quitAndStop` below already got this right.
    @objc private func startServer() { runLifecycle("start", QuernCLI.start) }
    @objc private func stopServer() { runLifecycle("stop", QuernCLI.stop) }
    @objc private func restartServer() { runLifecycle("restart", QuernCLI.restart) }

    private func runLifecycle(
        _ verb: String,
        _ action: (((Int32, String) -> Void)?) -> Void
    ) {
        action { [weak self] code, output in
            guard let self else { return }
            self.reader.refresh()
            guard code != 0 else { return }
            self.reportFailure("Could not \(verb) the server", detail: output)
        }
    }

    @objc private func restartToUpdate() {
        updater.restartToUpdate(
            status: { [weak self] status in self?.updateStatusText = status },
            failure: { [weak self] message, detail in
                self?.reportFailure(message, detail: detail)
            }
        )
    }

    @objc private func openSettings() { settings.show() }

    @objc private func openDocs() {
        NSWorkspace.shared.open(URL(string: "https://quern.dev/docs")!)
    }

    /// The screen-mirror bundle quern installs alongside its other binaries,
    /// or nil if this install doesn't have one.
    static var screenMirrorApp: URL? {
        let url = StateReader.quernDir
            .appendingPathComponent("bin", isDirectory: true)
            .appendingPathComponent("Quern Preview.app", isDirectory: true)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    /// Launch the mirror straight from its bundle rather than through the CLI.
    ///
    /// There is no `quern` subcommand for it, and the HTTP route needs the API
    /// key — but neither is necessary: `ios-preview` with no arguments already
    /// means "offer every device", which is exactly what a user picking this
    /// from a menu wants. Opening the bundle also gets a Dock icon and the
    /// camera-access prompt attributed to "Quern Preview", which spawning the
    /// bare executable would not.
    @objc private func openScreenMirror() {
        guard let url = Self.screenMirrorApp else { return }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        NSWorkspace.shared.openApplication(at: url, configuration: config) { _, error in
            guard let error else { return }
            NSLog("Screen mirror launch failed: \(error.localizedDescription)")
        }
    }

    @objc private func quitApp() { NSApp.terminate(nil) }

    @objc private func quitAndStop() {
        QuernCLI.stop { [weak self] code, output in
            guard code == 0 else {
                // Quitting anyway is the one outcome this item must not
                // produce: the server keeps running and the menu bar that
                // would have said so is gone, so the failure is invisible and
                // the next launch finds a daemon the user believes they
                // stopped. Stay up and report it instead.
                self?.reportFailure("Could not stop the server", detail: output)
                return
            }
            NSApp.terminate(nil)
        }
    }

    /// Surfaces a failed action. The menu bar has no window to put an error
    /// in, so this is a modal alert -- rare by construction, since it only
    /// fires when an explicit command the user chose did not do what it said.
    private func reportFailure(_ message: String, detail: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = message
            let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
            alert.informativeText = trimmed.isEmpty
                ? "Run `quern status` to see what state it is in."
                : trimmed
            alert.runModal()
        }
    }

    // MARK: - Login item

    private func registerLoginItemOnFirstLaunch() {
        let key = "quern.didRegisterLoginItem"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        if #available(macOS 13.0, *) {
            if SMAppService.mainApp.status == .notRegistered {
                // Marking first launch done on a failed registration burns the
                // only attempt: the guard above returns on every later launch,
                // so a transient failure here means the app never launches at
                // login and never tries again. Leave the marker unset and let
                // the next launch retry.
                guard LoginItem.setEnabled(true) else { return }
            }
        }
        UserDefaults.standard.set(true, forKey: key)
    }
}
