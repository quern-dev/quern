"""Quern — main entry point.

Usage:
    python3 -m server                  Start in foreground (backward compat)
    python3 -m server start            Start as daemon
    python3 -m server start -f         Start in foreground
    python3 -m server stop             Stop a running daemon
    python3 -m server restart          Restart the daemon
    python3 -m server status           Show server status
    python3 -m server setup            Check environment and install dependencies
    python3 -m server regenerate-key   Generate a new API key
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from server import get_version
from server.api.app_state import router as app_state_router
from server.api.build_app import router as build_app_router
from server.api.builds import router as builds_router
from server.api.crashes import router as crashes_router
from server.api.device import router as device_router
from server.api.device_pool import router as device_pool_router
from server.api.device_ui import router as device_ui_router
from server.api.landmarks import router as landmarks_router
from server.api.logs import router as logs_router
from server.api.proxy import router as proxy_router
from server.api.proxy_certs import router as proxy_certs_router
from server.api.proxy_intercept import router as proxy_intercept_router
from server.api.system import router as system_router
from server.api.wda import router as wda_router
from server.auth import APIKeyMiddleware
from server.config import ServerConfig, get_local_capture_processes, set_local_capture_processes
from server.device.controller import DeviceController
from server.lifecycle.daemon import _print_status, daemonize
from server.lifecycle.ports import (
    DEFAULT_PROXY_PORT,
    DEFAULT_SERVER_PORT,
    find_available_port,
    reclaim_port,
)
from server.lifecycle.state import (
    detect_local_ip,
    fetch_tools,
    is_server_healthy,
    read_state,
    remove_state,
    write_state,
)
from server.lifecycle.watchdog import proxy_watchdog
from server.models import LogEntry
from server.processing.deduplicator import Deduplicator
from server.processing.ingestion_filter import IngestionFilter
from server.proxy.capture_session import CaptureSessionManager
from server.proxy.flow_store import FlowStore
from server.sources import BaseSourceAdapter
from server.sources.build import BuildAdapter
from server.sources.crash import DIAGNOSTIC_REPORTS_DIR, CrashAdapter
from server.sources.oslog import OslogAdapter
from server.sources.proxy import ProxyAdapter
from server.sources.server_log import ServerLogAdapter
from server.sources.syslog import SyslogAdapter
from server.storage.ring_buffer import RingBuffer

logger = logging.getLogger(__name__)


def _fix_developer_dir() -> str | None:
    """Auto-fix DEVELOPER_DIR if xcode-select doesn't provide simctl.

    Common scenarios this handles:
    1. User renamed Xcode.app (e.g. to "Xcode 26.3.app") — xcode-select -p
       returns a stale path that no longer exists.
    2. xcode-select points to CommandLineTools, which has xcrun but not simctl.

    Setting DEVELOPER_DIR env var overrides xcode-select for this process
    and all child processes (xcrun, xcodebuild, swiftc, etc.).

    Returns a message describing the fix applied, or None if no fix was needed.
    """
    if os.environ.get("DEVELOPER_DIR"):
        return None  # Already explicitly set, don't override

    import subprocess

    def _simctl_works() -> bool:
        try:
            r = subprocess.run(
                ["xcrun", "simctl", "help"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    if _simctl_works():
        return None  # Everything is fine

    # simctl doesn't work — try to find an Xcode installation and set DEVELOPER_DIR
    try:
        result = subprocess.run(
            ["xcode-select", "-p"], capture_output=True, text=True, timeout=5,
        )
        current_dir = result.stdout.strip() if result.returncode == 0 else "(unknown)"
    except Exception:
        current_dir = "(unknown)"

    try:
        for xcode_app in sorted(Path("/Applications").glob("Xcode*.app")):
            candidate = xcode_app / "Contents" / "Developer"
            if candidate.exists():
                os.environ["DEVELOPER_DIR"] = str(candidate)
                # Verify this actually fixes simctl
                if _simctl_works():
                    msg = (
                        f"Xcode developer tools not found at default location ({current_dir}).\n"
                        f"Using {xcode_app} instead.\n"
                        f"To make this permanent: sudo xcode-select -s '{candidate}'"
                    )
                    logger.info(msg)
                    return msg
                # Didn't help — undo and try next
                del os.environ["DEVELOPER_DIR"]

        logger.warning(
            "simctl not available (xcode-select points to '%s'). "
            "No working Xcode found in /Applications. "
            "Simulator features will be disabled.",
            current_dir,
        )
    except Exception:
        pass  # Best-effort — don't block startup
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage server startup and shutdown."""
    config: ServerConfig = app.state.config
    buffer: RingBuffer = app.state.ring_buffer

    # Processing pipeline: adapter → deduplicator → filter → ring buffer
    ingestion_filter = IngestionFilter()
    app.state.ingestion_filter = ingestion_filter

    async def filtered_append(entry: LogEntry) -> None:
        if ingestion_filter.should_admit(entry):
            await buffer.append(entry)

    dedup = Deduplicator(on_entry=filtered_append)
    dedup.start()
    app.state.deduplicator = dedup

    # Server log adapter — dedicated buffer so device syslog can't evict server logs
    server_buffer: RingBuffer = app.state.server_buffer
    server_log = ServerLogAdapter(on_entry=server_buffer.append)
    adapters: dict[str, BaseSourceAdapter] = {"server": server_log}
    await server_log.start()

    # Start source adapters (all feed into the deduplicator)

    if app.state.enable_syslog:
        syslog = SyslogAdapter(
            device_id=config.default_device_id,
            on_entry=dedup.process,
            process_filter=app.state.process_filter,
        )
        adapters["syslog"] = syslog
        await syslog.start()

    # OSLog adapter (macOS only)
    if app.state.enable_oslog:
        oslog = OslogAdapter(
            device_id=config.default_device_id,
            on_entry=dedup.process,
            subsystem_filter=app.state.subsystem_filter,
            process_filter=app.state.process_filter,
        )
        adapters["oslog"] = oslog
        await oslog.start()

    # Crash report watcher
    if app.state.enable_crash:
        crash = CrashAdapter(
            device_id=config.default_device_id,
            on_entry=dedup.process,
            watch_dir=app.state.crash_dir,
            extra_watch_dirs=app.state.crash_extra_watch_dirs,
            process_filter=app.state.crash_process_filter,
            on_crash_hook=app.state.on_crash_hook,
        )
        adapters["crash"] = crash
        app.state.crash_adapter = crash
        await crash.start()

    # Build adapter (on-demand, no background loop)
    build = BuildAdapter(
        device_id=config.default_device_id,
        on_entry=buffer.append,
    )
    adapters["build"] = build
    app.state.build_adapter = build
    await build.start()

    # Proxy adapter — always create so status/start/stop endpoints work at runtime.
    # Only auto-start when enabled via --proxy / enable_proxy.
    flow_store = FlowStore()
    app.state.flow_store = flow_store
    app.state.capture_sessions = CaptureSessionManager()
    proxy = ProxyAdapter(
        device_id=config.default_device_id,
        on_entry=dedup.process,
        flow_store=flow_store,
        listen_port=app.state.proxy_port,
        local_capture_processes=app.state.local_capture_processes,
    )
    adapters["proxy"] = proxy
    app.state.proxy_adapter = proxy
    if app.state.enable_proxy:
        await proxy.start()

        # Check if system proxy is already configured (from previous run or manual setup)
        try:
            from server.lifecycle.state import read_state, update_state
            from server.proxy.system_proxy import (
                detect_active_interface,
                snapshot_system_proxy,
            )

            state = read_state()
            already_tracked = state and state.get("system_proxy_configured")

            # Detect if proxy is already pointing to our port
            iface = detect_active_interface()
            if iface:
                current_snap = await asyncio.to_thread(snapshot_system_proxy, iface)
                is_pointing_to_us = (
                    current_snap.http_proxy_enabled
                    and current_snap.http_proxy_server in ("127.0.0.1", "localhost")
                    and current_snap.http_proxy_port == app.state.proxy_port
                )

                if is_pointing_to_us and not already_tracked:
                    # System proxy is pointing to us but we don't have it tracked
                    # (probably from a previous crash or manual configuration)
                    logger.warning(
                        "Detected system proxy already pointing to port %d on %s — "
                        "saving snapshot for cleanup on shutdown",
                        app.state.proxy_port,
                        iface,
                    )
                    try:
                        update_state(
                            system_proxy_configured=True,
                            system_proxy_interface=iface,
                            system_proxy_snapshot=current_snap.to_dict(),
                        )
                    except Exception:
                        logger.debug("Could not update state file", exc_info=True)
                # else: system proxy not pointing to us — leave it alone.
                # Agents opt in via configure_system_proxy when ready to capture.
        except Exception:
            logger.warning("Failed to auto-configure system proxy", exc_info=True)

    app.state.source_adapters = adapters

    # Simulator log adapters — managed on-demand via API
    app.state.sim_log_adapters = {}

    # Physical device log adapters — managed on-demand via API
    app.state.device_log_adapters = {}

    # Plist watcher adapters — managed on-demand via API
    app.state.plist_watchers = {}

    # Device controller (Phase 3)
    device_controller = DeviceController()
    app.state.device_controller = device_controller
    tools = await device_controller.check_tools(adopt=True)
    logger.info("Device tools: %s", tools)
    if tools.get("sim_bridge"):
        logger.info("sim-bridge available — using native simulator UI backend")
    else:
        logger.info("sim-bridge not available — using idb for simulator UI")

    # Warn about missing tools
    if not tools.get("simctl"):
        logger.warning(
            "simctl not available — device management and screenshots disabled. "
            "If Xcode is installed, check 'xcode-select -p' points to a valid path. "
            "Fix with: sudo xcode-select -s /path/to/Xcode.app/Contents/Developer  "
            "Otherwise install Xcode Command Line Tools: xcode-select --install"
        )
    if not tools.get("idb") and not tools.get("sim_bridge"):
        logger.warning(
            "Neither sim-bridge nor idb available — simulator UI automation "
            "(tap, swipe, accessibility tree) disabled. "
            "sim-bridge requires Xcode 26+ with Apple Silicon. "
            "idb fallback: pip install fb-idb && brew install idb-companion"
        )
    if not tools.get("adb"):
        logger.info(
            "adb not available — Android device management disabled. "
            "Install Android Studio or the Android SDK platform-tools."
        )

    # Preview manager (live device screen preview)
    from server.device.preview import PreviewManager
    preview_manager = PreviewManager()
    app.state.preview_manager = preview_manager

    # Scrcpy preview (Android live device screen preview)
    from server.device.scrcpy_preview import ScrcpyPreview
    scrcpy_preview = ScrcpyPreview()
    app.state.scrcpy_preview = scrcpy_preview
    if scrcpy_preview.is_available():
        logger.info("scrcpy available — Android live preview enabled")
    else:
        logger.info(
            "scrcpy not available — Android live preview disabled "
            "(install with: brew install scrcpy)"
        )

    # Device pool (Phase 4b-alpha)
    from server.device.pool import DevicePool
    device_pool = DevicePool(device_controller)
    device_controller._pool = device_pool  # Enable pool-aware resolution
    app.state.device_pool = device_pool

    # Refresh pool state on startup
    await device_pool.refresh()

    # Warm device caches in the background (device type dispatch, WDA os_versions)
    async def _warmup_devices():
        try:
            devices = await device_controller.list_devices()
            logger.info("Device warmup: discovered %d device(s)", len(devices))
            # The caches are warm now, so the restored active device can be
            # written back with its name and type. Doing it here rather than
            # leaving it to a later resolve: the pool's sticky-active path
            # returns the UDID without running the setter, so a session that
            # never resolves by name or UDID would never refresh the sidecar.
        except Exception:
            logger.debug("Device warmup failed (non-fatal)", exc_info=True)
            return
        # Local capture routes traffic through the proxy with no gate on this
        # path -- the process list comes from config.json, written by a CLI
        # command in an earlier process. Warn rather than refuse; see the
        # function's docstring for why booting cannot be the thing that fails.
        try:
            from server.proxy.cert_preflight import warn_if_capture_lacks_trust

            await warn_if_capture_lacks_trust(
                device_controller, app.state.local_capture_processes,
            )
        except Exception:
            logger.debug("Could not check CA trust at startup", exc_info=True)

        try:
            device_controller.refresh_active_device()
        except OSError:
            # Its own handler, at its own level. Folded into the warmup catch
            # this was logged as "Device warmup failed" at debug -- invisible
            # by default and pointing at device discovery rather than at an
            # unwritable ~/.quern.
            logger.warning(
                "Could not refresh the active-device sidecar; the menu bar may "
                "show a UDID instead of a name", exc_info=True,
            )

    app.state._warmup_task = asyncio.create_task(_warmup_devices())

    # Re-probe the simulator UI backend periodically. Selection used to be
    # latched at startup and never revisited, so an Xcode upgrade under a
    # running server left every tap routed to a backend that could no longer
    # work -- indefinitely, and while /tools reported the correct answer
    # (#179). A /tools call re-syncs it too; this covers nobody asking.
    async def _resync_sim_bridge() -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await device_controller.refresh_sim_bridge_availability(max_age=0)
            except Exception:
                logger.debug("sim-bridge re-probe failed", exc_info=True)

    sim_bridge_resync_task = asyncio.create_task(_resync_sim_bridge())

    # Launch proxy watchdog if proxy is enabled
    watchdog_task = None
    if app.state.enable_proxy:
        watchdog_task = asyncio.create_task(
            proxy_watchdog(lambda: app.state.proxy_adapter)
        )

    # Periodic update check (repeats every 24h for long-lived servers)
    update_check_task = None
    try:
        from server.lifecycle.update_check import periodic_update_check
        update_check_task = asyncio.create_task(periodic_update_check())
    except Exception:
        pass

    # Network-change monitor — polls every ~15s so the server notices
    # SSID/IP changes proactively. Lets proxy_status surface "the network
    # just changed" without anyone having to ask.
    from server.lifecycle.network_monitor import (
        NetworkState,
        network_monitor_loop,
        update_network_state,
    )
    app.state.network_state = NetworkState()
    update_network_state(app.state.network_state)  # establish baseline immediately
    network_monitor_task = asyncio.create_task(
        network_monitor_loop(app.state.network_state),
    )
    logger.info(
        "Network monitor started: ssid=%r local_ip=%r",
        app.state.network_state.ssid,
        app.state.network_state.local_ip,
    )
    # Subsequent SSID/IP shifts get logged at INFO by network_monitor_loop.

    logger.info(
        "Server started on http://%s:%d — API key: %s...%s",
        config.host,
        config.port,
        config.api_key[:8],
        config.api_key[-4:],
    )

    yield

    # Shutdown: cancel background tasks, stop adapters, flush deduplicator
    for task in (
        watchdog_task, update_check_task, network_monitor_task,
        sim_bridge_resync_task,
    ):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Stop live previews if running
    if preview_manager:
        await preview_manager.stop()
    if scrcpy_preview:
        await scrcpy_preview.stop()

    # Shutdown WDA client (cancels idle task, deletes sessions, kills port-forwards)
    # Note: does NOT kill xcodebuild processes — they persist across restarts
    warmup = getattr(app.state, "_warmup_task", None)
    if warmup and not warmup.done():
        # It used to only warm caches, where an abrupt teardown cost nothing.
        # It now installs a CA and then records that it did, and losing the
        # loop between those two steps leaves the TrustStore holding a
        # certificate that cert-state.json says is absent.
        warmup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warmup

    if device_controller:
        await device_controller.wda_client.close()

    for adapter in adapters.values():
        await adapter.stop()
    for sim_adapter in app.state.sim_log_adapters.values():
        await sim_adapter.stop()
    for dev_adapter in app.state.device_log_adapters.values():
        await dev_adapter.stop()
    for plist_watcher in app.state.plist_watchers.values():
        await plist_watcher.stop()
    await dedup.stop()

    # Restore system proxy if we configured it
    from server.proxy.system_proxy import restore_from_state
    restore_from_state()

    # Clean up state file (if daemon mode wrote one)
    remove_state()
    logger.info("Server stopped")


