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
 * Every button here counts its own clicks into a label, `<id>_count`, so a
 * test reads which one the tap hit instead of inferring it. The identifiers,
 * labels and layout match the iOS probe's Taps tab, so one test reads both:
 *
 * - **Target / decoy:** side by side, each counted.
 * - **Twins:** two buttons with the same label, for the ambiguity case.
 * - **Slide:** after `tap_slide_arm`, `tap_slider` jumps between its start and
 *   the spot below `tap_slide_arm` every 120 ms, until reset or the tab is
 *   left. `tap_slide_behind` sits where it started, so a tap aimed at a stale
 *   position counts there or reaches nothing.
 * - **Remount:** `tap_remount_arm` removes `tap_remount`, and 1.5 s later a new
 *   button with the same id and label appears a column over and a row down.
 *   Nothing is left where the old one was, so a tap at its old position
 *   reaches nothing.
 * - **Late overlay:** 500 ms after `tap_overlay_arm`, an overlay covers the
 *   centre of `tap_overlay_target` -- part of it, not all, as a popup does --
 *   and counts the taps it takes, until `tap_overlay_clear`.
 *
 * `tap_reset` clears every counter and puts every cell back. Laid out in code
 * with fixed positions, because the cells move and the test reasons about
 * where they were. Everything fits in 512 dp, so a small phone shows it all.
 */
class TapsFragment : Fragment() {

    companion object {
        // About a tap's round trip through quern on an emulator, so a read and
        // the tap that follows it can straddle a jump; a one-off slide had
        // always finished before quern read the tree, so it never raced.
        const val SLIDE_PERIOD_MS = 120L
        const val REMOUNT_GAP_MS = 1500L
        const val OVERLAY_DELAY_MS = 500L

        const val W = 160
        const val H = 44
    }

    private val counts = mutableMapOf<String, Int>()
    private val labels = mutableMapOf<String, TextView>()
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var root: FrameLayout
    private lateinit var slider: Button
    private var remount: Button? = null
    private lateinit var overlay: Button

    private fun dp(v: Int) = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics,
    ).toInt()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        root = FrameLayout(requireContext())

        val heading = TextView(requireContext())
        heading.text = "Taps"
        heading.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        heading.id = resourceId("taps_heading")
        place(heading, 16, 0, w = 200, h = 32)

        // Target and decoy, side by side.
        counted("Target", "tap_target", x = 16, y = 36)
        counted("Decoy", "tap_decoy", x = 200, y = 36)

        // Twins: the same label twice.
        counted("Twin", "tap_twin_a", x = 16, y = 104)
        counted("Twin", "tap_twin_b", x = 200, y = 104)

        // Slide: the behind button is where the slider starts.
        counted("Behind", "tap_slide_behind", x = 16, y = 172)
        slider = counted("Slider", "tap_slider", x = 16, y = 172, countY = 236)
        control("Arm slide", "tap_slide_arm", x = 200, y = 172) { armSlide() }

        // Remount: its counter stays put while the button is replaced.
        counter("tap_remount", x = 16, y = 312)
        placeRemount(16, 268)
        control("Arm remount", "tap_remount_arm", x = 200, y = 268) { armRemount() }

        // Late overlay, over the middle of its target.
        counted("Covered", "tap_overlay_target", x = 16, y = 364, countY = 420)
        control("Arm overlay", "tap_overlay_arm", x = 200, y = 364) { armOverlay() }
        control("Clear overlay", "tap_overlay_clear", x = 200, y = 412) { clearOverlay() }
        overlay = button("Overlay", "tap_overlay", x = 48, y = 372, w = 140)
        overlay.setBackgroundColor(0xFFCC4444.toInt())
        overlay.setTextColor(Color.WHITE)
        counter("tap_overlay", x = 16, y = 440)

        control("Reset", "tap_reset", x = 16, y = 468) { reset() }
        reset()
        return root
    }

    override fun onPause() {
        super.onPause()
        // The pager keeps every page alive; a slide left running would jump
        // forever behind the other tabs.
        stopSlide()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        handler.removeCallbacksAndMessages(null)
    }

    private fun place(view: View, x: Int, y: Int, w: Int = W, h: Int = H) {
        val params = FrameLayout.LayoutParams(dp(w), dp(h))
        params.leftMargin = dp(x)
        params.topMargin = dp(y)
        root.addView(view, params)
    }

    /** A button that counts its clicks into `<id>_count`. */
    private fun button(text: String, id: String, x: Int, y: Int, w: Int = W): Button {
        val button = Button(requireContext())
        button.isAllCaps = false   // labels read as on iOS: "Target", not "TARGET"
        button.text = text
        button.id = resourceId(id)
        button.setOnClickListener {
            counts[id] = (counts[id] ?: 0) + 1
            render(id)
        }
        place(button, x, y, w = w)
        return button
    }

    private fun counter(id: String, x: Int, y: Int) {
        val count = TextView(requireContext())
        count.gravity = Gravity.CENTER_VERTICAL
        count.id = resourceId("${id}_count")
        place(count, x, y, h = 20)
        labels[id] = count
    }

    private fun counted(text: String, id: String, x: Int, y: Int, countY: Int = y + H): Button {
        val button = button(text, id, x, y)
        counter(id, x, countY)
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

    /**
     * A real resource id, so uiautomator sees `com.quern.probe:id/<name>`.
     * Every name is declared in res/values/ids.xml; a missing one fails here
     * rather than becoming an anonymous view a test cannot find.
     */
    private fun resourceId(name: String): Int {
        val id = resources.getIdentifier(name, "id", requireContext().packageName)
        check(id != 0) { "no id/$name in res/values/ids.xml" }
        return id
    }

    private fun render(id: String) {
        labels[id]?.text = "${id.removePrefix("tap_")} ${counts[id] ?: 0}"
    }

    private val slide = object : Runnable {
        override fun run() {
            val away = slider.translationX == 0f
            slider.translationX = if (away) dp(184).toFloat() else 0f
            slider.translationY = if (away) dp(48).toFloat() else 0f
            handler.postDelayed(this, SLIDE_PERIOD_MS)
        }
    }

    private fun armSlide() {
        handler.removeCallbacks(slide)
        handler.post(slide)
    }

    private fun stopSlide() {
        handler.removeCallbacks(slide)
        slider.translationX = 0f
        slider.translationY = 0f
    }

    /** Replace the remount button with a new view at (x, y). */
    private fun placeRemount(x: Int, y: Int) {
        remount?.let { root.removeView(it) }
        remount = button("Remount", "tap_remount", x, y)
    }

    private val remountAway = Runnable { placeRemount(200, 316) }

    private fun armRemount() {
        remount?.let { root.removeView(it) }
        remount = null
        // Arming again during the gap restarts it rather than queueing a second.
        handler.removeCallbacks(remountAway)
        handler.postDelayed(remountAway, REMOUNT_GAP_MS)
    }

    private val showOverlay = Runnable { overlay.visibility = View.VISIBLE }

    private fun armOverlay() {
        handler.removeCallbacks(showOverlay)
        handler.postDelayed(showOverlay, OVERLAY_DELAY_MS)
    }

    private fun clearOverlay() {
        // A clear before the overlay arrived cancels it too.
        handler.removeCallbacks(showOverlay)
        overlay.visibility = View.GONE
    }

    private fun reset() {
        handler.removeCallbacksAndMessages(null)
        counts.clear()
        labels.keys.forEach { render(it) }
        stopSlide()
        placeRemount(16, 268)
        overlay.visibility = View.GONE
    }
}
