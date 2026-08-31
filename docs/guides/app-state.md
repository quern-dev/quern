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

**Optionally, the simulator keychain** — see below. Off by default.

**What's NOT included:**
- **Push notification registration** — Server-side (APNs).
- **System permissions** — Camera, location, etc. are managed by iOS, not the app. Use your agent to grant these separately.

## Logged-In Checkpoints and the Keychain

Auth tokens live in the simulator keychain at `<device>/data/Library/Keychains/`,
which sits *outside* every app container. A checkpoint of containers alone
therefore always restores to a **logged-out** app, however it was captured — so
a checkpoint you named `logged-in-with-data` will land you on the login screen.

The failure is quiet rather than loud. The containers come back carrying the
app's own "keychain is ready" flags, so the app believes it holds credentials
that are gone, and the result reads as a confusing auth bug rather than as
missing state.

To capture it, ask for the keychain explicitly:

> "Save the current state as 'logged-in', including the keychain"

Driving the API yourself? That is the `include_keychain` flag on the
`save_app_state` MCP tool, or on `POST /api/v1/device/app/state/save`. It
defaults to false, so an existing checkpoint keeps behaving exactly as it did.

Restore puts it back automatically whenever the checkpoint carries one.
`restore_app_state` takes the same flag — pass `include_keychain: false` to
restore the containers but deliberately start logged out. Both
halves have to come from the same captured moment — a keychain restored onto a
signed-out container leaves you logged out just the same.

**The device must be shut down for both save and restore.** The keychain is a
WAL-mode SQLite database held open by `securityd`: copying it while the
simulator is booted yields a torn snapshot, and writing it beneath a running
`securityd` is simply ignored. Shut down first:

```sh
xcrun simctl shutdown <udid>
```

The preconditions are checked *before* anything is wiped, so calling against a
booted device fails cleanly and tells you the exact command to run — it will not
leave a half-restored app behind.

Checkpoints saved without the keychain keep working unchanged, and report
`keychain.restored: false` with a reason so the "why am I logged out" case
explains itself.

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