def create_app(
    config: ServerConfig | None = None,
    process_filter: str | None = None,
    enable_syslog: bool = False,
    enable_oslog: bool = False,
    subsystem_filter: str | None = None,
    enable_crash: bool = True,
    crash_dir: Path | None = None,
    crash_extra_watch_dirs: list[Path] | None = None,
    crash_process_filter: str | None = None,
    enable_proxy: bool = True,
    proxy_port: int = 9101,
    on_crash_hook: str | None = None,
    local_capture_processes: list[str] | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = ServerConfig()

    app = FastAPI(
        title="Quern",
        version=get_version(),
        description="Debug log capture and AI context server",
        lifespan=lifespan,
    )

    # Store shared state
    app.state.config = config
    app.state.ring_buffer = RingBuffer(max_size=config.ring_buffer_size)
    app.state.server_buffer = RingBuffer(max_size=1_000)
    app.state.process_filter = process_filter
    app.state.enable_syslog = enable_syslog
    app.state.enable_oslog = enable_oslog
    app.state.subsystem_filter = subsystem_filter
    app.state.enable_crash = enable_crash
    app.state.crash_dir = crash_dir
    app.state.crash_extra_watch_dirs = crash_extra_watch_dirs or []
    app.state.crash_process_filter = crash_process_filter
    app.state.enable_proxy = enable_proxy
    app.state.proxy_port = proxy_port
    app.state.on_crash_hook = on_crash_hook
    app.state.local_capture_processes = local_capture_processes or []
    app.state.ingestion_filter = None
    app.state.source_adapters = {}
    app.state.crash_adapter = None
    app.state.build_adapter = None
    app.state.proxy_adapter = None
    app.state.flow_store = None
    app.state.device_controller = None
    app.state.device_pool = None
    app.state.preview_manager = None
    app.state.scrcpy_preview = None
    app.state.sim_log_adapters = {}
    app.state.device_log_adapters = {}
    app.state.network_state = None  # populated when the lifespan starts the monitor

    # Screen landmarks
    from server.device.landmarks import LandmarkRegistry
    app.state.landmark_registry = LandmarkRegistry()

    # Screenshot timeline state
    app.state.active_timeline = None

    # Timeline middleware — must be added before auth so it wraps the action endpoints
    from server.screenshot_timeline import TimelineMiddleware
    app.add_middleware(TimelineMiddleware)

    # Auth middleware
    app.add_middleware(APIKeyMiddleware, api_key=config.api_key)

    # CORS — allow local origins (Quern Helm, browser dev tools, etc.)
    # Must be added after auth so it wraps auth (handles OPTIONS preflight first)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routes
    app.include_router(logs_router)
    app.include_router(crashes_router)
    app.include_router(builds_router)
    app.include_router(proxy_router)
    app.include_router(proxy_intercept_router)
    app.include_router(proxy_certs_router)
    app.include_router(device_router)
    app.include_router(device_ui_router)
    app.include_router(device_pool_router)
    app.include_router(wda_router)
    app.include_router(build_app_router)
    app.include_router(app_state_router)
    app.include_router(landmarks_router)
    app.include_router(system_router)

    @app.get("/")
    async def root() -> RedirectResponse:
        """Redirect root to API docs."""
        return RedirectResponse(url="/docs")

    @app.get("/video-test")
    async def video_test() -> HTMLResponse:
        """Simple test page for MJPEG video streaming."""
        html = """<!DOCTYPE html>
<html><head><title>Quern Video Test</title></head>
<body style="background:#111;color:#fff;font-family:sans-serif;padding:20px">
<h2>Quern MJPEG Stream Test</h2>
<div id="streams"></div>
<script>
async function startStream(udid, label) {
  const container = document.getElementById('streams');
  const div = document.createElement('div');
  div.style.cssText = 'display:inline-block;margin:10px;text-align:center';
  div.innerHTML = '<p>' + label + '</p>';
  const img = document.createElement('img');
  img.style.cssText = 'border:1px solid #333;max-height:500px';
  div.appendChild(img);
  container.appendChild(div);

  const res = await fetch(
    '/api/v1/device/video?udid=' + encodeURIComponent(udid) + '&fps=5&scale=0.5&quality=75',
    { headers: { 'Authorization': 'Bearer """ + config.api_key + """' } }
  );
  const reader = res.body.getReader();
  let buffer = new Uint8Array();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const tmp = new Uint8Array(buffer.length + value.length);
    tmp.set(buffer); tmp.set(value, buffer.length); buffer = tmp;
    let start = -1, end = -1;
    for (let i = 0; i < buffer.length - 1; i++) {
      if (buffer[i] === 0xFF && buffer[i+1] === 0xD8) start = i;
      if (buffer[i] === 0xFF && buffer[i+1] === 0xD9 && start >= 0) { end = i + 2; break; }
    }
    if (start >= 0 && end > start) {
      const blob = new Blob([buffer.slice(start, end)], { type: 'image/jpeg' });
      const url = URL.createObjectURL(blob);
      const prev = img.src;
      img.src = url;
      if (prev.startsWith('blob:')) URL.revokeObjectURL(prev);
      buffer = buffer.slice(end);
    }
  }
}
// Auto-detect booted devices
fetch('/api/v1/device/list', {
  headers: { 'Authorization': 'Bearer """ + config.api_key + """' }
}).then(r => r.json()).then(data => {
  const eligible = data.devices.filter(
    d => d.state === 'booted' && d.connection_type !== 'localNetwork');
  if (eligible.length === 0) {
    document.getElementById('streams').innerHTML = '<p>No booted USB/wired devices found</p>';
    return;
  }
  eligible.forEach(d => startStream(d.udid, d.name));
});
</script></body></html>"""
        return HTMLResponse(html)

    @app.get("/health")
    async def health() -> dict:
        """Fast liveness ping.

        Intentionally does NO device-tool probing (that lives on /tools and the
        `doctor` command). check_tools() shells out to adb/simctl/idb/etc., and a
        slow probe like `idb list-targets` (~5s) previously pushed /health past
        the CLI's 2s health-check timeout — making `quern status`/`start` wrongly
        report the server as down/stale. Keep this endpoint sub-millisecond.
        """
        return {"status": "ok", "version": get_version()}

    @app.get("/api/v1/health")
    async def api_health() -> dict:
        """Fast liveness ping — see health()."""
        return {"status": "ok", "version": get_version()}

    @app.get("/tools")
    async def tools() -> dict:
        """Device-tool availability + UI cache stats.

        Split out of /health so the health ping stays fast (tool probes can take
        several seconds). Consumed by the `doctor` and `status` CLI commands.
        Public, like /health — it returns only non-sensitive availability flags.
        """
        tools_status: dict = {}
        cache_stats: dict = {}
        if hasattr(app.state, "device_controller") and app.state.device_controller:
            tools_status = await app.state.device_controller.check_tools(adopt=True)
            cache_stats = app.state.device_controller.get_cache_stats()
        # Availability flags only. The per-site inventory carries absolute
        # paths -- account names and filesystem layout -- and lives behind auth
        # at /api/v1/device/tools/sites instead.
        return {"tools": tools_status, "ui_cache": cache_stats}

    return app


def _add_server_flags(parser: argparse.ArgumentParser) -> None:
    """Add shared server flags to a subcommand parser."""
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 9100)")
    parser.add_argument("--process", "-p", default=None, help="Filter logs to this process name")
    parser.add_argument(
        "--buffer-size", type=int, default=10_000, help="Ring buffer size (default: 10000)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--oslog", action="store_true", default=False,
        help="Enable OSLog adapter (default: off)",
    )
    parser.add_argument(
        "--no-oslog", action="store_true", default=False,
        help="Disable OSLog adapter",
    )
    parser.add_argument("--subsystem", default=None, help="OSLog subsystem filter")
    parser.add_argument(
        "--no-crash", action="store_true", default=False,
        help="Disable crash report watcher",
    )
    parser.add_argument(
        "--crash-dir", default=None, type=Path,
        help="Directory to watch for crash reports (default: ~/.quern/crashes)",
    )
    parser.add_argument(
        "--simulator-crashes",
        action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Watch ~/Library/Logs/DiagnosticReports/ for simulator "
            "crash reports (default: enabled)"
        ),
    )
    parser.add_argument(
        "--crash-process-filter", default=None, type=str,
        help="Only capture crashes whose process name contains this string",
    )
    parser.add_argument(
        "--syslog", action="store_true", default=False,
        help="Enable idevicesyslog capture from USB-connected devices (default: off)",
    )
    parser.add_argument(
        "--no-syslog", action="store_true", default=False,
        help="Disable idevicesyslog capture (default: already off)",
    )
    parser.add_argument(
        "--on-crash", default=None, type=str,
        help="Shell command to run on each crash (CrashReport JSON piped to stdin)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=False,
        help="Disable the mitmproxy network capture adapter",
    )
    parser.add_argument(
        "--proxy-port", type=int, default=None,
        help="Port for the mitmproxy listener (default: 9101)",
    )


