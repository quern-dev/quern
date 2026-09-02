package com.quern.probe

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.SeekBar
import android.widget.Switch
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.fragment.app.Fragment

/** Stateful controls, each mirroring its value into a readable label. */
class ControlsFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_controls, container, false)
        val readout = root.findViewById<TextView>(R.id.control_readout)

        val sw = root.findViewById<Switch>(R.id.control_switch)
        sw.setOnCheckedChangeListener { _, on -> readout.text = "switch=$on" }

        root.findViewById<CheckBox>(R.id.control_checkbox)
            .setOnCheckedChangeListener { _, on -> readout.text = "checkbox=$on" }

        // Mirrored into a label because a SeekBar exposes its value through
        // accessibility inconsistently across API levels; the label is stable.
        root.findViewById<SeekBar>(R.id.control_slider)
            .setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar?, v: Int, fromUser: Boolean) {
                    root.findViewById<TextView>(R.id.control_slider_value).text = v.toString()
                }
                override fun onStartTrackingTouch(sb: SeekBar?) {}
                override fun onStopTrackingTouch(sb: SeekBar?) {}
            })

        root.findViewById<Button>(R.id.control_show_alert).setOnClickListener {
            AlertDialog.Builder(requireContext())
                .setTitle("Probe Alert")
                .setMessage("Deterministic alert body.")
                .setPositiveButton("OK") { d, _ -> d.dismiss(); readout.text = "alert=ok" }
                .setNegativeButton("Cancel") { d, _ -> d.dismiss(); readout.text = "alert=cancel" }
                .show()
        }
        return root
    }
}
