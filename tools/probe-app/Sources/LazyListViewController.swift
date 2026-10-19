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