def _resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in defaults for None-valued port args."""
    if args.port is None:
        args.port = DEFAULT_SERVER_PORT
    if args.proxy_port is None:
        args.proxy_port = DEFAULT_PROXY_PORT
    return args


def _is_our_process(pid: int) -> bool:
    """Check if a PID belongs to a quern-debug-server process (PID reuse guard)."""
    from server.lifecycle.ports import _is_quern_process
    return _is_quern_process(pid)


def _config_or_exit(**kwargs: str | int) -> ServerConfig:
    """Build the config, or stop with the reason on one line.

    An api-key file quern cannot use is a setup problem with a one-line fix,
    and the sentence saying what to do is the whole value of the check. A
    traceback buries it under frames from inside a dataclass.
    """
    try:
        return ServerConfig(**kwargs)
    except ValueError as exc:
        print(f"\n  {exc}\n")
        sys.exit(1)


def _cmd_start(args: argparse.Namespace) -> None:
    """Start the server (daemon or foreground)."""
    # Reconcile Python deps before anything imports them. Cheap when in sync
    # (two stat calls); self-healing when not, including after a manual git
    # pull or a branch switch, which `quern update` never sees.
    from server.__main__ import _ensure_mcp_built, _ensure_python_deps
    _ensure_python_deps(quiet=True)

    # Rebuild the MCP server only when dist/ is actually stale. A release
    # tarball ships it built, and reaching for npm to confirm that is what made
    # a GUI-launched start fail on a machine with an fnm node (#193).
    if not _ensure_mcp_built(quiet=True):
        print("Warning: MCP server build failed — MCP tools may be stale")

    # Non-blocking update check (once per 24h)
    # In foreground mode, run synchronously so the user sees the message.
    # In daemon mode, defer to after fork — threads before fork are not
    # safe on macOS (ObjC runtime detects partially-initialized state from
    # dead threads in the child and aborts on the next fork/subprocess call).
    if args.foreground:
        try:
            from server.lifecycle.update_check import check_for_updates
            update_msg = check_for_updates()
            if update_msg:
                print(update_msg)
        except Exception:
            pass  # Never block startup

    # Check for existing instance
    existing = read_state()
    if existing and is_server_healthy(existing["server_port"]):
        print("Server already running")
        _print_status(existing)
        sys.exit(0)

    # Validated here, before anything is torn down. Everything below this
    # point has side effects: stale state is removed, ports are reclaimed from
    # stale quern processes, and the process daemonizes. An api-key file quern
    # cannot use would otherwise stop the start *after* all of that -- having
    # killed the previous instance's leftovers -- and, past daemonize(), in a
    # child whose output goes to the log, so the shell sees `quern start`
    # succeed and there is no server. The port is not known yet, and does not
    # need to be: nothing being checked here depends on it.
    _config_or_exit(host=args.host, ring_buffer_size=args.buffer_size)

    if existing:
        # Restore system proxy if stale state has it configured
        if existing.get("system_proxy_configured"):
            from server.proxy.system_proxy import restore_from_state_dict
            restore_from_state_dict(existing)
        # Stale state — clean up
        remove_state()

    # Resolve ports — try to reclaim from stale quern processes first,
    # only scan upward if occupied by something else
    server_port = args.port
    if not reclaim_port(server_port, args.host):
        print(f"Port {server_port} is in use by another application")
        server_port = find_available_port(
            server_port + 1, host=args.host, exclude={args.proxy_port},
        )
        print(f"Using port {server_port} instead (override with --port)")

    proxy_port = args.proxy_port
    enable_proxy = not args.no_proxy
    if enable_proxy:
        if not reclaim_port(proxy_port, args.host):
            print(f"Proxy port {proxy_port} is in use by another application")
            proxy_port = find_available_port(
                proxy_port + 1,
                host=args.host,
                exclude={server_port},
            )
            print(f"Using proxy port {proxy_port} instead (override with --proxy-port)")

    # Daemonize if not foreground mode
    if not args.foreground:
        daemonize(server_port)
        # daemonize() never returns — it spawns a child process and exits.

    # Configure logging
    # QUERN_LOG_LEVEL turns debug logging on without restarting through a
    # different command line -- which matters because the thing you most want
    # it for (an action that hung) is not reproducible on demand. `--verbose`
    # still wins if it is passed.
    # A whitelist rather than getattr(logging, name): `logging` has plenty of
    # uppercase attributes that are not levels, and one of them resolving to a
    # truthy non-level would configure logging with nonsense.
    _LEVELS = {
        "DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
        "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
    }
    _env_level = os.environ.get("QUERN_LOG_LEVEL", "").strip().upper()
    _level = (
        logging.DEBUG if args.verbose
        else _LEVELS.get(_env_level, logging.INFO)
    )
    logging.basicConfig(
        level=_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Daemon-mode update check — runs in the child process after fork,
    # so threads and ObjC state are clean.
    if not args.foreground:
        import threading

        def _bg_update_check():
            try:
                from server.lifecycle.update_check import check_for_updates
                msg = check_for_updates()
                if msg:
                    logger.info(msg)
            except Exception:
                pass

        threading.Thread(target=_bg_update_check, daemon=True).start()

    # Built again, now that the port is resolved. The first call is the one
    # that refuses; this one cannot fail for a reason that one would not have
    # caught, and constructing it twice is cheaper than carrying a half-built
    # config through the port resolution above.
    config = _config_or_exit(
        host=args.host, port=server_port, ring_buffer_size=args.buffer_size,
    )

    enable_syslog = args.syslog is True and not args.no_syslog
    enable_oslog = args.oslog is True and not args.no_oslog
    enable_crash = not args.no_crash
    local_capture_processes = get_local_capture_processes() if enable_proxy else []

    # Auto-fix developer dir before any tool checks
    developer_dir_msg = _fix_developer_dir()

    # Write state file. The active device persists across restarts via its
    # own sidecar file (server/lifecycle/state.py:ACTIVE_DEVICE_FILE) — no
    # need to round-trip it through state.json, which gets deleted on stop.
    state_dict = {
        "pid": os.getpid(),
        "server_host": args.host,
        "local_ip": detect_local_ip(),
        "server_port": server_port,
        "proxy_port": proxy_port,
        "proxy_enabled": enable_proxy,
        "proxy_status": "starting" if enable_proxy else "disabled",
        "local_capture": local_capture_processes,
        "started_at": datetime.now(UTC).isoformat(),
        "api_key": config.api_key,
    }
    if developer_dir_msg:
        state_dict["developer_dir_note"] = developer_dir_msg
    write_state(state_dict)

    if args.foreground:
        print(f"Quern v{get_version()}")
        print(f"  http://{config.host}:{server_port}")
        print(f"  API key: {config.api_key[:8]}...{config.api_key[-4:]}")
        print("  API key file: ~/.quern/api-key")
        if args.process:
            print(f"  Process filter: {args.process}")
        if enable_oslog:
            sub = args.subsystem or "(all)"
            print(f"  OSLog: enabled (subsystem: {sub})")
        if enable_crash:
            crash_path = args.crash_dir or "~/.quern/crashes"
            print(f"  Crash watcher: enabled (dir: {crash_path})")
            if args.simulator_crashes:
                print(f"  Simulator crashes: enabled ({DIAGNOSTIC_REPORTS_DIR})")
            if args.crash_process_filter:
                print(f"  Crash process filter: {args.crash_process_filter}")
        if enable_proxy:
            print(f"  Proxy: enabled (port: {proxy_port})")
            if local_capture_processes:
                print(f"  Local capture: {', '.join(local_capture_processes)}")
            else:
                print("  Local capture: disabled")
                print("    Capture simulator traffic without a system proxy:")
                print("    Run: quern enable-local-capture [process ...]")
        if args.on_crash:
            print(f"  On-crash hook: {args.on_crash}")
        if developer_dir_msg:
            for i, line in enumerate(developer_dir_msg.splitlines()):
                prefix = "  Note: " if i == 0 else "        "
                print(f"{prefix}{line}")
        print()

    crash_extra_watch_dirs = []
    if args.simulator_crashes:
        crash_extra_watch_dirs.append(DIAGNOSTIC_REPORTS_DIR)

    app = create_app(
        config=config,
        process_filter=args.process,
        enable_syslog=enable_syslog,
        enable_oslog=enable_oslog,
        subsystem_filter=args.subsystem,
        enable_crash=enable_crash,
        crash_dir=args.crash_dir,
        crash_extra_watch_dirs=crash_extra_watch_dirs,
        crash_process_filter=args.crash_process_filter,
        enable_proxy=enable_proxy,
        proxy_port=proxy_port,
        on_crash_hook=args.on_crash,
        local_capture_processes=local_capture_processes,
    )

    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=server_port,
        log_level="debug" if args.verbose else "info",
        loop="asyncio",  # uvloop uses fork+exec for subprocesses, which crashes on macOS
    )
    server = uvicorn.Server(uv_config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # Clean shutdown already handled by lifespan


def _restore_system_proxy_if_needed(state: dict) -> None:
    """Restore system proxy from state dict if configured (for stale/crash recovery)."""
    if state.get("system_proxy_configured"):
        from server.proxy.system_proxy import restore_from_state_dict
        if restore_from_state_dict(state):
            print("Restored system proxy settings")


def _cmd_stop(args: argparse.Namespace) -> None:
    """Stop the running server daemon."""
    state = read_state()
    if not state:
        print("No server running")
        return

    pid = state.get("pid")
    if not pid:
        _restore_system_proxy_if_needed(state)
        remove_state()
        print("No server running (stale state cleaned up)")
        return

    # Check if process exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _restore_system_proxy_if_needed(state)
        remove_state()
        print("No server running (stale state cleaned up)")
        return

    # PID reuse guard
    if not _is_our_process(pid):
        _restore_system_proxy_if_needed(state)
        remove_state()
        print(f"Warning: PID {pid} is not a quern-debug-server process (stale state cleaned up)")
        return

    # Send SIGTERM — the server's lifespan handler will restore system proxy
    print(f"Stopping server (pid {pid})...")
    os.kill(pid, signal.SIGTERM)

    # Wait for exit
    for _ in range(50):  # 5 seconds at 100ms intervals
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            remove_state()
            print("Server stopped")
            return

    # Force kill — lifespan didn't run, so we must restore system proxy
    print("Server didn't stop gracefully, sending SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Re-read state since server may have partially updated it
    state = read_state() or state
    _restore_system_proxy_if_needed(state)

    # Clean up orphaned proxy subprocess (SIGKILL doesn't propagate
    # to children, so mitmdump may still be holding the proxy port)
    proxy_port = state.get("proxy_port")
    if proxy_port:
        reclaim_port(proxy_port)

    remove_state()
    print("Server killed")


def _cmd_restart(args: argparse.Namespace) -> None:
    """Restart the server daemon."""
    _cmd_stop(args)
    time.sleep(0.5)
    _cmd_start(args)


def _cmd_status(args: argparse.Namespace) -> None:
    """Show server status."""
    state = read_state()
    if not state:
        print("No server running")
        sys.exit(1)

    port = state.get("server_port", 9100)
    if not is_server_healthy(port):
        print("Server state file exists but server is not responding")
        print("  State file may be stale. Run 'quern-debug-server stop' to clean up.")
        sys.exit(1)

    # Calculate uptime
    started = state.get("started_at")
    if started:
        try:
            start_dt = datetime.fromisoformat(started)
            uptime = datetime.now(UTC) - start_dt
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            state["_uptime"] = f"{hours}h {minutes}m {seconds}s"
        except (ValueError, TypeError):
            pass

    _print_status(state)
    if "_uptime" in state:
        print(f"  Uptime:     {state['_uptime']}")

    # Device-tool availability. Fetched explicitly from /tools (NOT /health, which
    # is now a fast liveness ping). This adds a few seconds to `status` because it
    # probes adb/simctl/idb/etc. — acceptable for a manual command.
    tools_data = fetch_tools(port)
    if tools_data and tools_data.get("tools"):
        line = ", ".join(
            f"{name} {'✓' if ok else '✗'}"
            for name, ok in sorted(tools_data["tools"].items())
        )
        print(f"  Tools:      {line}")
    sys.exit(0)


def _fetch_device_tools() -> tuple[dict | None, str]:
    """Device-tool availability from the running server, or None and the reason.

    ``None`` means "could not ask" and is deliberately distinct from ``{}``,
    which means "asked, and the server has no device controller". Collapsing
    the two would report a check that never ran as one that came back empty.

    This is the only part of doctor that needs a server at all.
    """
    state = read_state()
    if not state:
        return None, "no server running (start it with `quern start`)"

    port = state.get("server_port", 9100)
    if not is_server_healthy(port):
        return None, f"server on port {port} is not responding on /health"

    data = fetch_tools(port)
    if data is None:
        return None, f"server on port {port} did not answer /tools"

    tools = data.get("tools")
    if not isinstance(tools, dict):
        # An explicit null is not the same as a missing key, and `.get(k, {})`
        # only defaults on the latter. Without this, a null answer produced a
        # dangling "not checked — " with no reason, blaming an unreachable
        # server for one that replied.
        return None, f"server on port {port} answered /tools without a tool list"
    return tools, ""


@contextlib.contextmanager
def _update_check_logged_to_file() -> Iterator[bool]:
    """Send the update check's own log lines to server.log, not the terminal.

    The daemon gets this for free: `daemonize()` redirects its stderr into
    server.log, so a `logger.warning` lands there. A CLI command has no such
    redirection and no handler, so logging falls back to stderr -- which would
    print the raw exception on screen directly underneath the one-line summary
    that exists to replace it, and would make "the full error is in
    server.log" a false promise in the same breath.
    """
    log = logging.getLogger(__name__)
    from server.lifecycle.daemon import LOG_FILE

    handler = None
    previous = log.propagate
    # Off first, and regardless of whether the handler opens. Leaving it on in
    # the failure case sends the record to the CLI's root handler, which prints
    # it to stderr -- the raw exception, directly underneath the one-line
    # summary that exists to replace it.
    log.propagate = False
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_FILE)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        log.addHandler(handler)
    except OSError:
        # An unwritable log directory must not stop the check. The summary and
        # the remedy still reach the reader; only the detail is lost -- and the
        # caller is told, so it does not point at a file nothing was written to.
        handler = None
    try:
        yield handler is not None
    finally:
        log.propagate = previous
        if handler is not None:
            log.removeHandler(handler)
            handler.close()


def _is_from_this_run(info: dict, started: datetime) -> bool:
    """Whether this update-info record was written by the check just made.

    Compared rather than assumed, because "the file exists" and "the file
    answers the question I just asked" are different claims and only the second
    one is safe to print. A record with no readable timestamp is not trusted:
    there is no way to tell which run it belongs to, and guessing in the
    optimistic direction is what reports a stale "up to date".
    """
    stamp = info.get("checked_at")
    if not isinstance(stamp, str):
        return False
    try:
        checked_at = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    # A second of slack: the record's timestamp is taken inside the check, a
    # moment after `started`, but a filesystem with coarse clock granularity --
    # or a record written by a process whose clock is a hair behind -- should
    # not read as stale.
    return checked_at >= started - timedelta(seconds=1)


def _cmd_check_updates() -> int:
    """Ask now, rather than waiting for the daily check.

    The cached answer in update-info.json is refreshed at most once a day, so
    a release landing this afternoon is not offered until tomorrow. The CLI has
    never had this problem -- `quern update` checks when you run it -- but
    anything reading the cache does, the menu bar included.
    """
    from server.lifecycle.update_check import (
        CheckFailure,
        check_for_updates,
        read_update_info,
    )

    failures: list[CheckFailure] = []
    # Before the check, so the cache it leaves can be told apart from the one
    # that was already there. See the freshness test below.
    started = datetime.now(UTC)
    with _update_check_logged_to_file() as detail_was_logged:
        # force=True: someone asked. That skips the once-a-day rate limit and
        # the "update_check": false opt-out alike, because the opt-out governs
        # the automatic check and this is not one.
        message = check_for_updates(force=True, on_error=failures.append)

    if failures:
        # Before the cache is consulted, deliberately. read_update_info()
        # returns whatever the last *successful* check left behind, so asking
        # it first lets a stale "update available" answer a question the
        # network never got to.
        for line in failures[0].lines():
            print(line, file=sys.stderr)
        # Where the raw error went. The screen gets a summary now, so this is
        # the pointer to the thing that actually happened.
        if detail_was_logged:
            from server.lifecycle.daemon import LOG_FILE
            print(f"The full error is in {LOG_FILE}", file=sys.stderr)

        return 1

    info = read_update_info() or {}

    # The cache only answers for this run if this run wrote it. A raised
    # exception is not the only way to learn nothing: check_for_updates returns
    # None silently when it cannot read a local version or a HEAD sha, and
    # _write_update_info swallows its own write errors -- so the check can come
    # back having produced no result at all while a record from days ago sits
    # on disk. Reporting that as today's answer is the same false all-clear the
    # failure ordering above exists to prevent, arrived at down a quieter path.
    if not _is_from_this_run(info, started):
        print("Could not check for updates: the check produced no result.",
              file=sys.stderr)
        print("Try again. If it keeps happening, run `quern capture-env` and "
              "open an issue.", file=sys.stderr)
        return 1

    if info.get("update_available"):
        print(message or f"Update available: v{info.get('latest_version')}")
        return 0

    current = info.get("current_version") or "unknown"
    print(f"Up to date (v{current}, channel '{info.get('channel', 'stable')}').")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> None:
    """Read-only diagnostics: device tools, venv, tool versions, service health.

    Only the device-tool section needs a running server. Everything else reads
    the filesystem or probes a separate daemon, so it used to be withheld for no
    reason: doctor exited at the first check it could not make and printed
    nothing else. That put its most useful output -- whether the venv matches
    pyproject.toml -- behind the very condition that most often sends someone
    looking for it, since a stale venv is a good way to stop the server coming
    up at all. A command named `doctor` should work when the patient is sick.

    Two different exit contracts, because `--fix` is an action and plain doctor
    is a report:

    * Without `--fix`, nonzero means a check could not be *made* -- an
      unreachable server, a probe that threw. A check that ran and found
      something wrong still exits 0: a stale venv is doctor working, not doctor
      failing, and the finding is in the output where it belongs.
    * With `--fix`, the status is the repair's. `quern doctor --fix && quern
      start` is the sequence this exists for, and it ran only when the server
      was already down -- so an exit code that folded in "device tools could
      not be checked" refused to continue after a repair that worked.
    """
    fix = getattr(args, "fix", False)

    tools, reason = _fetch_device_tools()
    print("Device tools:")
    if tools is None:
        print(f"  ? not checked — {reason}")
    elif not tools:
        print("  ? none reported — the server has no device controller")
    else:
        for name, ok in sorted(tools.items()):
            print(f"  {'✓' if ok else '✗'} {name}")

    repaired = _report_python_deps(fix)
    _report_external_tools(fix)
    node_complete = _report_node()
    menubar_checked, menubar_repaired = _report_menubar(fix)
    services_complete = _report_service_health(fix)

    if fix:
        # Only repairs. A *diagnostic* that could not be made must not veto a
        # repair that worked -- that is what this contract exists for, and
        # folding both halves of the menu-bar section into one bool broke it:
        # an unreadable Info.plist would have failed `doctor --fix && quern
        # start` after the venv repair succeeded.
        sys.exit(0 if repaired is not False and menubar_repaired is not False else 1)
    sys.exit(0 if tools is not None and services_complete and node_complete
             and menubar_checked else 1)


