//
//  main.swift
//  QuernProbe
//
//  A deterministic, offline UIKit playground for exercising and demoing
//  Quern's device-automation features. Every interactive element carries a
//  stable accessibility identifier so tests interact by identifier, never
//  by coordinates.
//

import UIKit

/// Tab construction, shared by both lifecycles.
///
/// The app-delegate bundle builds this from `didFinishLaunching`; the
/// scene-based bundle builds it from `scene(_:willConnectTo:)`. Keeping one
/// definition is why the scene variant is a second Info.plist rather than a
/// second app.
enum ProbeTabs {
    static func makeRootViewController() -> UIViewController {
        let tabs: [(UIViewController, String, String)] = [
            // Order matters: an iPhone tab bar shows five items and moves the
            // rest into a More list, which keeps its own navigation stack and
            // is markedly more awkward to drive. The five the self-test
            // exercises on every run go on the bar; Location, Web and Diag are
            // reached through More.
            (TextInputViewController(), "Text", "keyboard"),
            (ControlsViewController(), "Controls", "switch.2"),
            (ScrollViewController(), "Scroll", "list.bullet"),
            (LinksViewController(), "Links", "link"),
            (LogsViewController(), "Logs", "text.alignleft"),
            (LocationViewController(), "Location", "location"),
            (WebViewController(), "Web", "globe"),
            (DiagViewController(), "Diag", "exclamationmark.triangle"),
        ]

        let tabBarController = UITabBarController()
        tabBarController.viewControllers = tabs.map { controller, title, icon in
            controller.title = title
            let nav = UINavigationController(rootViewController: controller)
            nav.tabBarItem = UITabBarItem(title: title, image: UIImage(systemName: icon), tag: 0)
            nav.tabBarItem.accessibilityIdentifier = "tab_\(title.lowercased())"
            return nav
        }
        return tabBarController
    }
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = ProbeTabs.makeRootViewController()
        window.makeKeyAndVisible()
        self.window = window

        // Cold launch on the app-delegate lifecycle. The URL arrives here in
        // launchOptions — and then application(_:open:options:) is called with
        // the same URL, so a cold launch delivers it TWICE. Measured: one
        // simctl openurl at a terminated app leaves link_count at 2.
        //
        // An earlier version of this comment said open(_:) "only fires for an
        // already-running app", which is the same thing the guide said until
        // this fixture disproved it. Both callbacks are kept precisely so the
        // double delivery stays visible rather than being deduplicated away.
        if let url = launchOptions?[.url] as? URL {
            DeepLinkStore.shared.record(url, via: "appdelegate:launchOptions")
        }
        return true
    }

    func application(_ app: UIApplication, open url: URL,
                     options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        DeepLinkStore.shared.record(url, via: "appdelegate:open")
        return true
    }

    func application(_ application: UIApplication,
                     continue userActivity: NSUserActivity,
                     restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        if userActivity.activityType == NSUserActivityTypeBrowsingWeb,
           let url = userActivity.webpageURL {
            DeepLinkStore.shared.record(url, via: "appdelegate:continue")
            return true
        }
        return false
    }

    #if SCENE_LIFECYCLE
    // Compile-time, not runtime. Merely *implementing* this method opts the app
    // into the scene lifecycle even with no UIApplicationSceneManifest in the
    // Info.plist — measured: the manifest-free bundle reported
    // scene:willConnectTo until this was removed from its build. A runtime
    // guard cannot help, because the decision is made from the method's
    // presence before any of our code runs.
    func application(_ application: UIApplication,
                     configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default", sessionRole: session.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
    #endif
}
