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
//  - Slide: after `tap_slide_arm`, `tap_slider` jumps between its start and
//    the spot below `tap_slide_arm` every 120 ms, until reset or the tab is
//    left. `tap_slide_behind` sits where it started, so a tap aimed at a stale
//    position counts there or reaches nothing.
//  - Remount: `tap_remount_arm` removes `tap_remount`, and 1.5 s later a new
//    button with the same identifier and label appears a column over and a
//    row down. Nothing is left where the old one was, so a tap at its old
//    position reaches nothing.
//  - Late overlay: 500 ms after `tap_overlay_arm`, an overlay covers the
//    centre of `tap_overlay_target` -- part of it, not all, as a popup does --
//    and counts the taps it takes, until `tap_overlay_clear`.
//
//  `tap_reset` clears every counter and puts every cell back. Laid out with
//  fixed frames, because the cells move and the test reasons about where they
//  were. Everything fits in 512 pt below the navigation bar, so an iPhone SE
//  shows it all above the tab bar.
//

import UIKit

final class TapsViewController: UIViewController {
    // About a tap's round trip through quern, so a read and the tap that
    // follows it can straddle a jump; a one-off slide had always finished
    // before quern read the tree.
    static let slidePeriod: TimeInterval = 0.12
    static let remountGap: TimeInterval = 1.5
    static let overlayDelay: TimeInterval = 0.5

    static let width: CGFloat = 160
    static let height: CGFloat = 44

    private var counts: [String: Int] = [:]
    private var labels: [String: UILabel] = [:]
    private var actions: [String: () -> Void] = [:]
    private let board = UIView()
    private var slider: UIButton!
    private var remount: UIButton?
    private var overlay: UIButton!
    private var slideTimer: Timer?
    // Bumped to drop a pending remount or overlay: by arming again, by a
    // clear, and by reset.
    private var remountToken = 0
    private var overlayToken = 0

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        board.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(board)
        NSLayoutConstraint.activate([
            board.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            board.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            board.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            board.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
        ])

        let heading = UILabel(frame: CGRect(x: 16, y: 0, width: 200, height: 32))
        heading.text = "Taps"
        heading.font = .preferredFont(forTextStyle: .headline)
        heading.accessibilityIdentifier = "taps_heading"
        board.addSubview(heading)

        // Target and decoy, side by side.
        counted("Target", "tap_target", x: 16, y: 36)
        counted("Decoy", "tap_decoy", x: 200, y: 36)

        // Twins: the same label twice.
        counted("Twin", "tap_twin_a", x: 16, y: 104)
        counted("Twin", "tap_twin_b", x: 200, y: 104)

        // Slide: the behind button is where the slider starts.
        counted("Behind", "tap_slide_behind", x: 16, y: 172)
        slider = counted("Slider", "tap_slider", x: 16, y: 172, countY: 236)
        control("Arm slide", "tap_slide_arm", x: 200, y: 172) { [weak self] in self?.armSlide() }

        // Remount: its counter stays put while the button is replaced.
        counter("tap_remount", x: 16, y: 312)
        placeRemount(x: 16, y: 268)
        control("Arm remount", "tap_remount_arm", x: 200, y: 268) { [weak self] in
            self?.armRemount()
        }

        // Late overlay, over the middle of its target.
        counted("Covered", "tap_overlay_target", x: 16, y: 364, countY: 420)
        control("Arm overlay", "tap_overlay_arm", x: 200, y: 364) { [weak self] in
            self?.armOverlay()
        }
        control("Clear overlay", "tap_overlay_clear", x: 200, y: 412) { [weak self] in
            self?.clearOverlay()
        }
        overlay = button("Overlay", "tap_overlay", x: 48, y: 372, width: 140)
        overlay.configuration?.baseBackgroundColor =
            UIColor(red: 0.8, green: 0.27, blue: 0.27, alpha: 1)
        counter("tap_overlay", x: 16, y: 440)

        control("Reset", "tap_reset", x: 16, y: 468) { [weak self] in self?.reset() }
        reset()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        stopSlide()
    }

    /// A button that counts its taps into `<id>_count`.
    private func button(_ text: String, _ id: String, x: CGFloat, y: CGFloat,
                        width: CGFloat = width) -> UIButton {
        let button = plainButton(text, id, x: x, y: y, width: width, style: .filled())
        actions[id] = { [weak self] in
            guard let self else { return }
            self.counts[id, default: 0] += 1
            self.render(id)
        }
        return button
    }

    private func plainButton(_ text: String, _ id: String, x: CGFloat, y: CGFloat,
                             width: CGFloat = width,
                             style: UIButton.Configuration) -> UIButton {
        var config = style
        config.title = text
        let button = UIButton(configuration: config)
        button.frame = CGRect(x: x, y: y, width: width, height: Self.height)
        button.accessibilityIdentifier = id
        button.addTarget(self, action: #selector(tapped(_:)), for: .touchUpInside)
        board.addSubview(button)
        return button
    }

    private func counter(_ id: String, x: CGFloat, y: CGFloat) {
        let count = UILabel(frame: CGRect(x: x, y: y, width: Self.width, height: 20))
        count.accessibilityIdentifier = "\(id)_count"
        board.addSubview(count)
        labels[id] = count
    }

    @discardableResult
    private func counted(_ text: String, _ id: String, x: CGFloat, y: CGFloat,
                         countY: CGFloat? = nil) -> UIButton {
        let button = button(text, id, x: x, y: y)
        counter(id, x: x, y: countY ?? y + Self.height)
        return button
    }

    private func control(_ text: String, _ id: String, x: CGFloat, y: CGFloat,
                         action: @escaping () -> Void) {
        plainButton(text, id, x: x, y: y, style: .gray())
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
                ? CGAffineTransform(translationX: 184, y: 48) : .identity
        }
    }

    private func stopSlide() {
        slideTimer?.invalidate()
        slideTimer = nil
        slider?.transform = .identity
    }

    /// Replace the remount button with a new view at (x, y).
    private func placeRemount(x: CGFloat, y: CGFloat) {
        remount?.removeFromSuperview()
        remount = button("Remount", "tap_remount", x: x, y: y)
    }

    private func armRemount() {
        remount?.removeFromSuperview()
        remount = nil
        remountToken += 1
        let armed = remountToken
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.remountGap) { [weak self] in
            guard let self, self.remountToken == armed else { return }
            self.placeRemount(x: 200, y: 316)
        }
    }

    private func armOverlay() {
        overlayToken += 1
        let armed = overlayToken
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.overlayDelay) { [weak self] in
            guard let self, self.overlayToken == armed else { return }
            self.overlay.isHidden = false
        }
    }

    private func clearOverlay() {
        overlayToken += 1  // a clear before the overlay arrived cancels it too
        overlay.isHidden = true
    }

    private func reset() {
        remountToken += 1
        stopSlide()
        counts.removeAll()
        labels.keys.forEach(render)
        placeRemount(x: 16, y: 268)
        clearOverlay()
    }
}
