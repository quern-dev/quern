# App State Management

Save and restore complete snapshots of your iOS app's state on simulators. Think of it as checkpoints — save "logged in with test data," save "empty first launch," and switch between them in seconds.

## How to Use It

Save a checkpoint:

> "Save the current state of my app as 'logged-in-with-data'"

Restore later:

> "Restore my app to the 'logged-in-with-data' state"

List saved checkpoints:

> "What app states do I have saved?"

Your agent terminates the app before save/restore (to avoid corrupted state), then copies or restores the entire data container.

## What Gets Saved

**Everything the app writes to disk:**
- Documents, Library, tmp directories
- UserDefaults (just a plist in the container)
- Core Data stores (SQLite files)
- Downloaded files, caches
- App group containers (shared data with extensions/widgets)

**What's NOT included:**
- **Keychain items** — iOS Keychain is a system service, not part of the app container. Saved credentials, tokens stored in Keychain won't be captured.
- **Push notification registration** — Server-side (APNs).
- **System permissions** — Camera, location, etc. are managed by iOS, not the app. Use your agent to grant these separately.

## Plist Operations

Many apps store settings and feature flags in plist files. Your agent can read and modify these without launching the app:

> "Show me the app's preferences plist"
> "Set the feature_flags.dark_mode key to true"
> "Delete the cached_token key"

This is incredibly useful for:
- **Toggling feature flags** without rebuilding
- **Inspecting cached data** to understand app state
- **Clearing specific values** without wiping everything

If a plist is in an app group container (shared with an extension), tell your agent which group to target.

## Useful Patterns

### Reproducible Bug Testing

1. Get the app into the state that triggers the bug
2. **"Save this state as 'bug-repro'"**
3. Make your fix, rebuild, install
4. **"Restore to 'bug-repro'"** and verify the fix
5. Repeat as needed — instant reproduction every time

### Clean Slate Testing

1. Install the app fresh
2. **"Save this state as 'fresh-install'"**
3. Test various flows, accumulate state
4. **"Restore to 'fresh-install'"** — back to zero instantly

### Testing State Transitions

1. **"Save as 'before-migration'"**
2. Update to the new version, launch (migration runs)
3. Check results
4. **"Restore to 'before-migration'"** — rerun the migration with different data

### Feature Flag Testing

> "Set feature_flags.new_onboarding to true in the app's preferences, then launch the app"

No rebuild needed. Flip flags, restart, see the difference.

## Limitations

- **Simulator only.** Physical device app containers aren't accessible from the Mac.
- **Keychain not included.** If your app stores auth tokens in Keychain (which it should), they won't be part of the checkpoint. You may need to re-authenticate after restoring.
- **Large containers = large checkpoints.** Apps with big caches or downloaded content produce big snapshots. There's no selective backup.
- **Container UUID rotation.** If you uninstall and reinstall the app, the container gets a new UUID. Restore handles this by re-resolving paths, but if your app stores absolute paths internally, they may break.
