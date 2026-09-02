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

    /// Which callback delivered the last link.
    ///
    /// The point of the scene-based bundle: a test can assert not just that the
    /// link arrived but that it arrived *the way the documentation says it
    /// does*. Without this the two lifecycles are indistinguishable from
    /// outside, and a guide claiming `scene(_:openURLContexts:)` handles warm
    /// delivery would read as verified when nothing had checked it.
    private(set) var lastRoute: String = "none"
    var onChange: (() -> Void)?

    func record(_ url: URL, via route: String) {
        lastURI = url.absoluteString
        lastRoute = route
        count += 1
        onChange?()
    }
}
