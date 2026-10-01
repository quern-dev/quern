//
//  ListsViewController.swift
//  QuernProbe
//
//  List and grid structure for the same comparison as WidgetsViewController:
//  table sections with a header and footer, the cell accessory types, and a
//  collection view. Short enough to sit on screen whole (#336).
//

import UIKit

final class ListsViewController: UIViewController {
    private let rows: [(String, UITableViewCell.AccessoryType)] = [
        ("Disclosure row", .disclosureIndicator),
        ("Checkmark row", .checkmark),
        ("Detail row", .detailButton),
    ]

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let table = UITableView(
            frame: CGRect(x: 0, y: 100, width: view.bounds.width, height: 300),
            style: .insetGrouped,
        )
        table.dataSource = self
        table.delegate = self
        table.isScrollEnabled = false
        table.accessibilityIdentifier = "lists_table"
        table.register(UITableViewCell.self, forCellReuseIdentifier: "row")
        view.addSubview(table)

        let layout = UICollectionViewFlowLayout()
        layout.itemSize = CGSize(width: 80, height: 60)
        layout.minimumInteritemSpacing = 10
        let grid = UICollectionView(
            frame: CGRect(x: 20, y: 420, width: view.bounds.width - 40, height: 140),
            collectionViewLayout: layout,
        )
        grid.dataSource = self
        grid.isScrollEnabled = false
        grid.accessibilityIdentifier = "lists_grid"
        grid.register(GridCell.self, forCellWithReuseIdentifier: "tile")
        view.addSubview(grid)
    }
}

extension ListsViewController: UITableViewDataSource, UITableViewDelegate {
    func numberOfSections(in tableView: UITableView) -> Int { 1 }

    func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int {
        rows.count
    }

    func tableView(_ tableView: UITableView, titleForHeaderInSection section: Int) -> String? {
        "Section header"
    }

    func tableView(_ tableView: UITableView, titleForFooterInSection section: Int) -> String? {
        "Section footer"
    }

    func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let cell = tableView.dequeueReusableCell(withIdentifier: "row", for: indexPath)
        let (title, accessory) = rows[indexPath.row]
        var content = cell.defaultContentConfiguration()
        content.text = title
        cell.contentConfiguration = content
        cell.accessoryType = accessory
        cell.accessibilityIdentifier = "lists_row_\(indexPath.row)"
        return cell
    }
}

extension ListsViewController: UICollectionViewDataSource {
    func collectionView(_ collectionView: UICollectionView, numberOfItemsInSection section: Int) -> Int { 6 }

    func collectionView(_ collectionView: UICollectionView, cellForItemAt indexPath: IndexPath) -> UICollectionViewCell {
        let cell = collectionView.dequeueReusableCell(withReuseIdentifier: "tile", for: indexPath)
        (cell as? GridCell)?.label.text = "Tile \(indexPath.item)"
        cell.accessibilityIdentifier = "lists_tile_\(indexPath.item)"
        return cell
    }
}

private final class GridCell: UICollectionViewCell {
    let label = UILabel()

    override init(frame: CGRect) {
        super.init(frame: frame)
        contentView.backgroundColor = .secondarySystemBackground
        label.frame = contentView.bounds
        label.textAlignment = .center
        label.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        contentView.addSubview(label)
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}
