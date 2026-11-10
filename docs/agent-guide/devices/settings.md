# Device settings

*`quern://guide/devices/settings` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

- **Text input on an iOS simulator:** `set_simulator_setting` turns off what rewrites typed text (`auto_correction`, `auto_capitalization`, `smart_punctuation`, `period_shortcut`) and the "Save Password?" sheet (`password_autofill`). A change takes a reboot, so on a booted simulator the call is refused unless you pass `reboot: true`; a shut-down simulator, or a setting already in place, needs none. Do it before launching the app under test; `get_simulator_settings` shows what's set (see `quern://guide/ui`).
- **Hardware keyboard:** `set_hardware_keyboard` attaches or detaches a simulator's hardware keyboard. Attached, the software keyboard is hidden, so trees are smaller and screenshots unobstructed. `type_text` attaches it as a side effect of sending keys, so detach it again if a later step expects the software keyboard.
- **Locale:** `set_locale` on Android (on API 33 and later it needs a rootable build) and on physical iPhones (which briefly restarts SpringBoard); iOS simulators aren't supported yet.
- **Android display:** `set_font_scale` (1.0 is the default, 1.30 the largest standard) and `set_display_density` (omit `dpi` to reset) take effect at once.
- **Location and permissions:** `set_location` on simulators and emulators; `grant_permission` on simulators and Android devices.
- **Buttons and links:** `press_button` presses hardware buttons (iOS: `HOME`, `LOCK`, `SIDE_BUTTON`...; Android: `home`, `back`, `recents`...). `open_url` opens any URL the device has a handler for: web links, deep links, universal links, `geo:`, and settings URIs.
- **Factory reset:** `erase_device` erases a simulator (it is left shut down) or an Android emulator (it reboots with `-wipe-data`, possibly on a new serial, so use the `udid` in the answer). It removes every app, all data and every setting, simulator settings included. Not for physical devices.
