# Before You Start

*`quern://app-knowledge-guide/before-you-start` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Prepare the device before launching the app. Skipping this causes unnecessary interruptions during the tour.

1. **Resolve a device.** Use `resolve_device` to find or boot a simulator (or connect a physical device).
2. **Pre-grant permissions.** On simulators, use `grant_permission` to pre-accept common permission prompts before launching:
   ```
   grant_permission bundle_id="..." permission="location-always"
   grant_permission bundle_id="..." permission="photos"
   grant_permission bundle_id="..." permission="camera"
   grant_permission bundle_id="..." permission="notifications"
   ```
   This prevents system alerts from interrupting the tour. Not all apps need all permissions — ask the user which ones the app requests, or grant the obvious ones and handle any remaining prompts as you encounter them.
   Note: `grant_permission` is simulator-only. On physical devices, you'll need to accept permission prompts via the UI during the tour.
3. **Disable password autofill.** On simulators, the password autofill/keychain dialog can appear over login fields and block interaction. Disable it before starting:
   ```
   # Navigate to Settings > Passwords > Password Options > toggle off AutoFill Passwords
   launch_app bundle_id="com.apple.Preferences"
   ```
   Or use `set_app_plist_value` if a simulator-level plist key is available. This is especially important for apps with login flows.
3. **Install the app** if needed (`install_app`) and verify with `list_apps`.
4. **Check proxy status** if network capture is relevant (`proxy_status`).
