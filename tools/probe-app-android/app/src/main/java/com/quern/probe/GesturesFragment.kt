package com.quern.probe

import android.content.Context
import android.os.Bundle
import android.util.AttributeSet
import android.view.GestureDetector
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.TextView
import androidx.fragment.app.Fragment
import kotlin.math.atan2
import kotlin.math.hypot

/**
 * What a pinch, rotation, two-finger pan, double tap and two-finger tap did, as
 * the platform's own detectors saw it (#252). The counterpart of the iOS probe
 * app's Gestures tab, with the same label text, so one live test reads both.
 *
 * Pinch is ScaleGestureDetector's accumulated factor. Rotation is the turn of
 * the line between the two fingers, positive clockwise on screen, as iOS
 * reports it. A two-finger pan is the fingers' midpoint moving; a pinch or a
 * turn leaves it where it was, so neither reports one. A single tap waits for
 * the double tap to time out, so two taps inside the interval count once.
 */
class GesturesFragment : Fragment() {

    private val labels = mutableMapOf<String, TextView>()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_gestures, container, false)
        labels["pinch"] = root.findViewById(R.id.gesture_pinch)
        labels["rotate"] = root.findViewById(R.id.gesture_rotate)
        labels["pan"] = root.findViewById(R.id.gesture_pan)
        labels["tap"] = root.findViewById(R.id.gesture_tap)
        labels["double_tap"] = root.findViewById(R.id.gesture_double_tap)
        labels["two_finger_tap"] = root.findViewById(R.id.gesture_two_finger_tap)
        labels["long_press"] = root.findViewById(R.id.gesture_long_press)
        labels["back"] = root.findViewById(R.id.gesture_back)
        MainActivity.Backs.listener = { activity?.runOnUiThread { renderBacks() } }
        val pad = root.findViewById<GesturePadView>(R.id.gesture_pad)
        pad.onReport = { key, text -> labels[key]?.text = text }
        root.findViewById<Button>(R.id.gesture_reset).setOnClickListener {
            pad.reset()
            reset()
        }
        reset()
        return root
    }

    private fun reset() {
        labels["pinch"]?.text = "pinch -"
        labels["rotate"]?.text = "rotate -"
        labels["pan"]?.text = "pan -"
        labels["tap"]?.text = "tap 0"
        labels["double_tap"]?.text = "double 0"
        labels["two_finger_tap"]?.text = "twofinger 0"
        labels["long_press"]?.text = "long 0"
        MainActivity.Backs.count = 0
        renderBacks()
    }

    private fun renderBacks() {
        labels["back"]?.text = "back ${MainActivity.Backs.count}"
    }

    override fun onDestroyView() {
        MainActivity.Backs.listener = null
        super.onDestroyView()
    }
}

class GesturePadView(context: Context, attrs: AttributeSet?) : View(context, attrs) {

    var onReport: ((String, String) -> Unit)? = null

    private var taps = 0
    private var doubles = 0
    private var twoFingerTaps = 0
    private var longPresses = 0

    private var scale = 1f
    private var scaling = false
    private val scaleDetector = ScaleGestureDetector(context,
        object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScaleBegin(d: ScaleGestureDetector): Boolean {
                scale = 1f
                scaling = true
                return true
            }

            override fun onScale(d: ScaleGestureDetector): Boolean {
                scale *= d.scaleFactor
                report("pinch", "pinch %.2f changed".format(scale))
                return true
            }

            override fun onScaleEnd(d: ScaleGestureDetector) {
                scaling = false
                report("pinch", "pinch %.2f ended".format(scale))
            }
        })

    private val tapDetector = GestureDetector(context,
        object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent) = true

            override fun onSingleTapConfirmed(e: MotionEvent): Boolean {
                taps += 1
                report("tap", "tap $taps")
                return true
            }

            override fun onLongPress(e: MotionEvent) {
                longPresses += 1
                report("long_press", "long $longPresses")
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                doubles += 1
                report("double_tap", "double $doubles")
                return true
            }
        })

    // Two-finger state: from the moment a second finger lands to the moment
    // one lifts.
    private var twoDownAt = 0L
    private var lastAngle = 0.0
    private var turned = 0.0
    private var startMidX = 0f
    private var startMidY = 0f
    private var midX = 0f
    private var midY = 0f
    private var travelled = 0f
    private var startA = floatArrayOf(0f, 0f)
    private var startB = floatArrayOf(0f, 0f)
    private var tracking = false

    fun reset() {
        longPresses = 0
        taps = 0
        doubles = 0
        twoFingerTaps = 0
    }

    private fun report(key: String, text: String) {
        onReport?.invoke(key, text)
    }

    private fun angle(e: MotionEvent): Double =
        Math.toDegrees(atan2((e.getY(1) - e.getY(0)).toDouble(), (e.getX(1) - e.getX(0)).toDouble()))

    override fun onTouchEvent(e: MotionEvent): Boolean {
        scaleDetector.onTouchEvent(e)
        tapDetector.onTouchEvent(e)
        when (e.actionMasked) {
            MotionEvent.ACTION_POINTER_DOWN -> if (e.pointerCount == 2) {
                tracking = true
                twoDownAt = e.eventTime
                lastAngle = angle(e)
                turned = 0.0
                startMidX = (e.getX(0) + e.getX(1)) / 2
                startMidY = (e.getY(0) + e.getY(1)) / 2
                midX = startMidX
                midY = startMidY
                startA = floatArrayOf(e.getX(0), e.getY(0))
                startB = floatArrayOf(e.getX(1), e.getY(1))
                travelled = 0f
            }
            MotionEvent.ACTION_MOVE -> if (tracking && e.pointerCount >= 2) {
                val a = angle(e)
                var d = a - lastAngle
                while (d > 180) d -= 360
                while (d < -180) d += 360
                turned += d
                lastAngle = a
                midX = (e.getX(0) + e.getX(1)) / 2
                midY = (e.getY(0) + e.getY(1)) / 2
                travelled = maxOf(travelled,
                    hypot(e.getX(0) - startA[0], e.getY(0) - startA[1]),
                    hypot(e.getX(1) - startB[0], e.getY(1) - startB[1]))
            }
            MotionEvent.ACTION_POINTER_UP -> if (tracking) {
                tracking = false
                val dx = midX - startMidX
                val dy = midY - startMidY
                val quick = e.eventTime - twoDownAt < 400
                if (quick && travelled < 20f) {
                    twoFingerTaps += 1
                    report("two_finger_tap", "twofinger $twoFingerTaps")
                }
                if (kotlin.math.abs(turned) >= 5) {
                    report("rotate", "rotate %d ended".format(Math.round(turned).toInt()))
                }
                if (hypot(dx, dy) >= 10f) {
                    report("pan", "pan %d,%d ended".format(Math.round(dx), Math.round(dy)))
                }
            }
        }
        return true
    }
}