def _report_menubar(fix: bool = False) -> tuple[bool, bool | None]:
    """The Quern app's own version, next to the server's (#201).

    Returns `(checked, repaired)`: whether the section could look, and -- when
    `--fix` repaired something -- whether that worked. They answer different
    questions and go to different exit codes.

    `--fix` repairs an app that is *damaged or stale*; it does not install one
    that was never there. Fetching a release, writing an app into
    ~/Applications and launching it is a first install, and `doctor` is run
    non-interactively and unattended. `describe()` already prints the command
    for that case.
    """
    import platform

    if platform.system() != "Darwin":
        return True, None
    from server.lifecycle import menubar

    print()
    print("Quern app:")
    try:
        state = menubar.state()
    except Exception as exc:  # noqa: BLE001 -- doctor reports, it does not crash
        print(f"  ? could not be checked ({exc})")
        return False, None
    for line in menubar.describe(state):
        print(f"  {line}")
    if fix and state.installed and state.needs_install:
        print("  --fix: reinstalling the Quern app")
        # The result reaches the exit code: `doctor --fix` printing "failed
        # verification" and exiting 0 is the shape this whole file argues
        # against.
        return True, menubar.cmd_install() == 0
    return True, None


def _report_node() -> bool:
    """Which `node` each place will run, and what to do where it will fail.

    Returns whether the probe completed. One row per place because the fixes
    differ: fnm in `.zshenv` repairs the non-interactive row and does nothing
    for GUI apps (#214). Read-only, like the rest of doctor.
    """
    from server.lifecycle import node_env

    print()
    print(f"Node.js (the MCP wrapper needs {node_env.MIN_NODE_MAJOR}+):")
    try:
        sites = node_env.probe()
    except Exception as exc:  # noqa: BLE001 -- doctor reports, it does not crash
        print(f"  ? could not be checked ({exc})")
        return False
    marks = {node_env.OK: "\u2713", node_env.UNKNOWN: "?", node_env.SKIPPED: "\u2013"}
    for site in sites:
        found = f"{site.version or 'no version'}  {site.path}" if site.path else site.status
        mark = marks.get(site.status, "\u2717")
        print(f"  {mark} {site.place} — {found}")
        print(f"      used by: {site.used_by}")
        if not site.ok:
            label = "note" if site.status == node_env.SKIPPED else "fix"
            print(f"      {label}: {node_env.fix_for(site, sites)}")
    # A place whose probe failed was not checked, and doctor's exit code says
    # when a check could not be made. An unsupported shell is not that: it is
    # a permanent fact about the machine, and failing on it would fail every
    # doctor run there.
    return not any(site.status == node_env.UNKNOWN for site in sites)


