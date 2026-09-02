//
//  LinksViewController.swift
//  QuernProbe
//
//  Deep link landing surface. `link_count` is the assertion target; see
//  DeepLinkStore for why it is not the tool's own status.
//

import UIKit

final class LinksViewController: UIViewController {
    private let lastURILabel = UILabel()
    private let countLabel = UILabel()
    private let routeLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let width = view.bounds.width - 40
        var y: CGFloat = 120

        let caption = UILabel(frame: CGRect(x: 20, y: y, width: width, height: 24))
        caption.text = "Last URI received:"
        view.addSubview(caption)
        y += 30

        lastURILabel.frame = CGRect(x: 20, y: y, width: width, height: 44)
        lastURILabel.accessibilityIdentifier = "link_last_uri"
        lastURILabel.numberOfLines = 2
        lastURILabel.adjustsFontSizeToFitWidth = true
        view.addSubview(lastURILabel)
        y += 60

        let countCaption = UILabel(frame: CGRect(x: 20, y: y, width: width, height: 24))
        countCaption.text = "Links received:"
        view.addSubview(countCaption)
        y += 30

        countLabel.frame = CGRect(x: 20, y: y, width: width, height: 40)
        countLabel.accessibilityIdentifier = "link_count"
        countLabel.font = .systemFont(ofSize: 28, weight: .semibold)
        view.addSubview(countLabel)
        y += 50

        let routeCaption = UILabel(frame: CGRect(x: 20, y: y, width: width, height: 24))
        routeCaption.text = "Delivered via:"
        view.addSubview(routeCaption)
        y += 28

        routeLabel.frame = CGRect(x: 20, y: y, width: width, height: 40)
        routeLabel.accessibilityIdentifier = "link_route"
        routeLabel.numberOfLines = 2
        routeLabel.adjustsFontSizeToFitWidth = true
        view.addSubview(routeLabel)

        DeepLinkStore.shared.onChange = { [weak self] in
            DispatchQueue.main.async { self?.render() }
        }
        render()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        render()
    }

    private func render() {
        lastURILabel.text = DeepLinkStore.shared.lastURI
        countLabel.text = String(DeepLinkStore.shared.count)
        routeLabel.text = DeepLinkStore.shared.lastRoute
    }
}
