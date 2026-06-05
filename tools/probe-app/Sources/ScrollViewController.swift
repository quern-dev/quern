//
//  ScrollViewController.swift
//  QuernProbe
//
//  A long deterministic list for scroll/swipe testing and scroll-to-element
//  flows. Row identifiers are stable (row_0 ... row_199) so tests can assert
//  visibility of specific rows before and after scrolling.
//

import UIKit

final class ScrollViewController: UITableViewController {
    private let rowCount = 200

    override func viewDidLoad() {
        super.viewDidLoad()
        tableView.accessibilityIdentifier = "scroll_table"
        tableView.register(UITableViewCell.self, forCellReuseIdentifier: "cell")
    }

    override func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int {
        rowCount
    }

    override func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let cell = tableView.dequeueReusableCell(withIdentifier: "cell", for: indexPath)
        cell.textLabel?.text = "Row \(indexPath.row)"
        cell.accessibilityIdentifier = "row_\(indexPath.row)"
        return cell
    }
}