_HEALTH_MARKERS = {"healthy": "\u2713", "unsupported": "\u2013"}


def _report_service_health(fix: bool = False) -> bool:
    """Whether tunneld and local capture are actually working, not just present.

    Returns whether both probes completed. A probe that threw is doctor failing
    to look, not a clean result, and it reaches the exit code for the same
    reason an unreachable device-tool section does.

    Both fail in the same shape: the thing quern checks stays green while the
    thing that matters stops working. `check_tools()` reports tunneld from an
    HTTP probe that cannot separate "wedged" from "never started", and nothing
    looked at the mitmproxy system extension at all -- so local capture reports
    itself enabled while macOS runs an extension the installed wheel replaced.

    tunneld is reported and never repaired here, deliberately. Recovery is
    `bootout` + `bootstrap` and belongs to #73, which has a live wedged instance
    being preserved for testing; a `--fix` that silently restarted it would
    destroy the only real reproduction anyone has.
    """
    import asyncio

    print()
    print("Service health:")
    complete = True

    try:
        from server.device.tunneld import tunneld_health

        health = asyncio.run(tunneld_health())
    except Exception as exc:
        print(f"  ? tunneld — could not be checked ({exc})")
        complete = False
    else:
        marker = _HEALTH_MARKERS.get(health.status, "\u2717")
        print(f"  {marker} tunneld — {health.detail}")
        if health.remedy:
            print(f"      {health.remedy}")
        if health.status == "wedged":
            print("      not repaired automatically: see issue #73")

    try:
        from server.proxy.extension import extension_health

        ext = extension_health()
    except Exception as exc:
        print(f"  ? local capture extension — could not be checked ({exc})")
        return False

    marker = _HEALTH_MARKERS.get(ext.status, "\u2717")
    print(f"  {marker} local capture extension — {ext.detail}")

    if not ext.fixable:
        if ext.remedy:
            print(f"      {ext.remedy}")
        return complete

    if not fix:
        print(f"      {ext.remedy}")
        return complete

    from server.proxy.extension import reinstall

    ok, message = reinstall()
    # "handed off to macOS", not "fixed": approval is a human step, so claiming
    # success here would be reporting a repair that has not happened yet.
    outcome = "\u2192" if ok else "\u2717"
    print(f"      {outcome} {message}")
    return complete


