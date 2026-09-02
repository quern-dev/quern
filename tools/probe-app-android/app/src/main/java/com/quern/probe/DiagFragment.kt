package com.quern.probe

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import androidx.fragment.app.Fragment

/**
 * Terminal actions, kept on their own tab and behind explicit taps.
 *
 * A crash ends the process and takes any in-progress test with it, so these run
 * in their own phase with a relaunch afterwards. That is an ordering problem,
 * not a reason to put them in a separate app.
 */
class DiagFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_diag, container, false)
        root.findViewById<Button>(R.id.diag_crash_uncaught).setOnClickListener {
            throw RuntimeException("QuernProbe deliberate crash (uncaught)")
        }
        root.findViewById<Button>(R.id.diag_crash_background).setOnClickListener {
            Thread { throw IllegalStateException("QuernProbe deliberate crash (background thread)") }.start()
        }
        root.findViewById<Button>(R.id.diag_anr).setOnClickListener {
            Thread.sleep(ANR_SLEEP_MS)
        }
        return root
    }

    companion object { const val ANR_SLEEP_MS = 12_000L }
}
