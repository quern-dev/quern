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

        // Reset controls live in the navigation bar, not in a header or a
        // toolbar above the table. A table header scrolls away with the
        // content, so the one moment you need "back to top" is the moment it
        // is off-screen -- and anything stacked above the table would shrink
        // the scroll viewport, which is the quantity under measurement in #84.
        // The nav bar already occupies its space either way.
        navigationItem.leftBarButtonItem = makeButton(
            title: "Top", identifier: "scroll_to_top", action: #selector(scrollToTop),
        )
        navigationItem.rightBarButtonItem = makeButton(
            title: "Bottom", identifier: "scroll_to_bottom", action: #selector(scrollToBottom),
        )
        updatePositionReadout()
    }

    private func makeButton(
        title: String, identifier: String, action: Selector,
    ) -> UIBarButtonItem {
        let item = UIBarButtonItem(title: title, style: .plain, target: self, action: action)
        item.accessibilityIdentifier = identifier
        return item
    }

    /// Jump to a known end of the list, without animation.
    ///
    /// Not animated deliberately. An animated jump is a scroll like any other:
    /// a test that resets and immediately reads the tree would sample the
    /// animation rather than the destination, which is the same class of
    /// timing bug the reset exists to remove from scroll tests.
    @objc private func scrollToTop() {
        guard rowCount > 0 else { return }
        tableView.scrollToRow(at: IndexPath(row: 0, section: 0), at: .top, animated: false)
        updatePositionReadout()
    }

    @objc private func scrollToBottom() {
        guard rowCount > 0 else { return }
        tableView.scrollToRow(
            at: IndexPath(row: rowCount - 1, section: 0), at: .bottom, animated: false,
        )
        updatePositionReadout()
    }

    /// Publish the visible row range, so a screenshot says where it is.
    ///
    /// The identifiers already tell an automated reader which rows are on
    /// screen. This is for the human looking at a failure screenshot, and for
    /// a trace that wants one short string per sample rather than a parsed
    /// tree: `rows 47-63 of 200`.
    private func updatePositionReadout() {
        let visible = tableView.indexPathsForVisibleRows?.map(\.row).sorted()
        let text: String
        if let visible, let lo = visible.first, let hi = visible.last {
            text = "rows \(lo)-\(hi) of \(rowCount)"
        } else {
            text = "rows - of \(rowCount)"
        }
        navigationItem.title = text
        // Carried on the table as well: the nav title is chrome and a caller
        // filtering the tree to the scroll container should not have to reach
        // outside it to learn where the container is.
        tableView.accessibilityValue = text
    }

    override func scrollViewDidScroll(_ scrollView: UIScrollView) {
        updatePositionReadout()
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
