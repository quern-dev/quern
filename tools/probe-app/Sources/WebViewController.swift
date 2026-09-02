//
//  WebViewController.swift
//  QuernProbe
//
//  A WKWebView with fixed local content and no network.
//
//  This exists so the webview-automation work has a fixture whose DOM we
//  control. The open question it serves — what a native accessibility walk can
//  and cannot see inside a WKWebView — currently depends on a third-party app
//  whose markup cannot be changed to isolate a variable.
//
//  isInspectable is set on iOS 16.4+, where WebKit stopped opting apps into
//  remote inspection by default. It belongs to the hosting app rather than the
//  content, which is why a fixture that sets it is the clean way to test the
//  Web Inspector transport.
//

import UIKit
import WebKit

final class WebViewController: UIViewController {
    private var webView: WKWebView!

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        webView = WKWebView(frame: CGRect(x: 0, y: 100,
                                          width: view.bounds.width,
                                          height: view.bounds.height - 100))
        webView.accessibilityIdentifier = "web_view"
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        webView.loadHTMLString(Self.content, baseURL: nil)
        view.addSubview(webView)
    }

    // Deliberately plain: a heading, a labelled control, and a nested element,
    // so "did the walk descend into the document" has an unambiguous answer.
    static let content = """
    <html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="font-family:-apple-system,sans-serif;padding:16px">
      <h1 id="web_heading">Probe Web Heading</h1>
      <p id="web_paragraph">Deterministic paragraph inside the web view.</p>
      <button id="web_button"
              onclick="document.getElementById('web_result').innerText='clicked'">
        Web Button
      </button>
      <div id="web_result">unclicked</div>
      <div><span id="web_nested">nested-span-target</span></div>
    </body></html>
    """
}
