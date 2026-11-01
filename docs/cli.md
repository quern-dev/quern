# Command reference

Every `quern` command, and the files quern keeps in `~/.quern/`. The everyday ones are in the [README](../README.md#run); `quern help` prints the same list.

`tests/test_readme_sync.py` checks the command block below against the commands the CLI actually dispatches, and every flag shown on this page against the flags it accepts, so a new command or flag lands here or fails the suite.

## Commands

Startup is idempotent — running `start` when a server is already running is a no-op. Port conflicts are handled automatically by scanning upward. The MCP server is auto-rebuilt on start when the TypeScript source is newer than the compiled output.

```bash
quern setup                  # Check environment, install deps
quern start                  # Daemonize
quern start -f               # Foreground
quern stop                   # Graceful shutdown
quern restart                # Stop + start
quern status                 # Show PID, URL, uptime, tool availability
quern url                    # Print the server's base URL, for scripts
quern env                    # Print shell exports: eval "$(quern env)"
quern doctor                 # Diagnostics: device tools, venv, tool versions, service health, MCP clients
quern doctor --fix           # ...and reconcile the venv with pyproject.toml (venv only)
quern capture-env            # Write an environment report to attach to a bug report
quern help                   # Show the command list
quern version                # Print the installed version
quern check-updates          # Ask now, ignoring the once-a-day rate limit
quern update                 # Update to the latest release on your channel and rebuild
quern update --tools         # Also upgrade external tools quern installed (pipx, brew)
quern set-channel [name]     # Show or set the update channel (stable / beta)
quern record start --udid UDID [--out DIR]   # Record a device's actions, flows and logs to disk
quern record start --udid UDID --kinds flows,logs --host H --exclude-host H --include-unattributed --video
quern record start --udid UDID --allow-passthrough   # Record even if the simulator lacks quern's CA (no HTTPS captured)
quern record start --udid UDID --kinds flows --bodies errors --max-body-bytes 4096 --exclude-content-type image/
                             # A small recording of a long run: every flow, few bodies
quern record start --udid UDID --video --viewer   # Also print a LAN viewer link to post (needs viewer.enabled)
quern record stop ID [--require-complete]    # ...until stopped; exit 3 if anything was lost
quern record keyframe ID [--label TEXT]   # Make this moment a seek point, and mark it in the recording
quern record start --udid UDID --video --keyframes requests   # Seek points at requests only (default: actions and requests)
quern record start --udid UDID --requested-by NAME   # Name who asked, so runs on a shared server tell apart
quern record list [--requested-by NAME]   # Recordings running on this server (only NAME's)
quern record download ID [--dest DIR]   # Copy a recording's files here, from a remote quern too (QUERN_REMOTE)
quern session run --reserve N [--device-type simulator] -- CMD ...   # Hold a session, reserve devices for CMD, release on exit
quern session run --udid UDID [--on-release shutdown_booted] -- CMD   # ...exactly these; shut down what it booted afterwards
quern session run --group GROUP [--reserve N] [--name NAME] -- CMD   # A config.json device group; name the session
quern session run --reserve N --os-version 18 --device-name "iPhone 16" --no-boot -- CMD   # Criteria; only already-booted devices
quern session hold NAME [--background] [--remote REMOTE]   # Hold session NAME's socket for raw REST calls; prints its id
quern session list           # Held sessions, their state (live / grace) and devices
quern session release ID     # End a session and free its devices
quern clients add LABEL      # Issue a client key for another caller (printed once)
quern clients list           # Issued client keys: created, last used, revoked
quern clients revoke LABEL   # Revoke one; its sessions end and its devices are freed
quern pair LABEL             # A one-time code for another machine to pair with (remote listener)
quern connect URL --code CODE   # On that machine: pair, and remember the server for QUERN_REMOTE
quern connect URL --key KEY --fingerprint SHA256   # ...or remember one whose key was issued by hand
quern remote status          # The remote listener here: its address and certificate
quern remote rotate-cert     # A new certificate for it; every client pairs again
quern remote list [--json] [--no-check] [--devices] [--with-local]   # The remote querns this machine is paired with, each asked now: up or down, version, free devices; --devices lists each one (OS, model, state, who holds it); --with-local lists this Mac's server first
quern remote forget NAME     # Forget one
quern set-update-check [on|off]
                             # Show or set the automatic daily update check
                             #   (default: on). check-updates works either way.
quern set-auto-install-cert [on|off]
                             # Show or set whether the capture certificate is installed
                             #   automatically, or Quern asks first (default: ask)
quern uninstall              # Remove Quern and dependencies installed by setup
quern regenerate-key         # New API key
quern mcp-install            # Register MCP server with Claude Code
quern grant-full-perms       # Allow all Quern MCP tools in Claude Code without prompting
quern install-precommit-hook # Install the pre-commit checklist hook
quern menubar [status]       # The Quern app's version, and whether it is running
quern menubar open           # Start the Quern app in the menu bar (a running one is left alone)
quern menubar install [--force]
                             # Install the signed app matching this quern and start it;
                             #   how a git install gets a newer app
quern enable-local-capture [--skip-cert-check] [--whole-mac] [process ...]
                             # Enable transparent simulator traffic capture. A booted
                             #   simulator that does not trust the capture certificate
                             #   has its TLS passed through, not decrypted;
                             #   --skip-cert-check is deprecated
quern disable-local-capture  # Disable local capture (never refused)
quern tunneld <cmd>          # Manage the tunneld LaunchDaemon (install/uninstall/status/restart,
                             #   grant-recovery/revoke-recovery for password-free wedge recovery)
```

`~/.quern/state.json` is the single source of truth for discovering a running instance.

## State files

The server prints connection info on startup — URL, API key, and proxy port. All state is stored in `~/.quern/`:

| File | Purpose |
|------|---------|
| `state.json` | Running instance info (port, PID, API key) — deleted on stop |
| `active-device.json` | The active device set via `resolve_device` — its UDID, name and type — persists across stop/start so you don't have to re-resolve after every restart, and is what the Quern app reads. Each agent session's own active device is kept here too, under `sessions`, for a day of inactivity (#254) |
| `cert-state.json` | Per-device certificate installation state, including per-SSID Wi-Fi proxy configs — persists across restarts |
| `device-pool.json` | Device pool state (simctl cache) — persists across restarts |
| `config.json` | Local capture processes, update channel, the automatic-update-check and certificate-install settings, and the sessions, remote listener, viewer and upload settings |
| `remotes.json` | The remote quern servers this machine is paired with (0600): read by `quern remote list`, `QUERN_REMOTE` and `use_remote` |
| `session-proxies.json` | Running session proxies' pids (`add_session_capture`), so a crashed server's leftovers are stopped at the next start |
| `session-hold-<id>.log` | The log of a `quern session hold --background` |
| `update-info.json` | The last update check's result — what the menu bar reads to offer an update |
| `last-update-check` | When the automatic check last ran, so it runs at most once a day |
| `last-update.json` | What `quern update` last did — updated, nothing to do, or failed |
| `installed-by-setup.json` | Packages installed by `quern setup` — used by `quern uninstall` |
| `tool-sites.json` | Where `quern setup` found each external tool, so a later run can tell a moved install from a missing one |
| `build-records/` | One directory per build (`<build id>/record.json`), plus a device build's dSYMs, so a crash can be symbolised against the binary that produced it |
| `crashes/` | Crash reports the watcher collects; a phone's own pulls go in `crashes/devices/<device id>/` so two devices' reports cannot be confused |
| `api-key` | Persistent API key |
| `server.log` | Daemon log output |
