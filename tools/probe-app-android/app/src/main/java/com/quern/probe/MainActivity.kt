package com.quern.probe

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.fragment.app.Fragment
import androidx.viewpager2.adapter.FragmentStateAdapter
import androidx.viewpager2.widget.ViewPager2
import com.google.android.material.tabs.TabLayout
import com.google.android.material.tabs.TabLayoutMediator

/**
 * Quern Probe — a deterministic, offline fixture for exercising Quern's Android
 * tooling.
 *
 * Every interactive element carries a stable resource id, content is fixed, and
 * nothing touches the network. The point is to be a target that does not shift
 * underneath a test the way Settings or Chrome do.
 */
class MainActivity : AppCompatActivity() {

    /**
     * Deep links that actually reached the app.
     *
     * This is the whole reason the Links tab exists. `open_url` on Android
     * currently reports success even when nothing can handle the URL (#78),
     * so the only trustworthy signal is whether the app itself saw the intent.
     * A test asserts against this counter, not against the tool's status.
     */
    object DeepLinks {
        var lastUri: String = "none"
        var count: Int = 0
        var listener: (() -> Unit)? = null

        fun record(uri: String) {
            lastUri = uri
            count += 1
            listener?.invoke()
        }
    }

    private val tabs: List<Pair<String, () -> Fragment>> = listOf(
        "Text" to ::TextFragment,
        "Controls" to ::ControlsFragment,
        "Scroll" to ::ScrollFragment,
        "Logs" to ::LogsFragment,
        "Links" to ::LinksFragment,
        "Web" to ::WebFragment,
        "Diag" to ::DiagFragment,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        recordIfDeepLink(intent)

        val pager = findViewById<ViewPager2>(R.id.probe_pager)
        // The fixture is for automation, not for humans swiping: keep the pager
        // fixed so a stray horizontal swipe during a test cannot change tabs.
        pager.isUserInputEnabled = false
        pager.offscreenPageLimit = tabs.size
        pager.adapter = object : FragmentStateAdapter(this) {
            override fun getItemCount() = tabs.size
            override fun createFragment(position: Int) = tabs[position].second()
        }

        val tabLayout = findViewById<TabLayout>(R.id.probe_tabs)
        TabLayoutMediator(tabLayout, pager) { tab, position ->
            tab.text = tabs[position].first
            tab.contentDescription = "tab_${tabs[position].first.lowercase()}"
        }.attach()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        recordIfDeepLink(intent)
    }

    private fun recordIfDeepLink(intent: Intent?) {
        if (intent?.action == Intent.ACTION_VIEW) {
            intent.data?.let { DeepLinks.record(it.toString()) }
        }
    }
}
