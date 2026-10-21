package com.quern.probe

import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.FrameLayout
import android.widget.TextView
import androidx.fragment.app.Fragment

/**
 * A tap-target oracle (#511): which element a tap actually reached.
 *
 * A passing tap check usually means the tap returned `ok` and the next read
 * looked plausible -- and a tap on the wrong element often looks the same.
 * Every button here counts its own clicks, so a test reads which one the tap
 * hit instead of inferring it:
 *
 * - **Target / decoy:** side by side, each counted.
 * - **Twins:** two buttons with the same label, for the ambiguity case.
 * - **Slide:** after `tap_slide_arm`, `tap_slider` jumps between its start and
 *   a spot down and to the right every 120 ms, until reset. `tap_slide_behind`
 *   sits where it started, so a tap aimed at a stale position counts there.
 *   It never settles, so a check that waits for a target to stop moving has
 *   to give up rather than tap.
 * - **Remount:** after `tap_remount_arm`, `tap_remount` is removed and added
 *   back 1.5 s later, shifted down -- the same label on a new view.
 * - **Late overlay:** 500 ms after `tap_overlay_arm`, an overlay covers
 *   `tap_overlay_target` and counts the taps it takes, until `tap_overlay_clear`.
 *
 * `tap_reset` clears every counter and puts every cell back. Laid out in code
 * with fixed positions, because the cells move and the test reasons about
 * where they were.
 */
class TapsFragment : Fragment() {

    companion object {
        // About a tap's round trip through quern on an emulator, so a read and
        // the tap that follows it often straddle a jump. A one-off slide had
        // always finished before quern read the tree, so it never raced.
        const val SLIDE_PERIOD_MS = 120L
        const val REMOUNT_GAP_MS = 1500L
        const val OVERLAY_DELAY_MS = 500L
    }

    private val counts = mutableMapOf<String, Int>()
    private val labels = mutableMapOf<String, TextView>()
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var root: FrameLayout
    private lateinit var slider: Button
    private lateinit var remount: Button
    private lateinit var overlay: Button

    private fun dp(v: Int) = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics,
    ).toInt()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        root = FrameLayout(requireContext())
        root.contentDescription = "tap_oracle"

        heading("Taps", "taps_heading", 8)

        // Target and decoy, side by side.
        counted("Target", "tap_target", x = 16, y = 56)
        counted("Decoy", "tap_decoy", x = 200, y = 56)

        // Twins: the same label twice.
        counted("Twin", "tap_twin_a", x = 16, y = 156)
        counted("Twin", "tap_twin_b", x = 200, y = 156)

        // Slide: the behind button is where the slider starts.
        counted("Behind", "tap_slide_behind", x = 16, y = 256)
        slider = counted("Slider", "tap_slider", x = 16, y = 256, countY = 328)
        control("Arm slide", "tap_slide_arm", x = 200, y = 256) { armSlide() }

        // Remount.
        remount = counted("Remount", "tap_remount", x = 16, y = 380)
        control("Arm remount", "tap_remount_arm", x = 200, y = 380) { armRemount() }

        // Late overlay over its target.
        counted("Covered", "tap_overlay_target", x = 16, y = 480)
        control("Arm overlay", "tap_overlay_arm", x = 200, y = 480) { armOverlay() }
        control("Clear overlay", "tap_overlay_clear", x = 200, y = 530) {
            overlay.visibility = View.GONE
        }
        overlay = counted("Overlay", "tap_overlay", x = 16, y = 480,
                          background = 0xFFCC4444.toInt(), countY = 552)
        overlay.visibility = View.GONE

        control("Reset", "tap_reset", x = 16, y = 610) { reset() }
        reset()
        return root
    }

    private fun place(view: View, x: Int, y: Int, w: Int = 160, h: Int = 48) {
        val params = FrameLayout.LayoutParams(dp(w), dp(h))
        params.leftMargin = dp(x)
        params.topMargin = dp(y)
        root.addView(view, params)
    }

    private fun heading(text: String, id: String, y: Int) {
        val tv = TextView(requireContext())
        tv.text = text
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
        tv.tag = id
        tv.id = resourceId(id)
        place(tv, 16, y, w = 200, h = 40)
    }

    /** A button that counts its clicks into a label beneath it: `<id>_count`. */
    private fun counted(
        text: String, id: String, x: Int, y: Int, background: Int? = null,
        countY: Int = y + 48,
    ): Button {
        val button = Button(requireContext())
        button.isAllCaps = false   // labels read as on iOS: "Target", not "TARGET"
        button.text = text
        button.tag = id
        button.id = resourceId(id)
        background?.let { button.setBackgroundColor(it); button.setTextColor(Color.WHITE) }
        place(button, x, y)
        val count = TextView(requireContext())
        count.gravity = Gravity.CENTER_VERTICAL
        count.id = resourceId("${id}_count")
        place(count, x, countY, h = 24)
        labels[id] = count
        button.setOnClickListener {
            counts[id] = (counts[id] ?: 0) + 1
            render(id)
        }
        return button
    }

    private fun control(text: String, id: String, x: Int, y: Int, action: () -> Unit) {
        val button = Button(requireContext())
        button.isAllCaps = false
        button.text = text
        button.id = resourceId(id)
        button.setOnClickListener { action() }
        place(button, x, y)
    }

    /** A real resource id, so uiautomator sees `com.quern.probe:id/<name>`. */
    private fun resourceId(name: String): Int {
        val id = resources.getIdentifier(name, "id", requireContext().packageName)
        return if (id != 0) id else View.generateViewId()
    }

    private fun render(id: String) {
        labels[id]?.text = "${id.removePrefix("tap_")} ${counts[id] ?: 0}"
    }

    private val slide = object : Runnable {
        override fun run() {
            val away = slider.translationX == 0f
            slider.translationX = if (away) dp(184).toFloat() else 0f
            slider.translationY = if (away) dp(80).toFloat() else 0f
            handler.postDelayed(this, SLIDE_PERIOD_MS)
        }
    }

    private fun armSlide() {
        handler.removeCallbacks(slide)
        handler.post(slide)
    }

    private fun armRemount() {
        val params = remount.layoutParams as FrameLayout.LayoutParams
        root.removeView(remount)
        handler.postDelayed({
            params.topMargin = dp(404)
            root.addView(remount, params)
        }, REMOUNT_GAP_MS)
    }

    private fun armOverlay() {
        handler.postDelayed({ overlay.visibility = View.VISIBLE }, OVERLAY_DELAY_MS)
    }

    private fun reset() {
        handler.removeCallbacksAndMessages(null)
        counts.clear()
        labels.keys.forEach { render(it) }
        slider.translationX = 0f
        slider.translationY = 0f
        if (remount.parent == null) {
            val params = remount.layoutParams as FrameLayout.LayoutParams
            root.addView(remount, params)
        }
        (remount.layoutParams as FrameLayout.LayoutParams).topMargin = dp(380)
        remount.requestLayout()
        overlay.visibility = View.GONE
    }
}
