# Linux Support Plan

## Goal

Support Android-only workflows on Linux. iOS features remain macOS-only (fundamental platform constraint — Xcode, simctl, CoreMediaIO don't exist on Linux). The server should detect the platform at startup and expose only the features available on that platform.

## Scope

**In scope (Linux Android-only):**
- Server startup, daemon mode, API, MCP
- Android device/emulator management (adb, uiautomator2)
- Android log capture (logcat)
- Android live preview (scrcpy)
- Network proxy (mitmproxy — already cross-platform)
- System proxy configuration (Linux equivalent)
- Install script and setup for Linux
- Certificate management for Android devices

**Out of scope:**
- iOS simulator/device support on Linux (impossible)
- CoreMediaIO preview (Apple-only framework)
- OSLog capture (macOS-only)
- Windows native support (separate effort, if ever)

## Platform Detection Strategy

Add a `server/platform.py` module that exposes the current platform and available capabilities:

```python
import platform
import shutil

MACOS = platform.system() == "Darwin"
LINUX = platform.system() == "Linux"

HAS_SIMCTL = MACOS and shutil.which("xcrun") is not None
HAS_ADB = shutil.which("adb") is not None
HAS_SCRCPY = shutil.which("scrcpy") is not None
```

Code that currently assumes macOS uses these flags to gate behavior. iOS-specific routers are only mounted when `HAS_SIMCTL` is true (which already happens implicitly in many places, but should be explicit).

## Changes by Component

### 1. Install Script (`quern.dev/public/_install.sh`)

**Current:** Hard-fails on non-Darwin.

**Change:**
- Accept Linux (`uname -s` == "Linux")
- On Linux, skip Homebrew check (warn about system packages instead)
- Use `apt`/`dnf`/`pacman` detection for prerequisite hints
- Same tarball download mechanism (already platform-agnostic)
- Same venv creation (Python's `venv` module is cross-platform)

**Effort:** Small. Most of the script is already platform-neutral.

### 2. Setup (`server/lifecycle/setup.py`)

**Current:** Warns on non-Darwin, checks for Xcode/Homebrew, installs iOS deps.

**Changes:**
| Check | macOS | Linux |
|-------|-------|-------|
| `check_platform()` | Accept Darwin | Accept Linux |
| `check_xcode_cli_tools()` | Keep | Skip |
| `check_homebrew()` | Keep | Skip (detect `apt`/`dnf`/`pacman` instead) |
| iOS deps (idb, libimobiledevice, etc.) | Keep | Skip |
| Android deps (adb, scrcpy) | Keep | Keep (install via system package manager) |
| `check_vpn()` | `route -n get default` | `ip route show default` |
| `check_crash_dialog()` | `defaults read` | Skip |
| `install_wrapper_script()` | Keep (`~/.local/bin`) | Keep (same path) |
| `_detect_shell_rc()` | Prefers .zshrc | Prefer .bashrc |
| Node.js install | `brew install node` | `apt install nodejs` / `dnf install nodejs` |
| mitmproxy install | `pipx install mitmproxy` | Same (pipx is cross-platform) |

**Effort:** Medium. Lots of conditional branches, but each one is straightforward.

### 3. System Proxy (`server/proxy/system_proxy.py`)

**Current:** Entirely macOS-specific — uses `route`, `networksetup`.

**Linux approach:**

```python
if MACOS:
    # existing networksetup logic
elif LINUX:
    # Option A: environment variables (works everywhere)
    #   Set http_proxy/https_proxy in the shell
    # Option B: gsettings (GNOME/GTK apps)
    #   gsettings set org.gnome.system.proxy mode 'manual'
    #   gsettings set org.gnome.system.proxy.http host '127.0.0.1'
    #   gsettings set org.gnome.system.proxy.http port 9101
    # Option C: Skip system proxy entirely
    #   On Linux, users typically configure proxy per-app or per-device
```

**Recommendation:** For Android device proxy setup, the user configures the device's Wi-Fi proxy to point at the host machine — same as macOS physical device setup. System proxy is less relevant on Linux since there's no simulator traffic to transparently capture. Implement a minimal version (environment variables + gsettings for GNOME) and document manual setup for other desktop environments.

**Effort:** Medium. The Android device proxy workflow is the same as macOS physical devices. System-wide proxy for the host machine is the complex part, but it's also less important for the Android use case.

### 4. Local Capture (`mitmproxy-macos` System Extension)

**Current:** macOS System Extension for transparent per-process capture.

**Linux equivalent:** mitmproxy supports `--mode transparent` on Linux using iptables/nftables (requires `CAP_NET_ADMIN`). However, per-process filtering is harder — Linux transparent proxy typically captures all traffic on a network namespace or interface, not per-process.

**Recommendation:** Skip local capture on Linux for now. Android devices are configured to proxy via Wi-Fi settings anyway — local capture is primarily useful for iOS simulators where you can't configure a proxy per-app.

**Effort:** None (skip).

### 5. Daemon Management

#### Server daemon (`server/lifecycle/daemon.py`)

**Current:** Uses `start_new_session=True` (POSIX setsid), double-fork pattern.

**Status:** Already cross-platform. No changes needed.

#### tunneld daemon (`server/device/tunneld.py`)

**Current:** Uses launchd plist (`/Library/LaunchDaemons/com.quern.tunneld.plist`).

**Linux approach:** tunneld is for iOS physical device USB tunneling via pymobiledevice3. This is iOS-only functionality.

**However:** pymobiledevice3 does work on Linux for some device operations (backup, diagnostics). If we want to support iOS device access from Linux in the future (unlikely but possible), we'd use systemd:

```ini
[Unit]
Description=Quern tunneld
After=network.target

[Service]
ExecStart=/path/to/pymobiledevice3 remote start-tunnel
Restart=on-failure

[Install]
WantedBy=default.target
```

**Recommendation:** Skip tunneld on Linux. It's iOS-only.

**Effort:** None (skip).

### 6. iOS Preview (`tools/ios-preview.swift`, `server/device/preview.py`)

**Current:** Compiles Swift tool using CoreMediaIO/AVFoundation frameworks.

**Linux:** Not applicable. CoreMediaIO is Apple-only.

**Android preview already works:** scrcpy preview (`server/device/scrcpy_preview.py`) is cross-platform and handles Android devices.

**Recommendation:** Gate `PreviewManager` (iOS preview) behind `MACOS`. Scrcpy preview works on Linux as-is.

**Effort:** Minimal (add platform guard).

### 7. Certificate Management (`server/proxy/cert_manager.py`)

**Current:** Installs mitmproxy CA cert into iOS simulator TrustStore.sqlite3.

**Linux:** Simulator cert install is iOS-only (skip). For Android devices, cert installation via adb push is already cross-platform.

**Android cert flow** (already implemented): Push cert to device → install via Settings or `adb shell am start` intent.

**Effort:** None — Android cert path already works.

### 8. MCP Tool Registration

**Current:** All 76 tools are registered regardless of platform.

**Change:** Tools should still all be registered (MCP tools are lazy and don't consume context). But iOS-only tools should return a clear error on Linux: "This tool requires macOS with Xcode installed."

**Alternative:** Only register platform-appropriate tools. This is cleaner but means the tool count varies by platform, which could confuse users reading docs.

**Recommendation:** Register all tools, return helpful errors for iOS-only tools on Linux. This is simpler and self-documenting.

**Effort:** Small. Add a platform check decorator or early return to iOS-only tool handlers.

### 9. MCP Server (`mcp/src/`)

**Current:** TypeScript, Node.js — already cross-platform.

**Status:** No changes needed. `npm install` and `npm run build` work on Linux.

### 10. API Route Registration

**Current:** All routers mounted unconditionally.

**Recommendation:** Keep all routes mounted. iOS-only endpoints return 400 with "iOS features require macOS" on Linux. This keeps the API surface consistent and discoverable (OpenAPI docs show all endpoints with platform notes).

**Effort:** Small.

## Install Script Changes (Detailed)

```bash
# Current
if [ "$(uname -s)" != "Darwin" ]; then
    die "Quern requires macOS."
fi

# New
OS="$(uname -s)"
case "$OS" in
    Darwin) ok "macOS $(sw_vers -productVersion)" ;;
    Linux)  ok "Linux $(uname -r)" ;;
    *)      die "Quern requires macOS or Linux. Detected: $OS" ;;
esac

# Homebrew check — macOS only
if [ "$OS" = "Darwin" ]; then
    # existing brew check
fi

# Linux package manager detection
if [ "$OS" = "Linux" ]; then
    if command -v apt &>/dev/null; then
        ok "apt (Debian/Ubuntu)"
    elif command -v dnf &>/dev/null; then
        ok "dnf (Fedora/RHEL)"
    elif command -v pacman &>/dev/null; then
        ok "pacman (Arch)"
    else
        warn "No supported package manager found"
    fi
fi
```

## Setup Changes (Detailed)

The `run_setup()` function in `setup.py` needs platform-conditional sections:

```python
system = platform.system()

# Always check
report.add(check_platform())        # Accept Darwin and Linux
report.add(check_python_version())
report.add(check_node())

# macOS-only
if system == "Darwin":
    report.add(check_xcode_cli_tools())
    if has_ios:
        # iOS deps: idb, libimobiledevice, etc.
        ...
    report.add(check_homebrew())

# Android (both platforms)
if has_adb:
    report.add(check_adb())
    report.add(check_scrcpy())

# Proxy (platform-specific checks)
report.add(check_vpn())             # Use ip route on Linux
report.add(check_mitmproxy_cert())   # Cross-platform
```

## Rollout Plan

### Phase 1: Platform abstraction (no user-facing changes)
1. Create `server/platform.py` with capability flags
2. Add platform guards to iOS-specific code paths
3. Make setup.py accept Linux (but keep macOS as primary)
4. Verify all Android code paths work without macOS assumptions
5. Run test suite on Linux (GitHub Actions)

### Phase 2: Linux install support
1. Update install script to accept Linux
2. Add Linux package manager detection to setup
3. Implement Linux system proxy (minimal: env vars + gsettings)
4. Update README with Linux instructions
5. Test on Ubuntu 22.04+ and Fedora 38+

### Phase 3: Polish
1. Linux CI testing (GitHub Actions matrix)
2. Linux-specific documentation on quern.dev
3. Address any edge cases found during testing

## Testing Strategy

- Add a `platform` dimension to the test matrix (GitHub Actions: `runs-on: [macos-latest, ubuntu-latest]`)
- iOS-specific tests should be skipped on Linux (`@pytest.mark.skipif(not MACOS)`)
- Android tests should run on both platforms
- Core server tests (API, storage, processing) should run on both platforms

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Android tools behave differently on Linux | Low | Medium | adb/u2 are well-tested cross-platform |
| System proxy fragmentation across Linux DEs | High | Low | Document manual setup, minimal auto-config |
| mitmproxy version differences on Linux | Low | Low | Pin version in requirements |
| Users expect iOS support on Linux | Medium | Low | Clear docs, helpful error messages |
| Maintenance burden of two platforms | Medium | Medium | Platform abstraction layer, CI matrix |

## Estimated Effort

| Phase | Effort |
|-------|--------|
| Phase 1: Platform abstraction | 1-2 days |
| Phase 2: Linux install support | 1-2 days |
| Phase 3: Polish | 1 day |
| **Total** | **3-5 days** |

Most of the work is conditional logic in setup.py and the install script. The server itself is already ~90% cross-platform for Android workflows.
