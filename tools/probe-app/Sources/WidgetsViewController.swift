//
//  WidgetsViewController.swift
//  QuernProbe
//
//  One of each standard control the other screens lack, all on screen at once,
//  for comparing how the accessibility tree (sim-bridge, idb) and XCUITest
//  (WDA) name the same element (#336). Nothing here is interactive beyond what
//  the control does by itself: the screen exists to be read, not driven.
//
//  On screen at once because a comparison is only fair if both backends can
//  see every element. WDA's /source times out on a long list and falls back to
//  navigation chrome, so a scrolling screen would compare a full tree against
//  a fallback.
//

import UIKit

final class WidgetsViewController: UIViewController {
    private let pickerItems = ["Alpha", "Bravo", "Charlie", "Delta"]

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        // Placed on this screen's own navigation item, so it cannot leak onto
        // the More list's shared stack.
        let done = UIBarButtonItem(title: "Done", style: .done, target: nil, action: nil)
        done.accessibilityIdentifier = "widget_nav_done"
        navigationItem.rightBarButtonItem = done

        let width = view.bounds.width - 40
        var y: CGFloat = 110

        let search = UISearchBar(frame: CGRect(x: 10, y: y, width: width + 20, height: 50))
        search.placeholder = "Search widgets"
        search.accessibilityIdentifier = "widget_search"
        view.addSubview(search)
        y += 56

        let progress = UIProgressView(progressViewStyle: .default)
        progress.frame = CGRect(x: 20, y: y + 8, width: width - 50, height: 4)
        progress.progress = 0.4
        progress.accessibilityIdentifier = "widget_progress"
        progress.accessibilityLabel = "Upload progress"
        view.addSubview(progress)

        let spinner = UIActivityIndicatorView(style: .medium)
        spinner.frame = CGRect(x: width - 10, y: y, width: 20, height: 20)
        spinner.accessibilityIdentifier = "widget_activity"
        spinner.accessibilityLabel = "Loading"
        spinner.startAnimating()
        view.addSubview(spinner)
        y += 30

        let pages = UIPageControl(frame: CGRect(x: 20, y: y, width: width, height: 26))
        pages.numberOfPages = 4
        pages.currentPage = 1
        pages.accessibilityIdentifier = "widget_pages"
        view.addSubview(pages)
        y += 34

        let image = UIImageView(image: UIImage(systemName: "star.fill"))
        image.frame = CGRect(x: 20, y: y, width: 36, height: 36)
        image.isAccessibilityElement = true
        image.accessibilityLabel = "Favourite"
        image.accessibilityIdentifier = "widget_image"
        view.addSubview(image)

        let menuButton = UIButton(type: .system)
        menuButton.frame = CGRect(x: 70, y: y, width: 140, height: 36)
        menuButton.setTitle("Sort by", for: .normal)
        menuButton.accessibilityIdentifier = "widget_menu"
        menuButton.menu = UIMenu(children: [
            UIAction(title: "Name") { _ in },
            UIAction(title: "Date") { _ in },
        ])
        menuButton.showsMenuAsPrimaryAction = true
        view.addSubview(menuButton)

        let colour = UIColorWell(frame: CGRect(x: 230, y: y, width: 36, height: 36))
        colour.selectedColor = .systemTeal
        colour.title = "Tint"
        colour.accessibilityIdentifier = "widget_colour"
        view.addSubview(colour)
        y += 46

        let date = UIDatePicker(frame: CGRect(x: 20, y: y, width: width, height: 36))
        date.datePickerMode = .date
        date.preferredDatePickerStyle = .compact
        date.date = Date(timeIntervalSince1970: 1_767_225_600)  // fixed, so reads are stable
        date.accessibilityIdentifier = "widget_date"
        view.addSubview(date)
        y += 46

        let text = UITextView(frame: CGRect(x: 20, y: y, width: width, height: 66))
        text.text = "Multi-line notes\nsecond line"
        text.font = .systemFont(ofSize: 15)
        text.layer.borderColor = UIColor.separator.cgColor
        text.layer.borderWidth = 1
        text.accessibilityIdentifier = "widget_textview"
        view.addSubview(text)
        y += 74

        let picker = UIPickerView(frame: CGRect(x: 20, y: y, width: width, height: 130))
        picker.dataSource = self
        picker.delegate = self
        picker.accessibilityIdentifier = "widget_picker"
        view.addSubview(picker)
        y += 136

        // A standalone toolbar rather than the navigation controller's: this
        // screen is pushed onto the More list's shared stack, and turning that
        // stack's toolbar on would leave it on for the screens that follow.
        let toolbar = UIToolbar(frame: CGRect(x: 0, y: y, width: view.bounds.width, height: 44))
        let share = UIBarButtonItem(barButtonSystemItem: .action, target: nil, action: nil)
        share.accessibilityIdentifier = "widget_toolbar_share"
        let trash = UIBarButtonItem(barButtonSystemItem: .trash, target: nil, action: nil)
        trash.accessibilityIdentifier = "widget_toolbar_trash"
        toolbar.items = [share, UIBarButtonItem(systemItem: .flexibleSpace), trash]
        toolbar.accessibilityIdentifier = "widget_toolbar"
        view.addSubview(toolbar)
    }
}

extension WidgetsViewController: UIPickerViewDataSource, UIPickerViewDelegate {
    func numberOfComponents(in pickerView: UIPickerView) -> Int { 1 }

    func pickerView(_ pickerView: UIPickerView, numberOfRowsInComponent component: Int) -> Int {
        pickerItems.count
    }

    func pickerView(_ pickerView: UIPickerView, titleForRow row: Int, forComponent component: Int) -> String? {
        pickerItems[row]
    }
}
