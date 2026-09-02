package com.quern.probe

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.fragment.app.Fragment

/**
 * Deep link landing surface.
 *
 * `link_count` is the assertion target. Quern's `open_url` returns
 * `{"status": "ok"}` on Android whether or not anything handled the URL (#78),
 * so a test that trusts the tool's response proves nothing. Reading the counter
 * here answers the real question: did the intent reach an app?
 */
class LinksFragment : Fragment() {

    private var lastUri: TextView? = null
    private var count: TextView? = null

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_links, container, false)
        lastUri = root.findViewById(R.id.link_last_uri)
        count = root.findViewById(R.id.link_count)
        MainActivity.DeepLinks.listener = { activity?.runOnUiThread { render() } }
        render()
        return root
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onDestroyView() {
        MainActivity.DeepLinks.listener = null
        super.onDestroyView()
    }

    private fun render() {
        lastUri?.text = MainActivity.DeepLinks.lastUri
        count?.text = MainActivity.DeepLinks.count.toString()
    }
}
