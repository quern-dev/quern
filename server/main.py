"""Quern Debug Server — main entry point.

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
import logging
import os
import platform
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from server.auth import APIKeyMiddleware
from server.device.controller import DeviceController
from server.config import ServerConfig, get_local_capture_processes, set_local_capture_processes
from server.lifecycle.daemon import LOG_FILE, daemonize, _print_status
from server.lifecycle.ports import (
    DEFAULT_PROXY_PORT,
    DEFAULT_SERVER_PORT,
    find_available_port,
    reclaim_port,
)
from server.lifecycle.state import (
    is_server_healthy,
    read_state,
    remove_state,
    write_state,
)
from server.lifecycle.watchdog import proxy_watchdog
from server.processing.deduplicator import Deduplicator
from server.proxy.flow_store import FlowStore
from server.sources import BaseSourceAdapter
from server.sources.build import BuildAdapter
from server.sources.crash import CrashAdapter, DIAGNOSTIC_REPORTS_DIR
from server.sources.oslog import OslogAdapter
from server.sources.proxy import ProxyAdapter
from server.sources.server_log import ServerLogAdapter
from server.sources.syslog import SyslogAdapter
from server.storage.ring_buffer import RingBuffer
from server.api.builds import router as builds_router
from server.api.crashes import router as crashes_router
from server.api.device import router as device_router
from server.api.device_ui import router as device_ui_router
from server.api.device_pool import router as device_pool_router
from server.api.logs import router as logs_router
from server.api.proxy import router as proxy_router
from server.api.proxy_intercept import router as proxy_intercept_router
from server.api.proxy_certs import router as proxy_certs_router
from server.api.wda import router as wda_router

logger = logging.getLogger("quern-debug-server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage server startup and shutdown."""
    config: ServerConfig = app.state.config
    buffer: RingBuffer = app.state.ring_buffer

    # Processing pipeline: adapter → deduplicator → ring buffer
    dedup = Deduplicator(on_entry=buffer.append)
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
            from server.proxy.system_proxy import (
                detect_active_interface,
                snapshot_system_proxy,
            )
            from server.lifecycle.state import update_state, read_state

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
    app.state.sim_log_adapters: dict[str, "SimulatorLogAdapter"] = {}

    # Physical device log adapters — managed on-demand via API
    app.state.device_log_adapters: dict[str, "PhysicalDeviceLogAdapter"] = {}

    # Device controller (Phase 3)
    device_controller = DeviceController()
    app.state.device_controller = device_controller
    tools = await device_controller.check_tools()
    logger.info("Device tools: %s", tools)

    # Warn about missing tools
    if not tools.get("simctl"):
        logger.warning(
            "simctl not available — device management and screenshots disabled. "
            "Install Xcode Command Line Tools: xcode-select --install"
        )
    if not tools.get("idb"):
        logger.warning(
            "idb not available — UI automation (tap, swipe, accessibility tree) disabled. "
            "Install with: pip install fb-idb && brew install idb-companion"
        )

    # Device pool (Phase 4b-alpha)
    from server.device.pool import DevicePool
    device_pool = DevicePool(device_controller)
    device_controller._pool = device_pool  # Enable pool-aware resolution
    app.state.device_pool = device_pool

    # Refresh pool state on startup
    await device_pool.refresh_from_simctl()

    # Cleanup stale claims from previous runs
    released = await device_pool.cleanup_stale_claims()
    if released:
        logger.info("Cleaned up %d stale device claims on startup", len(released))

    # Warm device caches in the background (device type dispatch, WDA os_versions)
    async def _warmup_devices():
        try:
            devices = await device_controller.list_devices()
            logger.info("Device warmup: discovered %d device(s)", len(devices))
        except Exception:
            logger.debug("Device warmup failed (non-fatal)", exc_info=True)

    warmup_task = asyncio.create_task(_warmup_devices())

    # Launch proxy watchdog if proxy is enabled
    watchdog_task = None
    if app.state.enable_proxy:
        watchdog_task = asyncio.create_task(
            proxy_watchdog(lambda: app.state.proxy_adapter)
        )

    logger.info(
        "Server started on http://%s:%d — API key: %s...%s",
        config.host,
        config.port,
        config.api_key[:8],
        config.api_key[-4:],
    )

    yield

    # Shutdown: cancel watchdog, stop adapters, flush deduplicator
    if watchdog_task and not watchdog_task.done():
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass

    # Shutdown WDA client (cancels idle task, deletes sessions, kills port-forwards)
    # Note: does NOT kill xcodebuild processes — they persist across restarts
    if device_controller:
        await device_controller.wda_client.close()

    for adapter in adapters.values():
        await adapter.stop()
    for sim_adapter in app.state.sim_log_adapters.values():
        await sim_adapter.stop()
    for dev_adapter in app.state.device_log_adapters.values():
        await dev_adapter.stop()
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
        title="Quern Debug Server",
        version="0.1.0",
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
    app.state.source_adapters = {}
    app.state.crash_adapter = None
    app.state.build_adapter = None
    app.state.proxy_adapter = None
    app.state.flow_store = None
    app.state.device_controller = None
    app.state.device_pool = None
    app.state.sim_log_adapters = {}
    app.state.device_log_adapters = {}

    # Auth middleware
    app.add_middleware(APIKeyMiddleware, api_key=config.api_key)

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

    @app.get("/health")
    async def health() -> dict:
        """Health check with tool availability status and cache stats."""
        tools = {}
        cache_stats = {}
        if hasattr(app.state, "device_controller") and app.state.device_controller:
            tools = await app.state.device_controller.check_tools()
            cache_stats = app.state.device_controller.get_cache_stats()
        return {
            "status": "ok",
            "version": "0.1.0",
            "tools": tools,
            "ui_cache": cache_stats,
        }

    @app.get("/api/v1/health")
    async def api_health() -> dict:
        """Health check with tool availability status and cache stats."""
        tools = {}
        cache_stats = {}
        if hasattr(app.state, "device_controller") and app.state.device_controller:
            tools = await app.state.device_controller.check_tools()
            cache_stats = app.state.device_controller.get_cache_stats()
        return {
            "status": "ok",
            "version": "0.1.0",
            "tools": tools,
            "ui_cache": cache_stats,
        }

    return app


