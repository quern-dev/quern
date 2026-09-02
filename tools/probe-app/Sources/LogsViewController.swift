//
//  LogsViewController.swift
//  QuernProbe
//
//  Every logging path iOS offers, emitted only while explicitly running.
//
//  Absorbed from the standalone LogTester app, which lived outside version
//  control while `server/sources/device_log.py` documented its parsing regex
//  against LogTester's output. Folding it in here puts the fixture behind that
//  regex in the repo, and gives it a self-test it never had.
//
//  Idle-by-default is deliberate: a fixture that logs continuously pollutes the
//  log stream during every other test in the same app, and log volume is itself
//  under test. Emission is a mode the test starts and stops.
//

import Foundation
import OSLog
import UIKit
import os

final class LogsViewController: UIViewController {
    private let statusLabel = UILabel()
    private let tickLabel = UILabel()
    private var timer: Timer?
    private var tick = 0

    private let legacyLog = OSLog(subsystem: "com.quern.probe", category: "probe")
    private let logger = Logger(subsystem: "com.quern.probe", category: "swift-logger")

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let width = view.bounds.width - 40
        var y: CGFloat = 120

        statusLabel.frame = CGRect(x: 20, y: y, width: width, height: 24)
        statusLabel.accessibilityIdentifier = "log_status"
        statusLabel.text = "stopped"
        view.addSubview(statusLabel)
        y += 32

        tickLabel.frame = CGRect(x: 20, y: y, width: width, height: 40)
        tickLabel.accessibilityIdentifier = "log_tick_count"
        tickLabel.font = .systemFont(ofSize: 28, weight: .semibold)
        tickLabel.text = "0"
        view.addSubview(tickLabel)
        y += 56

        for (title, identifier, action) in [
            ("Start emitting", "log_start", #selector(start)),
            ("Stop emitting", "log_stop", #selector(stop)),
            ("Emit burst", "log_burst", #selector(burst)),
        ] {
            let button = UIButton(type: .system)
            button.frame = CGRect(x: 20, y: y, width: width, height: 40)
            button.setTitle(title, for: .normal)
            button.accessibilityIdentifier = identifier
            button.addTarget(self, action: action, for: .touchUpInside)
            view.addSubview(button)
            y += 48
        }
    }

    @objc private func start() {
        guard timer == nil else { return }
        statusLabel.text = "running"
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.emitAll()
        }
        emitAll()
    }

    @objc private func stop() {
        timer?.invalidate()
        timer = nil
        statusLabel.text = "stopped"
    }

    @objc private func burst() {
        // A known, finite volume, for tests that need to count rather than sample.
        for i in 0..<20 {
            os_log("[PROBE-BURST] %{public}d of 20", log: legacyLog, type: .default, i)
        }
    }

    private func emitAll() {
        tick += 1
        // print() reaches stdout only; it is here so a test can distinguish
        // "the pipeline dropped it" from "it was never written to the log".
        print("[PROBE-PRINT] tick=\(tick) print()")
        NSLog("[PROBE-NSLOG] tick=%d NSLog()", tick)
        os_log("[PROBE-OSLOG-DEFAULT] tick=%{public}d", type: .default, tick)
        os_log("[PROBE-OSLOG-SUBSYSTEM] tick=%{public}d", log: legacyLog, type: .default, tick)
        os_log("[PROBE-OSLOG-INFO] tick=%{public}d", log: legacyLog, type: .info, tick)
        os_log("[PROBE-OSLOG-DEBUG] tick=%{public}d", log: legacyLog, type: .debug, tick)
        os_log("[PROBE-OSLOG-ERROR] tick=%{public}d", log: legacyLog, type: .error, tick)
        logger.log("[PROBE-LOGGER] tick=\(self.tick)")
        logger.info("[PROBE-LOGGER-INFO] tick=\(self.tick)")
        logger.debug("[PROBE-LOGGER-DEBUG] tick=\(self.tick)")
        logger.error("[PROBE-LOGGER-ERROR] tick=\(self.tick)")
        tickLabel.text = String(tick)
    }

    deinit { timer?.invalidate() }
}