def _report_external_tools(fix: bool = False) -> None:
    """Every install site quern uses, what manages it, and what it is behind.

    Read-only, like the rest of doctor: this reports the upgrade commands and
    never runs them -- including under `--fix`.

    That is a deliberate boundary rather than an omission. `--fix` is scoped to
    "exactly what server startup runs" (see `_report_python_deps`), which is the
    venv and nothing else; a pipx or brew upgrade changes state for every other
    consumer on the machine, which is further out of scope than `quern setup` --
    already ruled out for `--fix` on the same grounds. Having two commands that
    both upgrade external tools would also be two things to keep in agreement.

    What `--fix` must not do is stay silent about it. Printing "--fix: nothing to
    do" for the venv directly above a tool marked as behind reads as "and nothing
    to do about that either", so when `--fix` is asked for and cannot help, it
    says so and names the command that can.

    The full list rather than only the stale ones, because the question doctor
    gets asked is "why does this machine behave differently from that one", and
    two machines comparing only their problems will agree they have none while
    running three-major-apart copies of the same tool.
    """
    import asyncio

    from server.device.tool_updates import format_report, plan_updates
    from server.device.tool_versions import collect_sites

    print()
    try:
        async def gather():
            return await plan_updates(await collect_sites())

        updates = asyncio.run(gather())
    except Exception as exc:
        # Diagnostics must not be the thing that breaks. A machine where this
        # raises is exactly one someone is running doctor on.
        print(f"External tools: could not be checked ({exc})")
        return

    print(format_report(updates))

    from server.device.tool_updates import actionable

    if fix and actionable(updates):
        print()
        print("  --fix does not upgrade external tools: these commands change")
        print("  state for every consumer on this machine, not just quern.")
        print("  Run them yourself, or: quern update --tools")


