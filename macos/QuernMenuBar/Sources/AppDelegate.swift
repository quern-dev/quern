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
    private var checkingForUpdates = false
    /// The answer to a check that found nothing.
    ///
    /// Only meaningful in that case: when a check *does* find something the
    /// menu grows a "Restart to Update" item, which is the answer. Without
    /// this, a check that found nothing left the menu byte-for-byte identical
    /// to before it ran, which is indistinguishable from a dead menu item.
    private var lastCheckResult: String?
    /// Shown beside the icon while something is running.
    ///
    /// The menu is the wrong place for work in progress: it closes the instant
    /// you click an item, so an update reported only there ran for three
    /// minutes with nothing on screen. The status item is the one part of this
    /// app that is always visible, so that is where "something is happening"
    /// belongs. Cleared the moment there is an answer, so the menu bar is not
    /// permanently wider.
    private var activityText: String? {
        didSet {
            guard activityText != oldValue else { return }
            refreshStatusButton()
        }
    }
    private var didAttemptLaunchStart = false
    /// Holds "Checking…" on screen long enough to be seen. See MinimumDisplay.
    private let checkIndicator = MinimumDisplay()

    /// Puts the status item back to whatever is still happening.
    ///
    /// `activityText` is one slot with three writers -- the lifecycle, the
    /// update check, and the updater -- and none of them used to consult the
    /// others. Writing nil unconditionally at the end of a check blanked
    /// "Starting…" for the rest of a daemon start, which is the "ran for
    /// minutes with nothing on screen" failure the field exists to prevent.
    private func restoreActivityText() {
        activityText = lifecycle.isBusy ? lifecycle.statusText : nil
    }

    /// Everything about what is happening to the daemon. This class renders it
    /// and owns none of it -- see LifecycleController for why that split
    /// exists.
    private lazy var lifecycle: LifecycleController = {
        var deps = LifecycleController.Dependencies()
        deps.refreshState = { [weak self] in self?.reader.refresh() }
        deps.serverIsRunning = { [weak self] in
            guard let self else { return false }
            self.reader.refresh()
            return self.reader.snapshot.server.running
        }
        let controller = LifecycleController(deps)
        controller.onChange = { [weak self] in
            guard let self else { return }
            // Same reasoning as the updater: "Starting…" in a closed menu is
            // not feedback. LifecycleController already knows whether it is
            // busy, so this only has to render it.
            // Not while a check is on screen: a lifecycle step finishing
            // mid-check would clear "Checking…" early, and MinimumDisplay puts
            // a floor under the completion, not under an unrelated writer.
            if !self.checkingForUpdates {
                self.activityText = self.lifecycle.isBusy ? self.lifecycle.statusText : nil
            }
            self.refreshStatusButton()
        }
        controller.onAlert = { [weak self] message, detail in
            self?.reportFailure(message, detail: detail)
        }
        return controller
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureStatusButton()

        let menu = NSMenu()
        menu.delegate = self          // rebuilt lazily on each open
        statusItem.menu = menu

        reader.onChange = { [weak self] snap in
            guard let self else { return }
            self.snapshot = snap
            if snap.server.running { self.lifecycle.noteServerRunning() }
            // Same reasoning, for the other status line: without this a failed
            // update left its message in the menu for the life of the process,
            // including long after the user had fixed the cause.
            // Two messages, two lifetimes, so two fields. An update outcome
            // stops mattering once the update has landed; a "nothing new"
            // answer stops mattering once something new turns up. Sharing one
            // field meant one of them was always cleared at the wrong moment.
            if !snap.update.updateAvailable { self.updateStatusText = nil }
            if snap.update.updateAvailable { self.lastCheckResult = nil }
            self.refreshStatusButton()
            self.settings.update(snap)
        }
        reader.start()

        registerLoginItemOnFirstLaunch()
        startServerOnLaunchIfWanted()
    }

    /// Start the daemon once, at launch, unless it is already up or the user
    /// has turned this off.
    ///
    /// `reader.start()` refreshes synchronously, so the snapshot consulted
    /// here is current rather than the empty initial one -- which would read
    /// as "stopped" and start a second daemon on top of a healthy first.
    private func startServerOnLaunchIfWanted() {
        guard !didAttemptLaunchStart else { return }
        didAttemptLaunchStart = true
        guard StartOnLaunch.isEnabled, !reader.snapshot.server.running else { return }

        // menuOnly, not an alert: this can fire at login, and a modal taking
        // focus as you open your laptop is worse than the failure it reports.
        lifecycle.run(.start, reporting: .menuOnly)
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
        // Text beside the icon, not instead of it: the silhouette is how the
        // item is found in a crowded menu bar.
        button.title = activityText.map { " \($0)" } ?? ""
        button.imagePosition = activityText == nil ? .imageOnly : .imageLeading
        // Always a template. The bundled icon is pure black with alpha, so
        // rendering it untemplated would paint solid black -- fine on a light
        // menu bar, invisible on a dark one. The SF Symbol fallback has the
        // same problem, which is why this is unconditional rather than a
        // property of which image was loaded.
        button.image?.isTemplate = true
        // State instead comes from the standard status-item idiom: dimmed when
        // the daemon is down. It reads correctly in both appearances, which a
        // colour swap does not.
        // Three states, not two. Dimming distinguishes stopped from running,
        // but it cannot distinguish "stopped because nobody started it" from
        // "stopped because starting it failed" -- and those look identical in
        // the menu bar, which is the only part most people see. A start we
        // gave up on tints the icon instead.
        //
        // A tint rather than a different glyph, deliberately: the silhouette
        // stays constant for the reason in `statusImage` below, and red on the
        // same shape reads as "this one has a problem" without needing to be
        // recognised as a new symbol.
        //
        // The colour is baked into a non-template copy rather than set with
        // `contentTintColor`, which does nothing here -- the menu bar renders
        // template images in its own appearance and ignores it. Measured: the
        // icon came out neutral dark, which with `appearsDisabled` off made a
        // failed start look exactly like a healthy server. Worse than the
        // ambiguity it was meant to fix.
        button.appearsDisabled = !running && !lifecycle.hasFailed
        if lifecycle.hasFailed, let img = button.image {
            button.image = Self.tinted(img, .systemRed)
        }
        if lifecycle.hasFailed {
            button.toolTip = "Quern could not start — open the menu"
        } else if updateAvailable {
            let version = snapshot.update.latestVersion.map { " (v\($0))" } ?? ""
            button.toolTip = "Quern — update available\(version)"
        } else {
            button.toolTip = running ? "Quern is running" : "Quern is stopped"
        }
    }

    /// A non-template copy of `image` painted in `color`.
    ///
    /// Non-template is the point: it is what stops the menu bar substituting
    /// its own colour. The shape is preserved by compositing over the original,
    /// so this stays the same silhouette rather than becoming a new glyph.
    private static func tinted(_ image: NSImage, _ color: NSColor) -> NSImage {
        // A drawing handler rather than lockFocus/unlockFocus: the latter
        // rasterises once at whatever the main display's scale happens to be,
        // so the icon renders soft after a move between a Retina and a 1x
        // screen. This re-renders per target.
        let out = NSImage(size: image.size, flipped: false) { rect in
            image.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
        // Non-template is the point: it is what stops the menu bar substituting
        // its own colour. The cost is that the icon is no longer inverted to
        // white when the item is highlighted -- it stays red under the
        // selection fill. Accepted: red under a highlight still reads as the
        // problem it is, and the alternative is having no error state at all.
        out.isTemplate = false
        return out
    }

    /// Prefer the bundled template icon; fall back to an SF Symbol so the app
    /// is still usable if the asset is missing from the bundle.
    ///
    /// The bundled icon is one image for every state, so it does not vary the
    /// way the symbol fallback does. That is deliberate: a menu bar full of
    /// glyphs is easier to scan when each app keeps a constant silhouette, and
    /// the states it dropped are all still shown -- running and stopped by the
    /// dimming above, a start that failed by the red tint, an available update
    /// by its own menu item. All three keep the same shape.
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
        if !s.running, let status = lifecycle.statusText {
            menu.addItem(info(status))
            if lifecycle.hasFailed, FileManager.default.fileExists(atPath: Self.serverLog.path) {
                menu.addItem(action("Open Server Log", #selector(openServerLog)))
            }
        }

        menu.addItem(.separator())

        // Lifecycle. Suppressed while one is already running: `quern start`
        // blocks for up to 30s, and a second one racing the first for the port
        // is a worse outcome than a menu with nothing to click for a moment.
        // The status line above says what is happening.
        if lifecycle.isBusy {
            menu.addItem(info("Working…"))
        } else if s.running {
            menu.addItem(action("Stop Server", #selector(stopServer)))
            menu.addItem(action("Restart Server", #selector(restartServer)))
        } else {
            menu.addItem(action("Start Server", #selector(startServer)))
        }

        // The Ollama parallel: only appears once an update is staged.
        if u.updateAvailable {
            let title = u.latestVersion.map { "Restart to Update — v\($0)" } ?? "Restart to Update"
            menu.addItem(action(title, #selector(restartToUpdate)))
        } else if !checkingForUpdates {
            // Always offered when there is nothing staged. The item above is
            // driven by a cached answer the server refreshes at most once a
            // day, so a release landing this afternoon would not be offered
            // until tomorrow and there was no way to ask. The CLI never had
            // that problem -- `quern update` checks when you run it.
            menu.addItem(action("Check for Updates…", #selector(checkForUpdates)))
        }
        if checkingForUpdates {
            menu.addItem(info("Checking for updates…"))
        } else if let result = lastCheckResult {
            menu.addItem(info(result))
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
    @objc private func startServer() { lifecycle.run(.start, reporting: .alert) }
    @objc private func stopServer() { lifecycle.run(.stop, reporting: .alert) }
    @objc private func restartServer() { lifecycle.run(.restart, reporting: .alert) }

    @objc private func checkForUpdates() {
        guard !checkingForUpdates else { return }
        checkingForUpdates = true
        lastCheckResult = nil
        activityText = "Checking…"
        checkIndicator.begin()
        QuernCLI.checkForUpdates { [weak self] code, output in
            guard let self else { return }
            // The whole completion waits out the floor, not just the part that
            // clears the text. Clearing late but answering early would put the
            // result in the menu while the menu still said "Checking for
            // updates…", which is a worse frame than either end state.
            self.checkIndicator.end { [weak self] in
                guard let self else { return }
                self.checkingForUpdates = false
                self.restoreActivityText()
                // The reader refreshes on its own three-second poll, but
                // waiting for that after an action the user explicitly took
                // reads as nothing having happened.
                self.reader.refresh()
                guard code != 0 else {
                    // A successful check that found nothing still deserves an
                    // answer -- the menu would otherwise look identical before
                    // and after, which is indistinguishable from a dead menu
                    // item.
                    if !self.snapshot.update.updateAvailable {
                        self.lastCheckResult = output
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                            .components(separatedBy: "\n").first
                    }
                    return
                }
                // Named, not collapsed. A missing wrapper, a watchdog kill
                // and a CLI error are three different problems with three
                // different answers, and one alert saying "could not check"
                // for all of them is the dead end this work exists to remove.
                // LifecycleController.run already discriminates these; this
                // path did not.
                switch code {
                case QuernCLI.notFoundStatus:
                    self.reportFailure("Could not find the quern command",
                                       detail: output)
                case QuernCLI.timedOutStatus:
                    self.reportFailure(
                        "The update check did not finish",
                        detail: output + "\n\nThis usually means the network "
                            + "is not answering. Try again, or run "
                            + "`quern check-updates` in a terminal to see why."
                    )
                default:
                    self.reportFailure("Could not check for updates", detail: output)
                }
            }
        }
    }

    @objc private func restartToUpdate() {
        updater.restartToUpdate(
            status: { [weak self] progress in
                self?.updateStatusText = progress.text
                self?.activityText = progress.isWorking ? progress.text : nil
            },
            failure: { [weak self] message, detail in
                self?.reportFailure(message, detail: detail)
            }
        )
    }

    @objc private func openServerLog() { NSWorkspace.shared.open(Self.serverLog) }

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
    /// The server's log. `quern start` names this path in its own warning, so
    /// the alert offers to open it rather than leaving the reader to select a
    /// path out of a modal they cannot copy from.
    static var serverLog: URL { StateReader.quernDir.appendingPathComponent("server.log") }

    private func reportFailure(_ message: String, detail: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = message
            let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
            alert.informativeText = trimmed.isEmpty
                ? "Run `quern status` to see what state it is in."
                : trimmed
            alert.addButton(withTitle: "OK")
            // Copy before Open Log: when the CLI could not do something itself
            // it prints the command to run instead, and getting that onto the
            // clipboard is the next step. Copying rather than launching a
            // terminal is deliberate -- running a `sudo` command on one menu
            // click is a larger commitment than this app makes anywhere else,
            // it would pick a terminal on the user's behalf, and driving one
            // needs an Automation permission prompt. The command is visible
            // here and gets pasted wherever they actually work.
            let canCopy = !trimmed.isEmpty
            if canCopy { alert.addButton(withTitle: "Copy") }
            let hasLog = FileManager.default.fileExists(atPath: Self.serverLog.path)
            if hasLog { alert.addButton(withTitle: "Open Log") }

            switch alert.runModal() {
            case .alertSecondButtonReturn where canCopy:
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(trimmed, forType: .string)
            case .alertSecondButtonReturn where hasLog,
                 .alertThirdButtonReturn where hasLog:
                NSWorkspace.shared.open(Self.serverLog)
            default:
                break
            }
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
