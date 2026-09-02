package com.quern.probe

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.TextView
import androidx.fragment.app.Fragment

/**
 * Every logging path Android offers, emitted only while explicitly running.
 *
 * Idle-by-default is deliberate. A fixture that logs continuously pollutes the
 * log stream during every *other* test in the same app, and log volume is
 * itself under test here. Making emission a mode the test starts and stops puts
 * that control on the correct side of the boundary.
 */
class LogsFragment : Fragment() {

    private val handler = Handler(Looper.getMainLooper())
    private var tick = 0
    private var running = false
    private var status: TextView? = null
    private var counter: TextView? = null

    private val emit = object : Runnable {
        override fun run() {
            if (!running) return
            tick += 1
            Log.v(TAG, "[PROBE-V] tick=$tick verbose")
            Log.d(TAG, "[PROBE-D] tick=$tick debug")
            Log.i(TAG, "[PROBE-I] tick=$tick info")
            Log.w(TAG, "[PROBE-W] tick=$tick warn")
            Log.e(TAG, "[PROBE-E] tick=$tick error")
            // stdout/stderr go to logcat only when the app is started with the
            // right flags on some API levels -- emitted anyway so a test can
            // tell the difference between "not captured" and "not written".
            println("[PROBE-STDOUT] tick=$tick println")
            System.err.println("[PROBE-STDERR] tick=$tick System.err")
            Log.e(TAG, "[PROBE-TRACE] tick=$tick", RuntimeException("probe stack trace"))
            counter?.text = tick.toString()
            handler.postDelayed(this, INTERVAL_MS)
        }
    }

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_logs, container, false)
        status = root.findViewById(R.id.log_status)
        counter = root.findViewById(R.id.log_tick_count)

        root.findViewById<Button>(R.id.log_start).setOnClickListener {
            if (!running) { running = true; status?.text = "running"; handler.post(emit) }
        }
        root.findViewById<Button>(R.id.log_stop).setOnClickListener {
            running = false
            handler.removeCallbacks(emit)
            status?.text = "stopped"
        }
        root.findViewById<Button>(R.id.log_burst).setOnClickListener {
            // One shot at every level, for tests that want a known, finite volume.
            repeat(BURST) { i -> Log.i(TAG, "[PROBE-BURST] $i of $BURST") }
        }
        return root
    }

    override fun onDestroyView() {
        running = false
        handler.removeCallbacks(emit)
        super.onDestroyView()
    }

    companion object {
        const val TAG = "QuernProbe"
        const val INTERVAL_MS = 2000L
        const val BURST = 20
    }
}
