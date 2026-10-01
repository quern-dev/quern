//
//  StateViewController.swift
//  QuernProbe
//
//  The one persistent surface in the app, for exercising the app-state and
//  plist tools. Three UserDefaults keys of three types, each mirrored in a
//  label, so a test can write the preferences plist from outside and read
//  back what the app actually sees -- which is not the same thing as what is
//  in the file, since the app reads through cfprefsd's cache.
//
//  Labels are read from UserDefaults when the tab appears and on Reload, not
//  kept in memory, so a relaunch shows whatever is persisted.
//

import UIKit

enum ProbeState {
    static let greetingKey = "probe.greeting"
    static let counterKey = "probe.counter"
    static let flagKey = "probe.flag"
}

final class StateViewController: UIViewController {
    private let greetingLabel = UILabel()
    private let counterLabel = UILabel()
    private let flagLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let rows: [(UILabel, String)] = [
            (greetingLabel, "state_greeting"),
            (counterLabel, "state_counter"),
            (flagLabel, "state_flag"),
        ]
        var y: CGFloat = 120
        for (label, identifier) in rows {
            label.frame = CGRect(x: 20, y: y, width: view.bounds.width - 40, height: 28)
            label.font = .monospacedSystemFont(ofSize: 14, weight: .regular)
            label.accessibilityIdentifier = identifier
            view.addSubview(label)
            y += 36
        }

        let buttons: [(String, String, Selector)] = [
            ("Increment", "state_increment", #selector(increment)),
            ("Toggle flag", "state_toggle_flag", #selector(toggleFlag)),
            ("Reload", "state_reload", #selector(reload)),
            ("Reset", "state_reset", #selector(reset)),
        ]
        y += 12
        for (title, identifier, action) in buttons {
            let button = UIButton(type: .system)
            button.setTitle(title, for: .normal)
            button.frame = CGRect(x: 20, y: y, width: 200, height: 36)
            button.contentHorizontalAlignment = .left
            button.accessibilityIdentifier = identifier
            button.addTarget(self, action: action, for: .touchUpInside)
            view.addSubview(button)
            y += 44
        }
        reload()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        reload()
    }

    @objc private func increment() {
        let defaults = UserDefaults.standard
        defaults.set(defaults.integer(forKey: ProbeState.counterKey) + 1,
                     forKey: ProbeState.counterKey)
        reload()
    }

    @objc private func toggleFlag() {
        let defaults = UserDefaults.standard
        defaults.set(!defaults.bool(forKey: ProbeState.flagKey), forKey: ProbeState.flagKey)
        reload()
    }

    @objc private func reset() {
        for key in [ProbeState.greetingKey, ProbeState.counterKey, ProbeState.flagKey] {
            UserDefaults.standard.removeObject(forKey: key)
        }
        reload()
    }

    /// Absent keys render as `—` rather than as their type's zero value, so
    /// "never written" and "written as 0 / false" stay distinguishable.
    @objc private func reload() {
        let defaults = UserDefaults.standard
        func show(_ key: String) -> String {
            guard let value = defaults.object(forKey: key) else {
                return "—"
            }
            return "\(value)"
        }
        greetingLabel.text = "greeting: \(show(ProbeState.greetingKey))"
        counterLabel.text = "counter: \(show(ProbeState.counterKey))"
        // NSNumber prints a bool as 1/0; spell it so a string "1" written by
        // mistake cannot read as `true`.
        if let flag = defaults.object(forKey: ProbeState.flagKey) as? NSNumber,
           CFGetTypeID(flag) == CFBooleanGetTypeID() {
            flagLabel.text = "flag: \(flag.boolValue)"
        } else {
            flagLabel.text = "flag: \(show(ProbeState.flagKey))"
        }
    }
}
