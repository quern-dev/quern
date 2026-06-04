//
//  LocationViewController.swift
//  QuernProbe
//
//  Live CoreLocation readout for verifying set_location and simulated
//  movement. Labels update on every location callback; the update counter
//  lets tests assert that movement produced a stream of updates rather
//  than a single fix.
//

import CoreLocation
import UIKit

final class LocationViewController: UIViewController {
    private let manager = CLLocationManager()
    private var updateCount = 0

    private let authLabel = UILabel()
    private let latitudeLabel = UILabel()
    private let longitudeLabel = UILabel()
    private let speedLabel = UILabel()
    private let timestampLabel = UILabel()
    private let countLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let rows: [(UILabel, String, String)] = [
            (authLabel, "location_auth", "authorization: unknown"),
            (latitudeLabel, "location_lat", "latitude: —"),
            (longitudeLabel, "location_lon", "longitude: —"),
            (speedLabel, "location_speed", "speed: —"),
            (timestampLabel, "location_time", "timestamp: —"),
            (countLabel, "location_count", "updates: 0"),
        ]

        var y: CGFloat = 120
        for (label, identifier, placeholder) in rows {
            label.frame = CGRect(x: 20, y: y, width: view.bounds.width - 40, height: 28)
            label.font = .monospacedSystemFont(ofSize: 14, weight: .regular)
            label.accessibilityIdentifier = identifier
            label.text = placeholder
            view.addSubview(label)
            y += 36
        }

        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.requestWhenInUseAuthorization()
        manager.startUpdatingLocation()
    }
}

extension LocationViewController: CLLocationManagerDelegate {
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status: String
        switch manager.authorizationStatus {
        case .authorizedWhenInUse: status = "whenInUse"
        case .authorizedAlways: status = "always"
        case .denied: status = "denied"
        case .restricted: status = "restricted"
        case .notDetermined: status = "notDetermined"
        @unknown default: status = "unknown"
        }
        authLabel.text = "authorization: \(status)"
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else {
            return
        }
        updateCount += 1
        latitudeLabel.text = "latitude: \(location.coordinate.latitude.formatted(.number.precision(.fractionLength(6))))"
        longitudeLabel.text = "longitude: \(location.coordinate.longitude.formatted(.number.precision(.fractionLength(6))))"
        speedLabel.text = "speed: \(location.speed.formatted(.number.precision(.fractionLength(2)))) m/s"
        timestampLabel.text = "timestamp: \(location.timestamp.formatted(date: .omitted, time: .standard))"
        countLabel.text = "updates: \(updateCount)"
    }
}
