//
//  HostedApp.swift
//  QuernProbeHosted
//
//  The probe's UIKit screens, unchanged, in a SwiftUI TabView: the bundle
//  `./build.sh --hosted` builds, for running the conformance suite on a phone
//  with no WDA (#664).
//
//  Why it exists: on iOS 27 a full read of the accessibility tree -- the read
//  quern makes with no WDA -- crashes a UIKit app on a tab whose root is a
//  UINavigationController reached through a UITabBarController's More list
//  (#669). The probe's other bundles are exactly that, so every screen behind
//  More crashed. Under SwiftUI's TabView the same view controllers, each in a
//  UINavigationController, are read without a crash (measured on an iPhone 11,
//  iOS 27.2.1, all nine More screens). The screens and their identifiers are
//  the probe's own, so the contract the suite holds them to is unchanged.
//
//  Not built by default: `Sources/*.swift` does not reach this directory, and
//  this file replaces main.swift and SceneDelegate.swift, whose entry point it
//  would collide with.
//

import SwiftUI
import UIKit

/// One probe screen in a UINavigationController, as `ProbeTabs` builds each
/// tab, so its title and its own navigationItem (Widgets' Done) show.
struct HostedScreen: UIViewControllerRepresentable {
    let title: String
    let make: () -> UIViewController

    func makeUIViewController(context: Context) -> UIViewController {
        let controller = make()
        controller.title = title
        return UINavigationController(rootViewController: controller)
    }

    func updateUIViewController(_ controller: UIViewController, context: Context) {}
}

/// SwiftUI gives a tab item no identifier of its own -- `.accessibilityIdentifier`
/// on the tab or its Label is dropped, and the item reports its image's name --
/// so each bar item under the TabView gets the `tab_<title>` main.swift sets.
/// SwiftUI's tab bar controller holds no viewControllers, only the bar's items.
enum TabIdentifiers {
    static func apply() {
        DispatchQueue.main.async {
            for scene in UIApplication.shared.connectedScenes {
                for window in (scene as? UIWindowScene)?.windows ?? [] {
                    for item in tabBarController(in: window.rootViewController)?.tabBar.items ?? [] {
                        if let title = item.title {
                            item.accessibilityIdentifier = "tab_\(title.lowercased())"
                        }
                    }
                }
            }
        }
    }

    static func tabBarController(in controller: UIViewController?) -> UITabBarController? {
        guard let controller else { return nil }
        if let tabs = controller as? UITabBarController { return tabs }
        for child in controller.children {
            if let found = tabBarController(in: child) { return found }
        }
        return tabBarController(in: controller.presentedViewController)
    }
}

@main
struct HostedApp: App {
    /// The order main.swift uses, so the same five are on the bar.
    let tabs: [(String, String, () -> UIViewController)] = [
        ("Text", "keyboard", { TextInputViewController() }),
        ("Controls", "switch.2", { ControlsViewController() }),
        ("Scroll", "list.bullet", { ScrollViewController() }),
        ("Links", "link", { LinksViewController() }),
        ("Logs", "text.alignleft", { LogsViewController() }),
        ("Location", "location", { LocationViewController() }),
        ("Web", "globe", { WebViewController() }),
        ("Diag", "exclamationmark.triangle", { DiagViewController() }),
        ("State", "externaldrive", { StateViewController() }),
        ("Widgets", "slider.horizontal.3", { WidgetsViewController() }),
        ("Lists", "list.dash", { ListsViewController() }),
        ("Gestures", "hand.draw", { GesturesViewController() }),
        ("Lazy", "square.stack", { LazyListViewController() }),
        ("Taps", "hand.tap", { TapsViewController() }),
    ]

    var body: some Scene {
        WindowGroup {
            TabView {
                ForEach(tabs.indices, id: \.self) { i in
                    HostedScreen(title: tabs[i].0, make: tabs[i].2)
                        .ignoresSafeArea()
                        .tabItem { Label(tabs[i].0, systemImage: tabs[i].1) }
                }
            }
            .onAppear(perform: TabIdentifiers.apply)
            // SwiftUI's delivery paths, recorded as the other bundles record theirs.
            .onOpenURL { url in DeepLinkStore.shared.record(url, via: "swiftui:onOpenURL") }
            .onContinueUserActivity(NSUserActivityTypeBrowsingWeb) { activity in
                if let url = activity.webpageURL {
                    DeepLinkStore.shared.record(url, via: "swiftui:onContinueUserActivity")
                }
            }
        }
    }
}
