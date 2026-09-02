//
//  DiagViewController.swift
//  QuernProbe
//
//  Terminal actions, on their own tab and behind explicit taps.
//
//  A crash ends the process and takes any in-progress test with it, so these
//  run in their own phase with a relaunch afterwards. That is an ordering
//  problem, not a reason for a separate app.
//

import UIKit

final class DiagViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let width = view.bounds.width - 40
        var y: CGFloat = 120

        let caption = UILabel(frame: CGRect(x: 20, y: y, width: width, height: 40))
        caption.text = "These terminate or freeze the app on purpose."
        caption.numberOfLines = 2
        caption.font = .systemFont(ofSize: 14)
        caption.textColor = .secondaryLabel
        view.addSubview(caption)
        y += 56

        for (title, identifier, action) in [
            ("Crash (uncaught exception)", "diag_crash_uncaught", #selector(crashUncaught)),
            ("Crash (fatalError)", "diag_crash_fatal", #selector(crashFatal)),
            ("Hang main thread", "diag_hang", #selector(hang)),
        ] {
            let button = UIButton(type: .system)
            button.frame = CGRect(x: 20, y: y, width: width, height: 40)
            button.setTitle(title, for: .normal)
            button.accessibilityIdentifier = identifier
            button.setTitleColor(.systemRed, for: .normal)
            button.addTarget(self, action: action, for: .touchUpInside)
            view.addSubview(button)
            y += 48
        }
    }

    @objc private func crashUncaught() {
        NSException(name: .genericException,
                    reason: "QuernProbe deliberate crash (uncaught exception)",
                    userInfo: nil).raise()
    }

    @objc private func crashFatal() {
        fatalError("QuernProbe deliberate crash (fatalError)")
    }

    @objc private func hang() {
        Thread.sleep(forTimeInterval: 12)
    }
}
