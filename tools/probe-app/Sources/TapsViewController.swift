//
//  TapsViewController.swift
//  QuernProbe
//
//  A tap-target oracle (#511): which element a tap actually reached.
//
//  A passing tap check usually means the tap returned `ok` and the next read
//  looked plausible -- and a tap on the wrong element often looks the same.
//  Every button here counts its own taps into a label, `<id>_count`, so a test
//  reads which one the tap hit instead of inferring it. The identifiers,
//  labels and layout match the Android probe's Taps tab, so one test reads
//  both:
//
//  - Target / decoy: side by side, each counted.
//  - Twins: two buttons with the same label, for the ambiguity case.
//  - Slide: after `tap_slide_arm`, `tap_slider` jumps between its start and a
//    spot down and to the right every 120 ms, until reset. `tap_slide_behind`
//    sits where it started, so a tap aimed at a stale position counts there.
//  - Remount: after `tap_remount_arm`, `tap_remount` is removed and added back
//    1.5 s later, shifted down -- the same label on a new view.
//  - Late overlay: 500 ms after `tap_overlay_arm`, an overlay covers
//    `tap_overlay_target` and counts the taps it takes, until
//    `tap_overlay_clear`.
//
//  `tap_reset` clears every counter and puts every cell back. Laid out with
//  fixed frames, because the cells move and the test reasons about where they
//  were.
//

import UIKit

final class TapsViewController: UIViewController {
    // About a tap's round trip through quern, so a read and the tap that
    // follows it can straddle a jump; a one-off slide had always finished
    // before quern read the tree.
    static let slidePeriod: TimeInterval = 0.12
    static let remountGap: TimeInterval = 1.5
    static let overlayDelay: TimeInterval = 0.5

    private var counts: [String: Int] = [:]
    private var labels: [String: UILabel] = [:]
    private var actions: [String: () -> Void] = [:]
    private let board = UIView()
    private var slider: UIButton!
    private var remount: UIButton!
    private var overlay: UIButton!
    private var slideTimer: Timer?
    // Bumped by reset, so a pending remount or overlay from before it is dropped.
    private var generation = 0

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        board.translatesAutoresizingMaskIntoConstraints = false
        board.accessibilityIdentifier = "tap_oracle"
        view.addSubview(board)
        NSLayoutConstraint.activate([
            board.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            board.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            board.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            board.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])

        let heading = UILabel(frame: CGRect(x: 16, y: 8, width: 200, height: 40))
        heading.text = "Taps"
        heading.font = .preferredFont(forTextStyle: .title2)
        heading.accessibilityIdentifier = "taps_heading"
        board.addSubview(heading)

        // Target and decoy, side by side.
        counted("Target", "tap_target", x: 16, y: 56)
        counted("Decoy", "tap_decoy", x: 200, y: 56)

        // Twins: the same label twice.
        counted("Twin", "tap_twin_a", x: 16, y: 156)
        counted("Twin", "tap_twin_b", x: 200, y: 156)

        // Slide: the behind button is where the slider starts.
        counted("Behind", "tap_slide_behind", x: 16, y: 256)
        slider = counted("Slider", "tap_slider", x: 16, y: 256, countY: 328)
        control("Arm slide", "tap_slide_arm", x: 200, y: 256) { [weak self] in self?.armSlide() }

        // Remount.
        remount = counted("Remount", "tap_remount", x: 16, y: 380)
        control("Arm remount", "tap_remount_arm", x: 200, y: 380) { [weak self] in self?.armRemount() }

        // Late overlay over its target.
        counted("Covered", "tap_overlay_target", x: 16, y: 480)
        control("Arm overlay", "tap_overlay_arm", x: 200, y: 480) { [weak self] in self?.armOverlay() }
        control("Clear overlay", "tap_overlay_clear", x: 200, y: 530) { [weak self] in
            self?.overlay.isHidden = true
        }
        overlay = counted("Overlay", "tap_overlay", x: 16, y: 480,
                          background: UIColor(red: 0.8, green: 0.27, blue: 0.27, alpha: 1),
                          countY: 552)

        control("Reset", "tap_reset", x: 16, y: 610) { [weak self] in self?.reset() }
        reset()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        slideTimer?.invalidate()
        slideTimer = nil
    }

    private func button(_ text: String, _ id: String, x: CGFloat, y: CGFloat) -> UIButton {
        var config = UIButton.Configuration.filled()
        config.title = text
        let button = UIButton(configuration: config)
        button.frame = CGRect(x: x, y: y, width: 160, height: 48)
        button.accessibilityIdentifier = id
        button.addTarget(self, action: #selector(tapped(_:)), for: .touchUpInside)
        board.addSubview(button)
        return button
    }

    /// A button that counts its taps into a label beneath it: `<id>_count`.
    @discardableResult
    private func counted(_ text: String, _ id: String, x: CGFloat, y: CGFloat,
                         background: UIColor? = nil, countY: CGFloat? = nil) -> UIButton {
        let button = button(text, id, x: x, y: y)
        if let background { button.configuration?.baseBackgroundColor = background }
        let count = UILabel(frame: CGRect(x: x, y: countY ?? y + 48, width: 160, height: 24))
        count.accessibilityIdentifier = "\(id)_count"
        board.addSubview(count)
        labels[id] = count
        actions[id] = { [weak self] in
            guard let self else { return }
            self.counts[id, default: 0] += 1
            self.render(id)
        }
        return button
    }

    private func control(_ text: String, _ id: String, x: CGFloat, y: CGFloat,
                         action: @escaping () -> Void) {
        let button = button(text, id, x: x, y: y)
        button.configuration = .gray()
        button.configuration?.title = text
        actions[id] = action
    }

    @objc private func tapped(_ sender: UIButton) {
        guard let id = sender.accessibilityIdentifier else { return }
        actions[id]?()
    }

    private func render(_ id: String) {
        let name = id.hasPrefix("tap_") ? String(id.dropFirst(4)) : id
        labels[id]?.text = "\(name) \(counts[id] ?? 0)"
    }

    private func armSlide() {
        slideTimer?.invalidate()
        slideTimer = Timer.scheduledTimer(withTimeInterval: Self.slidePeriod, repeats: true) {
            [weak self] _ in
            guard let slider = self?.slider else { return }
            slider.transform = slider.transform.isIdentity
                ? CGAffineTransform(translationX: 184, y: 80) : .identity
        }
    }

    private func armRemount() {
        remount.removeFromSuperview()
        let armed = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.remountGap) { [weak self] in
            guard let self, self.generation == armed else { return }
            self.remount.frame.origin.y = 404
            self.board.addSubview(self.remount)
        }
    }

    private func armOverlay() {
        let armed = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.overlayDelay) { [weak self] in
            guard let self, self.generation == armed else { return }
            self.overlay.isHidden = false
        }
    }

    private func reset() {
        generation += 1
        slideTimer?.invalidate()
        slideTimer = nil
        counts.removeAll()
        labels.keys.forEach(render)
        slider.transform = .identity
        remount.frame.origin.y = 380
        if remount.superview == nil { board.addSubview(remount) }
        overlay.isHidden = true
    }
}
