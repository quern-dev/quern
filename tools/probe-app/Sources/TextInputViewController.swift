//
//  TextInputViewController.swift
//  QuernProbe
//
//  Text fields covering the keyboard types that matter for input-fidelity
//  testing (shift handling, URL keyboards, secure entry). The event log label
//  echoes the last UITextField delegate callback so tests and humans can see
//  exactly what character data reached UIKit.
//

import os
import UIKit

final class TextInputViewController: UIViewController {
    private let log = Logger(subsystem: "com.quern.probe", category: "text-input")
    private let eventLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let configs: [(String, UIKeyboardType, Bool)] = [
            ("field_default", .default, false),
            ("field_url", .URL, false),
            ("field_email", .emailAddress, false),
            ("field_secure", .default, true),
        ]

        var y: CGFloat = 120
        for (identifier, keyboard, isSecure) in configs {
            let field = UITextField(frame: CGRect(x: 20, y: y, width: view.bounds.width - 40, height: 40))
            field.borderStyle = .roundedRect
            field.placeholder = identifier
            field.accessibilityIdentifier = identifier
            field.keyboardType = keyboard
            field.isSecureTextEntry = isSecure
            field.autocorrectionType = .no
            field.autocapitalizationType = .none
            field.smartDashesType = .no
            field.smartQuotesType = .no
            field.smartInsertDeleteType = .no
            field.spellCheckingType = .no
            field.delegate = self
            view.addSubview(field)
            y += 60
        }

        eventLabel.frame = CGRect(x: 20, y: y + 10, width: view.bounds.width - 40, height: 80)
        eventLabel.numberOfLines = 4
        eventLabel.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        eventLabel.textColor = .secondaryLabel
        eventLabel.accessibilityIdentifier = "text_event_log"
        eventLabel.text = "no input events yet"
        view.addSubview(eventLabel)

        // A physical device has no hardware keyboard, so the software one
        // stays up after typing and covers the tab bar. Its Dictate key sits
        // where the More tab is; a tap meant for the tab once landed on it and
        // raised a system prompt over the app (F32). The suite dismisses the
        // keyboard with this before it navigates.
        let dismiss = UIButton(type: .system)
        dismiss.setTitle("Dismiss keyboard", for: .normal)
        dismiss.frame = CGRect(x: 20, y: y + 100, width: 200, height: 36)
        dismiss.contentHorizontalAlignment = .left
        dismiss.accessibilityIdentifier = "text_dismiss_keyboard"
        dismiss.addTarget(self, action: #selector(dismissKeyboard), for: .touchUpInside)
        view.addSubview(dismiss)
    }

    @objc private func dismissKeyboard() {
        view.endEditing(true)
    }
}

extension TextInputViewController: UITextFieldDelegate {
    func textField(_ textField: UITextField,
                   shouldChangeCharactersIn range: NSRange,
                   replacementString string: String) -> Bool {
        let identifier = textField.accessibilityIdentifier ?? "?"
        let event = "\(identifier) loc=\(range.location) len=\(range.length) repl=\(string.debugDescription)"
        log.info("\(event, privacy: .public)")
        eventLabel.text = event
        return true
    }

    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        textField.resignFirstResponder()
        return true
    }
}