def _add_server_flags(parser: argparse.ArgumentParser) -> None:
    """Add shared server flags to a subcommand parser."""
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
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
        "--simulator-crashes", action=argparse.BooleanOptionalAction, default=True,
        help="Watch ~/Library/Logs/DiagnosticReports/ for simulator crash reports (default: enabled)",
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


def _cmd_start(args: argparse.Namespace) -> None:
    """Start the server (daemon or foreground)."""
    # Always rebuild MCP server to ensure dist/ is current
    from server.__main__ import _ensure_mcp_built
    if not _ensure_mcp_built(quiet=True):
        print("Warning: MCP server build failed — MCP tools may be stale")

    # Non-blocking update check (once per 24h)
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
        # Only the child process reaches here

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = ServerConfig(
        host=args.host,
        port=server_port,
        ring_buffer_size=args.buffer_size,
    )

    enable_syslog = args.syslog is True and not args.no_syslog
    enable_oslog = args.oslog is True and not args.no_oslog
    enable_crash = not args.no_crash
    local_capture_processes = get_local_capture_processes() if enable_proxy else []

    # Write state file
    write_state({
        "pid": os.getpid(),
        "server_port": server_port,
        "proxy_port": proxy_port,
        "proxy_enabled": enable_proxy,
        "proxy_status": "starting" if enable_proxy else "disabled",
        "local_capture": local_capture_processes,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_key": config.api_key,
        "active_devices": [],
    })

    if args.foreground:
        print(f"Quern Debug Server v0.1.0")
        print(f"  http://{config.host}:{server_port}")
        print(f"  API key: {config.api_key[:8]}...{config.api_key[-4:]}")
        print(f"  API key file: ~/.quern/api-key")
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
                print(f"  Local capture: disabled")
                print(f"    Capture simulator traffic without a system proxy:")
                print(f"    Run: quern enable-local-capture [process ...]")
        if args.on_crash:
            print(f"  On-crash hook: {args.on_crash}")
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
        print(f"  State file may be stale. Run 'quern-debug-server stop' to clean up.")
        sys.exit(1)

    # Calculate uptime
    started = state.get("started_at")
    if started:
        try:
            start_dt = datetime.fromisoformat(started)
            uptime = datetime.now(timezone.utc) - start_dt
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            state["_uptime"] = f"{hours}h {minutes}m {seconds}s"
        except (ValueError, TypeError):
            pass

    _print_status(state)
    if "_uptime" in state:
        print(f"  Uptime:     {state['_uptime']}")
    sys.exit(0)


def _cmd_enable_local_capture(process_names: list[str]) -> None:
    """Enable local capture mode for specific processes."""
    processes = process_names if process_names else ["MobileSafari", "com.apple.WebKit.Networking"]

    current = get_local_capture_processes()
    if current == processes:
        print(f"Local capture is already enabled for: {', '.join(processes)}")
        return

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
        description="Quern Debug Server — capture device logs for AI agents",
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

    # setup
    subparsers.add_parser("setup", help="Check environment and install dependencies")

    # regenerate-key (preserved)
    subparsers.add_parser("regenerate-key", help="Generate a new API key")

    # enable-local-capture / disable-local-capture
    enable_lc = subparsers.add_parser(
        "enable-local-capture",
        help="Enable local traffic capture via macOS System Extension",
    )
    enable_lc.add_argument(
        "processes", nargs="*", default=[],
        help="Process names to capture (default: MobileSafari)",
    )
    subparsers.add_parser("disable-local-capture", help="Disable local traffic capture")

    args, remaining = parser.parse_known_args()

    # Backward compat: no subcommand → start --foreground
    if args.command is None:
        # Re-parse with start defaults + foreground=True
        # Server flags (--no-proxy, etc.) live on start_parser, not the
        # top-level parser, so use remaining args from parse_known_args.
        start_parser.parse_args(remaining, namespace=args)
        args.command = "start"
        args.foreground = True

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
    elif args.command == "regenerate-key":
        key = ServerConfig.regenerate_api_key()
        print(f"New API key: {key}")
    elif args.command == "enable-local-capture":
        _cmd_enable_local_capture(args.processes)
    elif args.command == "disable-local-capture":
        _cmd_disable_local_capture()
    elif args.command == "setup":
        from server.lifecycle.setup import run_setup
        sys.exit(run_setup())


if __name__ == "__main__":
    cli()
