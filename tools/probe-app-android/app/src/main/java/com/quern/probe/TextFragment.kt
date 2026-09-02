package com.quern.probe

import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.TextView
import androidx.fragment.app.Fragment

/** Typing fidelity across keyboard types, with an echo of what actually landed. */
class TextFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_text, container, false)
        val echo = root.findViewById<TextView>(R.id.text_event_log)
        // The echo exists because "the field contains X" and "the app was told
        // about X" are different claims, and typing bugs have shown up as the
        // second failing while the first passed.
        listOf(
            R.id.field_default, R.id.field_url, R.id.field_email, R.id.field_password,
        ).forEach { id ->
            root.findViewById<EditText>(id).addTextChangedListener(object : TextWatcher {
                override fun afterTextChanged(s: Editable?) {
                    echo.text = "${resources.getResourceEntryName(id)}=${s ?: ""}"
                }
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            })
        }
        return root
    }
}
