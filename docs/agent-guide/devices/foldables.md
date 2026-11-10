# Foldable simulators

*`quern://guide/devices/foldables` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

On an iPhone Duo simulator (Xcode 27.1+), `set_hinge` folds and unfolds it with Device Hub's poses (`closed`, `open`, `flat`) or any `angle`, and `get_hinge` reads the angle and which panel is lit. Which panel lights depends on how the hinge moved, not only where it stopped, so read `lit_panel` in the answer rather than assuming; a pose whose panel didn't light carries a warning. Taps, the UI tree and screenshots follow the lit panel, and the next tap after a fold waits for the screen to settle. Edge swipes are refused on a Duo.