def _report_python_deps(fix: bool) -> bool | None:
    """Report whether the venv matches pyproject.toml, and optionally repair it.

    Returns the repair outcome: True repaired, False repair failed, None no
    repair attempted. `doctor --fix` reports that rather than the diagnostics,
    because it is an action and the caller wants to know whether it worked.

    Read-only by default — `doctor` is documented as read-only diagnostics, and
    a tool you reach for when things are broken should not change state while
    you are looking at it.

    `--fix` runs exactly what server startup runs, nothing more. It is
    deliberately not `quern setup`: setup prompts in twenty places, can use
    sudo, and can delete and recreate the venv. Repairing a stale dependency
    should not be able to end in any of those.
    """
    from server.__main__ import _ensure_python_deps, python_deps_state

    state = python_deps_state()
    print()
    print("Python dependencies:")

    if not state["applicable"]:
        print(f"  - not applicable ({state['reason']})")
        return None

    if state["in_sync"]:
        print("  ✓ in sync with pyproject.toml")
        if fix:
            print("    --fix: nothing to do")
        return None

    print(f"  ✗ out of sync — {state['reason']}")
    if not fix:
        print("    Repair with: quern doctor --fix")
        print("    (starting the server also reconciles this automatically)")
        return None

    if _ensure_python_deps(quiet=False, force=True):
        print("  ✓ repaired")
        return True

    print("  ✗ repair failed — see the error above")
    # Returned, not exited. Exiting here skipped the external-tool and
    # service-health sections, so the one run where the most has gone wrong
    # printed the least -- and a failed venv repair is a good reason to want
    # to know what else is stale.
    return False


def _local_capture_cert_gate(processes: list[str], skip_cert_check: bool) -> None:
    """The capture gate, for the one caller that reaches it without a request.

    `enable-local-capture` writes config.json and the lifespan starts routing
    from it, so this command is a routing boundary in the same sense the two
    HTTP endpoints are -- and until now the only one of the three with no gate.
    It is not a human-only path: an agent has a shell, and the agent guide names
    this command.

    Runs the preflight in-process rather than through the API, so the CLI does
    not need an authenticated HTTP client it has nowhere else. A
    `DeviceController` costs almost nothing to build -- it reads the
    active-device sidecar and nothing else -- and the check itself is about
    0.9s, near enough all of it `list_devices()` enumerating simulators. That
    is paid on every run of this command, booted simulators or not.

    Exits non-zero rather than returning, so a script or an agent that ignores
    the text still sees the failure.
    """
    if not processes or skip_cert_check:
        return

    import asyncio

    from server.config import get_auto_install_cert
    from server.device.controller import DeviceController
    from server.proxy import cert_manager
    from server.proxy.cert_preflight import simulators_without_cert

    # No CA yet means no trust to check and nothing anyone could install. It is
    # generated on the first proxy start (see lifecycle/setup.py), so on a fresh
    # machine this command legitimately runs before it exists -- and refusing
    # there would offer three resolutions of which two are impossible and the
    # third reads as a workaround. `device.py` guards the same way.
    if not cert_manager.get_cert_path().exists():
        return

    async def _check():
        controller = DeviceController()
        missing = await simulators_without_cert(controller)
        if not missing:
            return None, []
        if get_auto_install_cert():
            from server.proxy.cert_manager import install_cert

            failed = []
            for dev in missing:
                try:
                    await install_cert(controller, dev["udid"], device_name=dev["name"])
                    print(f"  Installed the CA on {dev['name']} ({dev['udid'][:8]})")
                except Exception as e:
                    # Not swallowed into the check's own error handler below.
                    # A failed install is not "could not check" -- the answer is
                    # known and it is bad, and proceeding would enable capture
                    # that cannot work for the user who asked us to handle this.
                    failed.append((dev, e))
            return None, failed
        return missing, []

    try:
        missing, failed = asyncio.run(_check())
    except Exception as e:
        # Fails open, like the preflight itself. Blocking capture over a bug in
        # the check would be worse than the state it prevents.
        print(f"Could not check certificate trust ({e}); continuing.")
        return

    if failed:
        print("auto_install_cert is set, but installing the CA failed:")
        for dev, err in failed:
            print(f"  {dev['name']} ({dev['udid'][:8]}): {err}")
        print()
        print("Capture from those simulators would fail. Install the CA by hand,")
        print("or rerun with --skip-cert-check to enable capture anyway.")
        sys.exit(1)

    if not missing:
        return

    names = ", ".join(f"{d['name']} ({d['udid'][:8]})" for d in missing)
    print(f"Refusing: {len(missing)} booted simulator(s) do not trust the mitmproxy CA.")
    print(f"  {names}")
    print()
    print("Every HTTPS request from them would fail, and nothing in the app would")
    print("point at the proxy as the cause. Three ways forward:")
    print("  - install the CA on those simulators, then rerun this")
    print("  - quern set-auto-install-cert on   (Quern installs it from now on)")
    print("  - quern enable-local-capture --skip-cert-check ...   (proceed anyway)")
    sys.exit(1)


