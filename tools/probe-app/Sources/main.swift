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

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
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

        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = tabBarController
        window.makeKeyAndVisible()
        self.window = window

        // A URL supplied at launch arrives here rather than through
        // application(_:open:options:), which only fires for an already-running
        // app. Missing this is how a cold-launch deep link goes unrecorded.
        if let url = launchOptions?[.url] as? URL {
            DeepLinkStore.shared.record(url)
        }
        return true
    }

    func application(_ app: UIApplication, open url: URL,
                     options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        DeepLinkStore.shared.record(url)
        return true
    }

    func application(_ application: UIApplication,
                     continue userActivity: NSUserActivity,
                     restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        // Universal links arrive as an activity rather than an openURL call.
        if userActivity.activityType == NSUserActivityTypeBrowsingWeb,
           let url = userActivity.webpageURL {
            DeepLinkStore.shared.record(url)
            return true
        }
        return false
    }
}
