//
//  ControlsViewController.swift
//  QuernProbe
//
//  Standard controls for exercising element-state reads (value, selected),
//  value-aware tapping (tap_element value=), and alert/sheet handling.
//

import UIKit

final class ControlsViewController: UIViewController {
    private let valueLabel = UILabel()
    private let toggle = UISwitch()
    private let slider = UISlider()
    private let segment = UISegmentedControl(items: ["One", "Two", "Three"])
    private let stepper = UIStepper()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let width = view.bounds.width - 40
        var y: CGFloat = 120

        toggle.frame = CGRect(x: 20, y: y, width: 60, height: 31)
        toggle.accessibilityIdentifier = "control_switch"
        toggle.addTarget(self, action: #selector(controlChanged), for: .valueChanged)
        view.addSubview(toggle)
        y += 50

        slider.frame = CGRect(x: 20, y: y, width: width, height: 31)
        slider.accessibilityIdentifier = "control_slider"
        slider.minimumValue = 0
        slider.maximumValue = 100
        slider.value = 50
        slider.addTarget(self, action: #selector(controlChanged), for: .valueChanged)
        view.addSubview(slider)
        y += 50

        segment.frame = CGRect(x: 20, y: y, width: width, height: 32)
        segment.accessibilityIdentifier = "control_segment"
        segment.selectedSegmentIndex = 0
        segment.addTarget(self, action: #selector(controlChanged), for: .valueChanged)
        view.addSubview(segment)
        y += 52

        stepper.frame = CGRect(x: 20, y: y, width: 94, height: 32)
        stepper.accessibilityIdentifier = "control_stepper"
        stepper.addTarget(self, action: #selector(controlChanged), for: .valueChanged)
        view.addSubview(stepper)
        y += 52

        let alertButton = UIButton(type: .system)
        alertButton.frame = CGRect(x: 20, y: y, width: width, height: 44)
        alertButton.setTitle("Show Alert", for: .normal)
        alertButton.accessibilityIdentifier = "control_show_alert"
        alertButton.addTarget(self, action: #selector(showAlert), for: .touchUpInside)
        view.addSubview(alertButton)
        y += 54

        let sheetButton = UIButton(type: .system)
        sheetButton.frame = CGRect(x: 20, y: y, width: width, height: 44)
        sheetButton.setTitle("Show Sheet", for: .normal)
        sheetButton.accessibilityIdentifier = "control_show_sheet"
        sheetButton.addTarget(self, action: #selector(showSheet), for: .touchUpInside)
        view.addSubview(sheetButton)
        y += 54

        valueLabel.frame = CGRect(x: 20, y: y + 10, width: width, height: 60)
        valueLabel.numberOfLines = 3
        valueLabel.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        valueLabel.textColor = .secondaryLabel
        valueLabel.accessibilityIdentifier = "control_value_log"
        valueLabel.text = "no control events yet"
        view.addSubview(valueLabel)
    }
}

private extension ControlsViewController {
    @objc func controlChanged() {
        let sliderValue = slider.value.formatted(.number.precision(.fractionLength(1)))
        valueLabel.text = "switch=\(toggle.isOn) slider=\(sliderValue) "
            + "segment=\(segment.selectedSegmentIndex) stepper=\(Int(stepper.value))"
    }

    @objc func showAlert() {
        let alert = UIAlertController(
            title: "Probe Alert",
            message: "Deterministic alert for dismissal tests.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Confirm", style: .default) { [weak self] _ in
            self?.valueLabel.text = "alert confirmed"
        })
        present(alert, animated: false)
    }

    @objc func showSheet() {
        let sheet = UIAlertController(
            title: "Probe Sheet",
            message: "Deterministic action sheet for dismissal tests.",
            preferredStyle: .actionSheet
        )
        sheet.addAction(UIAlertAction(title: "Option A", style: .default) { [weak self] _ in
            self?.valueLabel.text = "sheet optionA"
        })
        sheet.addAction(UIAlertAction(title: "Option B", style: .default) { [weak self] _ in
            self?.valueLabel.text = "sheet optionB"
        })
        sheet.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        present(sheet, animated: false)
    }
}
