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
            (TextInputViewController(), "Text", "keyboard"),
            (LocationViewController(), "Location", "location"),
            (ControlsViewController(), "Controls", "switch.2"),
            (ScrollViewController(), "Scroll", "list.bullet"),
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
        return true
    }
}
