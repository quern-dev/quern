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
        button.toolTip = "Quern"
    }

    private func refreshStatusButton() {
        guard let button = statusItem.button else { return }
        button.image = Self.statusImage(
            running: snapshot.server.running,
            updateAvailable: snapshot.update.updateAvailable
        )
        button.image?.isTemplate = !snapshot.update.updateAvailable
    }

    /// Prefer a bundled template icon; fall back to an SF Symbol so the app is
    /// always usable even before a custom icon ships.
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
            menu.addItem(action("Stop", #selector(stopServer)))
            menu.addItem(action("Restart", #selector(restartServer)))
        } else {
            menu.addItem(action("Start", #selector(startServer)))
        }

        // The Ollama parallel: only appears once an update is staged.
        if u.updateAvailable {
            let title = u.latestVersion.map { "Restart to Update — v\($0)" } ?? "Restart to Update"
            menu.addItem(action(title, #selector(restartToUpdate)))
        }

        menu.addItem(.separator())
        menu.addItem(action("Settings…", #selector(openSettings), key: ","))
        menu.addItem(action("Documentation", #selector(openDocs)))
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
        NSWorkspace.shared.open(URL(string: "https://quern.dev")!)
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
