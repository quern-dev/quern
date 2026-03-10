# App State Management

Save and restore complete snapshots of an iOS app's state on simulators. Think of it as checkpoints — save "logged in with test data", save "empty first launch", and switch between them instantly.

## How It Works

An iOS app's persistent state lives in its data container — a directory the OS assigns that holds `Documents/`, `Library/`, `tmp/`, and anything else the app writes to disk. Some apps also use app group containers for sharing data with extensions.

Quern copies these containers to a labeled checkpoint, and can restore them later by copying back.

### Save

```
save_app_state(
    udid="...",
    bundle_id="com.example.myapp",
    label="logged-in-with-data",
    description="User logged in, 3 items in cart, notifications enabled"
)
```

This:
1. Terminates the app (can't snapshot while it's writing)
2. Finds the data container via `simctl get_app_container`
3. Discovers app group containers by scanning the shared container directory
4. Copies everything to `~/.quern/app-states/com.example.myapp/logged-in-with-data/`
5. Writes metadata (timestamp, paths, description)

### Restore

```
restore_app_state(
    udid="...",
    bundle_id="com.example.myapp",
    label="logged-in-with-data"
)
```

This:
1. Terminates the app
2. Re-resolves live container paths (they may change after reinstall)
3. Wipes the current containers
4. Copies the checkpoint back

Launch the app and it's in exactly the state it was when you saved.

### List

```
list_app_states(bundle_id="com.example.myapp")
```

Returns all saved checkpoints with labels, descriptions, and timestamps.

### Delete

```
delete_app_state(bundle_id="com.example.myapp", label="logged-in-with-data")
```

## Plist Operations

Many iOS apps store settings and feature flags in plist files inside their data container. Quern can read and modify these without launching the app.

### Reading

```
read_app_plist(
    udid="...",
    bundle_id="com.example.myapp",
    path="Library/Preferences/com.example.myapp.plist"
)
```

Returns the plist contents as JSON. Handles both binary and XML plists transparently.

### Writing

```
set_app_plist_value(
    udid="...",
    bundle_id="com.example.myapp",
    path="Library/Preferences/com.example.myapp.plist",
    key="feature_flags.new_onboarding",
    value=true
)
```

Type is inferred from the value: booleans, integers, floats, and strings are supported.

### Deleting Keys

```
delete_app_plist_key(
    udid="...",
    bundle_id="com.example.myapp",
    path="Library/Preferences/com.example.myapp.plist",
    key="cached_token"
)
```

### App Group Containers

If the plist is in an app group container instead of the main data container, specify the group:

```
read_app_plist(
    udid="...",
    bundle_id="com.example.myapp",
    container="group.com.example.shared",
    path="Library/Preferences/group.com.example.shared.plist"
)
```

## Use Cases

### Reproducible Bug Testing

```
# Save a known state that triggers the bug
save_app_state(udid, bundle_id, label="bug-repro-state")

# Test the fix
# ... make code changes, rebuild, install ...

# Restore and verify
restore_app_state(udid, bundle_id, label="bug-repro-state")
launch_app(udid, bundle_id)
# Bug should be fixed now
```

### Testing State Transitions

```
# Save "before" state
save_app_state(udid, bundle_id, label="before-migration")

# Run the migration
launch_app(udid, bundle_id)
# ... app migrates data ...

# Compare: restore and re-run
restore_app_state(udid, bundle_id, label="before-migration")
launch_app(udid, bundle_id)
```

### Feature Flag Testing

```
# Toggle a feature flag without rebuilding
set_app_plist_value(udid, bundle_id,
    path="Library/Preferences/com.example.myapp.plist",
    key="feature_flags.dark_mode",
    value=true
)
launch_app(udid, bundle_id)
```

### Clean Slate Testing

```
# Save the fresh state right after install
install_app(udid, path="/path/to/MyApp.app")
save_app_state(udid, bundle_id, label="fresh-install")

# ... test things, app accumulates state ...

# Reset to fresh
restore_app_state(udid, bundle_id, label="fresh-install")
```

## What's Included (and What's Not)

**Included:**
- Data container: `Documents/`, `Library/`, `tmp/`, everything the app writes
- App group containers: Shared data with extensions and widgets
- UserDefaults (they're just a plist in `Library/Preferences/`)
- Core Data stores (SQLite files in the data container)
- Downloaded files, caches, anything in the filesystem

**Not included:**
- **Keychain items**: The iOS Keychain is a system-level service, not a file in the app container. Quern can't snapshot or restore keychain entries.
- **Push notification registration**: Stored server-side by APNs.
- **System permissions**: Camera, location, etc. are managed by the OS and not part of the app container. Use `grant_permission` to set these separately.

## Limitations

- **Simulator only.** Physical device app containers aren't accessible from the Mac.
- **Container UUID rotation.** If you uninstall and reinstall the app, the container UUID changes. Restore handles this by re-resolving paths, but if the app stores absolute paths internally, they may break.
- **Large containers.** Apps with large caches or downloaded content will produce large checkpoints. There's no selective backup — it's all or nothing.
- **Running app conflict.** Always terminate the app before save/restore. Quern does this automatically, but if the app has background processes or extensions, they may write during the copy.
