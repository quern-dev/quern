//
//  DeepLinkStore.swift
//  QuernProbe
//
//  Records deep links that actually reached the app.
//
//  This exists because the tool cannot witness its own success. On Android,
//  `open_url` reports {"status": "ok"} whether or not anything handled the URL
//  (quern #78). iOS surfaces a failure, but only for an unregistered scheme —
//  a URL that opens Safari instead of the app also "succeeds". Either way the
//  honest signal is whether the app saw the intent, so tests assert here rather
//  than on the tool's response.
//

import Foundation

final class DeepLinkStore {
    static let shared = DeepLinkStore()

    private(set) var lastURI: String = "none"
    private(set) var count: Int = 0
    var onChange: (() -> Void)?

    func record(_ url: URL) {
        lastURI = url.absoluteString
        count += 1
        onChange?()
    }
}
