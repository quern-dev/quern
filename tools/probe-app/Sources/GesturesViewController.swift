//
//  GesturesViewController.swift
//  QuernProbe
//
//  A pad that reports every gesture it recognises, for the input primitives
//  quern synthesises beyond tap and swipe (#252): pinch, rotate, two-finger
//  pan, double tap, two-finger tap and scroll. Each result is a label with a
//  stable identifier, so a test reads what the app saw from the UI tree rather
//  than from a screenshot.
//
//  The recognisers run simultaneously, as they do in a map or a photo viewer:
//  a pinch that also turns slightly reports both, and a test asserts on the
//  one it meant. A single tap waits for the double tap to fail, so two taps
//  that land inside the double-tap interval count once, as a double -- which
//  is what tells a real double tap from two `tap` calls. A two-finger tap
//  waits for pinch, rotate and pan to fail, for the same reason.
//

import UIKit

final class GesturesViewController: UIViewController, UIGestureRecognizerDelegate {
    private let pad = UIView()
    private let pinchLabel = UILabel()
    private let rotateLabel = UILabel()
    private let panLabel = UILabel()
    private let scrollLabel = UILabel()
    private let tapLabel = UILabel()
    private let doubleTapLabel = UILabel()
    private let twoFingerTapLabel = UILabel()
    private let longPressLabel = UILabel()

    private var taps = 0
    private var doubleTaps = 0
    private var twoFingerTaps = 0
    private var longPresses = 0

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let width = view.bounds.width - 40
        var y: CGFloat = 110
        for (label, id) in [
            (pinchLabel, "gesture_pinch"), (rotateLabel, "gesture_rotate"),
            (panLabel, "gesture_pan"), (scrollLabel, "gesture_scroll"),
            (tapLabel, "gesture_tap"), (doubleTapLabel, "gesture_double_tap"),
            (twoFingerTapLabel, "gesture_two_finger_tap"),
            (longPressLabel, "gesture_long_press"),
        ] {
            label.frame = CGRect(x: 20, y: y, width: width, height: 22)
            label.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
            label.accessibilityIdentifier = id
            view.addSubview(label)
            y += 24
        }

        let reset = UIButton(type: .system)
        reset.frame = CGRect(x: 20, y: y, width: 80, height: 32)
        reset.setTitle("Reset", for: .normal)
        reset.accessibilityIdentifier = "gesture_reset"
        reset.addTarget(self, action: #selector(resetAll), for: .touchUpInside)
        view.addSubview(reset)
        y += 40

        // The pad fills what is left above the tab bar, so a gesture centred
        // on it has room to spread in every direction.
        let bottom = view.bounds.height - 100
        pad.frame = CGRect(x: 20, y: y, width: width, height: max(200, bottom - y))
        pad.backgroundColor = .secondarySystemBackground
        pad.accessibilityIdentifier = "gesture_pad"
        pad.isAccessibilityElement = true
        pad.accessibilityLabel = "Gesture pad"
        view.addSubview(pad)

        let pinch = UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:)))
        let rotate = UIRotationGestureRecognizer(target: self, action: #selector(rotated(_:)))
        let pan = UIPanGestureRecognizer(target: self, action: #selector(panned(_:)))
        pan.minimumNumberOfTouches = 2
        // Scroll events only: no touches at all, so a one-finger drag is not
        // mistaken for a scroll.
        let scroll = UIPanGestureRecognizer(target: self, action: #selector(scrolled(_:)))
        scroll.allowedScrollTypesMask = .all
        scroll.maximumNumberOfTouches = 0
        let doubleTap = UITapGestureRecognizer(target: self, action: #selector(doubleTapped))
        doubleTap.numberOfTapsRequired = 2
        let twoFingerTap = UITapGestureRecognizer(target: self, action: #selector(twoFingerTapped))
        twoFingerTap.numberOfTouchesRequired = 2
        let longPress = UILongPressGestureRecognizer(target: self, action: #selector(longPressed(_:)))
        let tap = UITapGestureRecognizer(target: self, action: #selector(tapped))
        tap.require(toFail: doubleTap)
        // A long press is not also a tap (#251).
        tap.require(toFail: longPress)
        // UIKit's two-finger tap also accepts a quick pinch or turn -- measured
        // here up to a 1s pinch, and with the fingers' midpoint moving 20pt --
        // so it waits for the moving gestures to fail, as an app with both
        // would. Without that every pinch also counted a two-finger tap.
        for moving in [pinch, rotate, pan] as [UIGestureRecognizer] {
            twoFingerTap.require(toFail: moving)
        }

        for recogniser in [pinch, rotate, pan, scroll, doubleTap, twoFingerTap, tap,
                           longPress] as [UIGestureRecognizer] {
            recogniser.delegate = self
            pad.addGestureRecognizer(recogniser)
        }
        resetAll()
    }

    func gestureRecognizer(_ g: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
        true
    }

    private func phase(_ state: UIGestureRecognizer.State) -> String {
        switch state {
        case .began: return "began"
        case .changed: return "changed"
        case .ended: return "ended"
        case .cancelled: return "cancelled"
        case .failed: return "failed"
        default: return "possible"
        }
    }

    @objc private func pinched(_ g: UIPinchGestureRecognizer) {
        pinchLabel.text = String(format: "pinch %.2f %@", g.scale, phase(g.state))
    }

    @objc private func rotated(_ g: UIRotationGestureRecognizer) {
        rotateLabel.text = String(format: "rotate %.0f %@", g.rotation * 180 / .pi, phase(g.state))
    }

    @objc private func panned(_ g: UIPanGestureRecognizer) {
        let t = g.translation(in: pad)
        panLabel.text = String(format: "pan %.0f,%.0f %@", t.x, t.y, phase(g.state))
    }

    @objc private func scrolled(_ g: UIPanGestureRecognizer) {
        let t = g.translation(in: pad)
        scrollLabel.text = String(format: "scroll %.0f,%.0f %@", t.x, t.y, phase(g.state))
    }

    @objc private func tapped() {
        taps += 1
        tapLabel.text = "tap \(taps)"
    }

    @objc private func doubleTapped() {
        doubleTaps += 1
        doubleTapLabel.text = "double \(doubleTaps)"
    }

    @objc private func twoFingerTapped() {
        twoFingerTaps += 1
        twoFingerTapLabel.text = "twofinger \(twoFingerTaps)"
    }

    @objc private func longPressed(_ g: UILongPressGestureRecognizer) {
        // Counted once, when it is recognised, not on every movement after.
        guard g.state == .began else { return }
        longPresses += 1
        longPressLabel.text = "long \(longPresses)"
    }

    @objc private func resetAll() {
        longPresses = 0
        longPressLabel.text = "long 0"
        taps = 0
        doubleTaps = 0
        twoFingerTaps = 0
        pinchLabel.text = "pinch -"
        rotateLabel.text = "rotate -"
        panLabel.text = "pan -"
        scrollLabel.text = "scroll -"
        tapLabel.text = "tap 0"
        doubleTapLabel.text = "double 0"
        twoFingerTapLabel.text = "twofinger 0"
    }
}
