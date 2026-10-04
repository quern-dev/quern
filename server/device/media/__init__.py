"""Screen media: live preview windows, the media engine behind them, and
screenshot processing.

`preview` runs the iOS preview window and `scrcpy_preview` the Android one;
`media_engine` builds the QuernMedia helper they stream through; `screenshots`
scales and annotates captured frames.

Nothing is re-exported here: import from the module that defines a name.
"""
