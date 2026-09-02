package com.quern.probe

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.fragment.app.Fragment

/**
 * A WebView with fixed local content and no network.
 *
 * This exists so the webview-automation work has a fixture whose DOM we control.
 * The open question it is meant to answer -- what an accessibility walk can and
 * cannot see inside a WebView -- currently depends on a third-party app whose
 * markup we cannot change.
 */
class WebFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_web, container, false)
        val web = root.findViewById<WebView>(R.id.web_view)
        web.settings.javaScriptEnabled = true
        WebView.setWebContentsDebuggingEnabled(true)
        web.loadDataWithBaseURL(null, CONTENT, "text/html", "utf-8", null)
        return root
    }

    companion object {
        // Deliberately plain: a heading, a labelled control, and a nested
        // element, so "did the walk descend into the document" has an
        // unambiguous answer.
        const val CONTENT = """
            <html><body style="font-family:-apple-system,sans-serif;padding:16px">
              <h1 id="web_heading">Probe Web Heading</h1>
              <p id="web_paragraph">Deterministic paragraph inside the WebView.</p>
              <button id="web_button" onclick="document.getElementById('web_result').innerText='clicked'">
                Web Button
              </button>
              <div id="web_result">unclicked</div>
              <div><span id="web_nested">nested-span-target</span></div>
            </body></html>
        """
    }
}
