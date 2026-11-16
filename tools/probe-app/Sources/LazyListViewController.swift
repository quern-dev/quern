//
//  LazyListViewController.swift
//  QuernProbe
//
//  A long list the way iOS 26 draws one: lazy and deep. Off-screen rows are
//  absent from the accessibility tree until scrolled to -- unlike the Scroll
//  tab's UITableView, which reports all 200 cells wherever they are -- and the
//  rows sit below quern's shallow WDA read depth (12).
//
//  That combination is what Settings, a SwiftUI `List` and a
//  `UICollectionView` list all measured as on iOS 26, and what the scroll
//  sweep could see neither the target nor the movement of, until #475. The
//  Scroll tab could not show it: its rows are at depth 12 and always present.
//

import UIKit

final class LazyListViewController: UIViewController, UICollectionViewDataSource {
    static let rowCount = 200
    private var list: UICollectionView?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let layout = UICollectionViewCompositionalLayout.list(
            using: UICollectionLayoutListConfiguration(appearance: .plain))
        let list = UICollectionView(frame: view.bounds, collectionViewLayout: layout)
        list.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        list.accessibilityIdentifier = "lazy_list"
        list.dataSource = self
        list.register(UICollectionViewListCell.self, forCellWithReuseIdentifier: "row")
        view.addSubview(list)
        self.list = list
        // In the navigation bar, as the Scroll tab's are: there whatever row
        // the list is on, so the screen is known when it is scrolled -- its
        // rows come and go, and a read of the tree push has no list (#664).
        let top = UIBarButtonItem(title: "Top", style: .plain, target: self,
                                  action: #selector(scrollToTop))
        top.accessibilityIdentifier = "lazy_to_top"
        navigationItem.leftBarButtonItem = top
    }

    /// Back to row 0, without animation, as the Scroll tab's Top.
    @objc private func scrollToTop() {
        list?.scrollToItem(at: IndexPath(item: 0, section: 0), at: .top, animated: false)
    }

    func collectionView(_ collectionView: UICollectionView,
                        numberOfItemsInSection section: Int) -> Int {
        Self.rowCount
    }

    func collectionView(_ collectionView: UICollectionView,
                        cellForItemAt indexPath: IndexPath) -> UICollectionViewCell {
        let cell = collectionView.dequeueReusableCell(withReuseIdentifier: "row", for: indexPath)
        var content = (cell as? UICollectionViewListCell)?.defaultContentConfiguration()
            ?? UIListContentConfiguration.cell()
        content.text = "Lazy row \(indexPath.item)"
        cell.contentConfiguration = content
        cell.accessibilityIdentifier = "lazy_row_\(indexPath.item)"
        return cell
    }
}
