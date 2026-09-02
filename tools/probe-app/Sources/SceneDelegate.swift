//
//  SceneDelegate.swift
//  QuernProbe
//
//  The scene-based URL delivery path, used only by the QuernProbeScene bundle.
//
//  Why a second bundle rather than a second app: an app either has a scene
//  manifest or it does not, and the choice decides which callbacks iOS calls.
//  There is no way to exercise both in one running binary. Two Info.plists over
//  one set of sources keeps the view controllers, the tabs and the self-test
//  shared, which is the same reasoning that folded LogTester into this app
//  rather than leaving it standing alone.
//
//  This exists because the guidance in docs/guides/deep-link-testing.md about
//  scene-based delivery was written from documentation rather than from
//  measurement. These callbacks are the claim; running them is the evidence.
//

import UIKit

@objc(SceneDelegate)
final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene,
               willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }

        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = ProbeTabs.makeRootViewController()
        window.makeKeyAndVisible()
        self.window = window

        // Cold launch. The URL is on connectionOptions, not delivered through
        // scene(_:openURLContexts:) — that only fires for a scene that is
        // already connected. An app handling only the latter loses every link
        // that starts it from cold, silently.
        for context in connectionOptions.urlContexts {
            DeepLinkStore.shared.record(context.url, via: "scene:willConnectTo")
        }
        for activity in connectionOptions.userActivities
        where activity.activityType == NSUserActivityTypeBrowsingWeb {
            if let url = activity.webpageURL {
                DeepLinkStore.shared.record(url, via: "scene:connectionOptions.userActivities")
            }
        }
    }

    // Warm delivery: the scene is already connected.
    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        for context in URLContexts {
            DeepLinkStore.shared.record(context.url, via: "scene:openURLContexts")
        }
    }

    // Universal links into a running scene.
    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        if userActivity.activityType == NSUserActivityTypeBrowsingWeb,
           let url = userActivity.webpageURL {
            DeepLinkStore.shared.record(url, via: "scene:continue")
        }
    }
}
