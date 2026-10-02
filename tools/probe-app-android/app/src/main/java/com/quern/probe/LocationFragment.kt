package com.quern.probe

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import java.util.Locale

/**
 * Live location readout for verifying `set_location`, mirroring the iOS tab:
 * the same identifiers and the same label text, so one test reads both.
 *
 * The permission is not requested from here. A runtime dialog would sit over
 * every other tab's tests; the suite grants it with `grant_permission`, and
 * `location_auth` says which state the app is actually in, so a test that
 * forgot to grant it fails on that label rather than on a coordinate that
 * never arrives.
 */
class LocationFragment : Fragment(), LocationListener {

    private var updateCount = 0
    private var listening = false
    private val labels = mutableMapOf<Int, TextView>()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_location, container, false)
        for (id in listOf(
            R.id.location_auth, R.id.location_lat, R.id.location_lon,
            R.id.location_speed, R.id.location_time, R.id.location_count,
        )) {
            labels[id] = root.findViewById(id)
        }
        return root
    }

    // Re-checked on every resume: the suite grants the permission while the
    // app is running, and the tab has to notice without a relaunch.
    override fun onResume() {
        super.onResume()
        start()
    }

    override fun onDestroyView() {
        stop()
        super.onDestroyView()
    }

    private fun start() {
        val granted = ContextCompat.checkSelfPermission(
            requireContext(), Manifest.permission.ACCESS_FINE_LOCATION,
        ) == PackageManager.PERMISSION_GRANTED
        labels[R.id.location_auth]?.text =
            "authorization: ${if (granted) "granted" else "denied"}"
        if (!granted || listening) return
        val manager = requireContext().getSystemService(Context.LOCATION_SERVICE) as LocationManager
        try {
            // GPS, because `adb emu geo fix` feeds the GPS provider.
            manager.requestLocationUpdates(
                LocationManager.GPS_PROVIDER, 0L, 0f, this, Looper.getMainLooper(),
            )
            listening = true
            manager.getLastKnownLocation(LocationManager.GPS_PROVIDER)?.let { onLocationChanged(it) }
        } catch (e: SecurityException) {
            labels[R.id.location_auth]?.text = "authorization: denied"
        } catch (e: IllegalArgumentException) {
            // No GPS provider on this device: say so where a test will read it.
            labels[R.id.location_auth]?.text = "authorization: granted, no gps provider"
        }
    }

    private fun stop() {
        if (!listening) return
        val manager = context?.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
        manager?.removeUpdates(this)
        listening = false
    }

    override fun onLocationChanged(location: Location) {
        updateCount += 1
        labels[R.id.location_lat]?.text = "latitude: ${fmt(location.latitude, 6)}"
        labels[R.id.location_lon]?.text = "longitude: ${fmt(location.longitude, 6)}"
        labels[R.id.location_speed]?.text = "speed: ${fmt(location.speed.toDouble(), 2)} m/s"
        labels[R.id.location_time]?.text = "timestamp: ${location.time}"
        labels[R.id.location_count]?.text = "updates: $updateCount"
    }

    // Locale.US so a device set to a comma-decimal locale -- set_locale is
    // under test too -- does not change what the assertions read.
    private fun fmt(value: Double, places: Int) = String.format(Locale.US, "%.${places}f", value)

    // Required on API < 29, where these are abstract.
    @Deprecated("Deprecated in Java")
    override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    override fun onProviderEnabled(provider: String) {}
    override fun onProviderDisabled(provider: String) {}
}