def _cmd_enable_local_capture(
    process_names: list[str], skip_cert_check: bool = False,
) -> None:
    """Enable local capture mode for specific processes."""
    processes = process_names if process_names else ["MobileSafari", "com.apple.WebKit.Networking"]

    current = get_local_capture_processes()
    if current == processes:
        # Before the gate on purpose. This command changes nothing, so there is
        # no new routing to refuse -- and refusing here would contradict a
        # config.json that already says capture is on, while the server carries
        # on capturing. With auto_install_cert set it would also install a root
        # CA on behalf of a no-op.
        print(f"Local capture is already enabled for: {', '.join(processes)}")
        return

    # Before the write. Refusing after config.json has changed would leave the
    # refusal and the persisted state disagreeing.
    _local_capture_cert_gate(processes, skip_cert_check)

    removed = [p for p in current if p not in processes]
    if removed:
        print(f"  Removing from capture: {', '.join(removed)}")
        print("  (this sets the list rather than adding to it)")

    print("Enabling local capture mode.")
    print("This uses a macOS System Extension (mitmproxy-macos) to transparently")
    print("capture HTTP traffic from iOS Simulator processes without configuring")
    print("a system proxy.")
    print()
    print("On first use, macOS will prompt you to allow the Mitmproxy Redirector")
    print("system extension in System Settings > Privacy & Security.")

    set_local_capture_processes(processes)
    print()
    print(f"Local capture enabled for: {', '.join(processes)}")

    # Restart server if running
    state = read_state()
    if state and is_server_healthy(state.get("server_port", 9100)):
        print("Restarting server to apply changes...")
        pid = state.get("pid")
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            print("Server stopped. Start it again with: quern start")


def _cmd_disable_local_capture() -> None:
    """Disable local capture mode."""
    if not get_local_capture_processes():
        print("Local capture is already disabled.")
        return

    set_local_capture_processes([])
    print("Local capture disabled in ~/.quern/config.json")

    # Restart server if running
    state = read_state()
    if state and is_server_healthy(state.get("server_port", 9100)):
        print("Restarting server to apply changes...")
        pid = state.get("pid")
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            print("Server stopped. Start it again with: quern start")


def cli() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        # Without this the usage line reads "__main__.py", because the `quern`
        # wrapper execs `python3 -m server` and argparse takes argv[0].
        prog="quern",
        description="Quern — capture device logs for AI agents",
        epilog=(
            "Other commands:\n"
            "  help                          Show this message\n"
            "  version, --version, -V        Print the installed version\n"
            "  update [--tools]              Update to the latest release on your channel\n"
            "  set-channel [name]            Show or set the update channel (stable / beta)\n"
            "  set-auto-install-cert [on|off]\n"
            "                                Show or set automatic capture-certificate install\n"
            "  set-update-check [on|off]     Show or set the automatic update check\n"
            "  install-precommit-hook        Install the pre-commit checklist hook\n"
            "  menubar [status|open|install] Show, start, or install the Quern app (menu bar)\n"
            "  tunneld <cmd>                 Manage the tunneld LaunchDaemon\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(command=None)

    subparsers = parser.add_subparsers(dest="command")

    # start
    start_parser = subparsers.add_parser("start", help="Start the server")
    start_parser.add_argument(
        "--foreground", "-f", action="store_true", default=False,
        help="Run in foreground (don't daemonize)",
    )
    _add_server_flags(start_parser)

    # stop
    subparsers.add_parser("stop", help="Stop the running server")

    # restart
    restart_parser = subparsers.add_parser("restart", help="Restart the server")
    restart_parser.add_argument(
        "--foreground", "-f", action="store_true", default=False,
        help="Run in foreground (don't daemonize)",
    )
    _add_server_flags(restart_parser)

    # status
    subparsers.add_parser("status", help="Show server status")

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor", help="Report device-tool availability (read-only diagnostics)"
    )
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Reconcile Python dependencies if they are stale (same action as server startup)",
    )

    # setup
    # Declared so `quern help` lists it and test_readme_sync sees it. Normally
    # unreachable -- server/__main__.py handles it before the venv re-exec,
    # which is the point of the command -- but reachable via `python -m
    # server.main`, and a declared command that silently does nothing is worse
    # than one that is not declared. It is dispatched below.
    capture_parser = subparsers.add_parser(
        "capture-env",
        help="Write an environment report to attach to a bug report",
    )
    capture_parser.add_argument(
        "output", nargs="?",
        help="File to write (default: print to stdout)",
    )
    subparsers.add_parser(
        "check-updates",
        help="Check for a new release now, ignoring the once-a-day rate limit",
    )
    setup_parser = subparsers.add_parser(
        "setup", help="Check environment and install dependencies")
    # `server.__main__` dispatches setup before this parser is reached, and
    # parses the same flag itself. Declared here too so the two entry points
    # do not disagree about what `quern setup` accepts.
    setup_parser.add_argument(
        "-y", "--yes", action="store_true", dest="assume_yes",
        help="Answer prompts with their default (unattended)")

    # uninstall
    subparsers.add_parser("uninstall", help="Remove Quern and its dependencies")

    # regenerate-key (preserved)
    subparsers.add_parser("regenerate-key", help="Generate a new API key")

    # enable-local-capture / disable-local-capture
    enable_lc = subparsers.add_parser(
        "enable-local-capture",
        help="Enable local traffic capture via macOS System Extension",
    )
    enable_lc.add_argument(
        "processes", nargs="*", default=[],
        help=(
            "Process names to capture (default: MobileSafari and "
            "com.apple.WebKit.Networking). Safari's requests leave through the "
            "WebKit networking extension, so naming the app alone captures "
            "nothing."
        ),
    )
    enable_lc.add_argument(
        "--skip-cert-check",
        action="store_true",
        help=(
            "Enable capture even when a booted simulator does not trust the "
            "mitmproxy CA. Correct when deliberately exercising TLS failure."
        ),
    )
    subparsers.add_parser("disable-local-capture", help="Disable local traffic capture")

    # mcp-install / grant-full-perms (handled in __main__.py, listed here for help)
    subparsers.add_parser(
        "mcp-install", help="Install Quern MCP server into AI coding tools",
    )
    subparsers.add_parser(
        "grant-full-perms",
        help="Allow all quern MCP tools in Claude Code without prompting",
    )

    args, remaining = parser.parse_known_args()

    # Backward compat: no subcommand → start --foreground
    if args.command is None:
        # Re-parse with start defaults + foreground=True
        # Server flags (--no-proxy, etc.) live on start_parser, not the
        # top-level parser, so use remaining args from parse_known_args.
        start_parser.parse_args(remaining, namespace=args)
        args.command = "start"
        args.foreground = True
    elif remaining:
        # A subcommand matched, so whatever is left is a typo. Dropping it
        # silently meant `quern setup --yse` ran a full setup with the flag
        # discarded, which is worse than refusing: the caller believes they
        # opted in. `parse_known_args` is needed only for the no-subcommand
        # case above, where server flags live on `start_parser`.
        parser.error("unrecognised arguments: " + " ".join(remaining))

    # Fill port defaults
    if hasattr(args, "port"):
        _resolve_args(args)

    # Dispatch
    if args.command == "start":
        _cmd_start(args)
    elif args.command == "stop":
        _cmd_stop(args)
    elif args.command == "restart":
        _cmd_restart(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "doctor":
        _cmd_doctor(args)
    elif args.command == "regenerate-key":
        key = ServerConfig.regenerate_api_key()
        print(f"New API key: {key}")
    elif args.command == "enable-local-capture":
        _cmd_enable_local_capture(args.processes, args.skip_cert_check)
    elif args.command == "disable-local-capture":
        _cmd_disable_local_capture()
    elif args.command == "capture-env":
        from server.lifecycle.capture_env import run
        sys.exit(run(getattr(args, "output", None)))
    elif args.command == "check-updates":
        sys.exit(_cmd_check_updates())
    elif args.command == "setup":
        from server.lifecycle.setup import run_setup
        sys.exit(run_setup(assume_yes=getattr(args, "assume_yes", False)))
    elif args.command == "uninstall":
        from server.lifecycle.setup import run_uninstall
        sys.exit(run_uninstall())


if __name__ == "__main__":
    cli()
