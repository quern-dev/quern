package com.quern.probe

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

/** 200 stable rows: scroll, swipe, and scroll-to-element against a known target. */
class ScrollFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?,
    ): View {
        val root = inflater.inflate(R.layout.fragment_scroll, container, false)
        val list = root.findViewById<RecyclerView>(R.id.scroll_list)
        list.layoutManager = LinearLayoutManager(requireContext())
        list.adapter = RowAdapter()
        return root
    }

    private class RowAdapter : RecyclerView.Adapter<RowAdapter.Holder>() {
        class Holder(val view: TextView) : RecyclerView.ViewHolder(view)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val tv = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_row, parent, false) as TextView
            return Holder(tv)
        }

        override fun onBindViewHolder(holder: Holder, position: Int) {
            // The label carries the index rather than the id, because
            // RecyclerView recycles views and a per-row resource id would not
            // survive. Tests match on text; row_199 is only reachable by
            // actually scrolling.
            holder.view.text = "row_$position"
            holder.view.contentDescription = "row_$position"
        }

        override fun getItemCount() = ROWS
    }

    companion object { const val ROWS = 200 }
}
