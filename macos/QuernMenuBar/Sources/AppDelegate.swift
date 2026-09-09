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
            menu.addItem(info("Active device: \(snapshot.device.name ?? snapshot.device.udid ?? "none")"))
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

    @objc private func startServer() { QuernCLI.start { [weak self] _, _ in self?.reader.refresh() } }
    @objc private func stopServer() { QuernCLI.stop { [weak self] _, _ in self?.reader.refresh() } }
    @objc private func restartServer() { QuernCLI.restart { [weak self] _, _ in self?.reader.refresh() } }

    @objc private func restartToUpdate() {
        updater.restartToUpdate { [weak self] status in
            self?.updateStatusText = status
        }
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
        QuernCLI.stop { _, _ in NSApp.terminate(nil) }
    }

    // MARK: - Login item

    private func registerLoginItemOnFirstLaunch() {
        let key = "quern.didRegisterLoginItem"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        if #available(macOS 13.0, *) {
            if SMAppService.mainApp.status == .notRegistered {
                LoginItem.setEnabled(true)
            }
        }
        UserDefaults.standard.set(true, forKey: key)
    }
}
