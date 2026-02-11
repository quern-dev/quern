"""iOS Debug Server — main entry point.

Usage:
    ios-debug-server                  Start in foreground (backward compat)
    ios-debug-server start            Start as daemon
    ios-debug-server start -f         Start in foreground
    ios-debug-server stop             Stop a running daemon
    ios-debug-server restart          Restart the daemon
    ios-debug-server status           Show server status
    ios-debug-server regenerate-key   Generate a new API key
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
from server.config import ServerConfig
from server.lifecycle.daemon import LOG_FILE, daemonize, _print_status
from server.lifecycle.ports import (
    DEFAULT_PROXY_PORT,
    DEFAULT_SERVER_PORT,
    find_available_port,
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
from server.sources.crash import CrashAdapter
from server.sources.oslog import OslogAdapter
from server.sources.proxy import ProxyAdapter
from server.sources.syslog import SyslogAdapter
from server.storage.ring_buffer import RingBuffer
from server.api.builds import router as builds_router
from server.api.crashes import router as crashes_router
from server.api.device import router as device_router
from server.api.logs import router as logs_router
from server.api.proxy import router as proxy_router

logger = logging.getLogger("ios-debug-server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage server startup and shutdown."""
    config: ServerConfig = app.state.config
    buffer: RingBuffer = app.state.ring_buffer

    # Processing pipeline: adapter → deduplicator → ring buffer
    dedup = Deduplicator(on_entry=buffer.append)
    dedup.start()
    app.state.deduplicator = dedup

    # Start source adapters (all feed into the deduplicator)
    adapters: dict[str, BaseSourceAdapter] = {}

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
            pull_from_device=app.state.pull_crashes,
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
    )
    adapters["proxy"] = proxy
    app.state.proxy_adapter = proxy
    if app.state.enable_proxy:
        await proxy.start()

    app.state.source_adapters = adapters

    # Device controller (Phase 3)
    device_controller = DeviceController()
    app.state.device_controller = device_controller
    tools = await device_controller.check_tools()
    logger.info("Device tools: %s", tools)

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

    for adapter in adapters.values():
        await adapter.stop()
    await dedup.stop()

    # Clean up state file (if daemon mode wrote one)
    remove_state()
    logger.info("Server stopped")


def create_app(
    config: ServerConfig | None = None,
    process_filter: str | None = None,
    enable_oslog: bool = True,
    subsystem_filter: str | None = None,
    enable_crash: bool = True,
    crash_dir: Path | None = None,
    pull_crashes: bool = False,
    enable_proxy: bool = True,
    proxy_port: int = 9101,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = ServerConfig()

    app = FastAPI(
        title="iOS Debug Server",
        version="0.1.0",
        description="iOS debug log capture and AI context server",
        lifespan=lifespan,
    )

    # Store shared state
    app.state.config = config
    app.state.ring_buffer = RingBuffer(max_size=config.ring_buffer_size)
    app.state.process_filter = process_filter
    app.state.enable_oslog = enable_oslog
    app.state.subsystem_filter = subsystem_filter
    app.state.enable_crash = enable_crash
    app.state.crash_dir = crash_dir
    app.state.pull_crashes = pull_crashes
    app.state.enable_proxy = enable_proxy
    app.state.proxy_port = proxy_port
    app.state.source_adapters = {}
    app.state.crash_adapter = None
    app.state.build_adapter = None
    app.state.proxy_adapter = None
    app.state.flow_store = None
    app.state.device_controller = None

    # Auth middleware
    app.add_middleware(APIKeyMiddleware, api_key=config.api_key)

    # Routes
    app.include_router(logs_router)
    app.include_router(crashes_router)
    app.include_router(builds_router)
    app.include_router(proxy_router)
    app.include_router(device_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/v1/health")
    async def api_health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

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
        "--oslog", action="store_true", default=None,
        help="Enable OSLog adapter (default: enabled on macOS)",
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
        help="Directory to watch for crash reports (default: ~/.ios-debug-server/crashes)",
    )
    parser.add_argument(
        "--pull-crashes", action="store_true", default=False,
        help="Run idevicecrashreport to pull crashes from device",
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
    """Check if a PID belongs to an ios-debug-server process (PID reuse guard)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        cmd = result.stdout.strip()
        return "ios-debug-server" in cmd or "server.main" in cmd or "uvicorn" in cmd
    except Exception:
        return False


def _cmd_start(args: argparse.Namespace) -> None:
    """Start the server (daemon or foreground)."""
    # Check for existing instance
    existing = read_state()
    if existing and is_server_healthy(existing["server_port"]):
        print("Server already running")
        _print_status(existing)
        sys.exit(0)

    if existing:
        # Stale state — clean up
        remove_state()

    # Resolve ports (scan for available)
    # Exclude the proxy port so the server never steals it
    server_port = find_available_port(
        args.port, host=args.host, exclude={args.proxy_port},
    )
    if server_port != args.port:
        print(f"Port {args.port} in use, using {server_port}")

    proxy_port = args.proxy_port
    enable_proxy = not args.no_proxy
    if enable_proxy:
        proxy_port = find_available_port(
            args.proxy_port,
            host=args.host,
            exclude={server_port},
        )
        if proxy_port != args.proxy_port:
            print(f"Proxy port {args.proxy_port} in use, using {proxy_port}")

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

    enable_oslog = not args.no_oslog and (
        args.oslog is True or platform.system() == "Darwin"
    )
    enable_crash = not args.no_crash

    # Write state file
    write_state({
        "pid": os.getpid(),
        "server_port": server_port,
        "proxy_port": proxy_port,
        "proxy_enabled": enable_proxy,
        "proxy_status": "starting" if enable_proxy else "disabled",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_key": config.api_key,
        "active_devices": [],
    })

    if args.foreground:
        print(f"iOS Debug Server v0.1.0")
        print(f"  http://{config.host}:{server_port}")
        print(f"  API key: {config.api_key[:8]}...{config.api_key[-4:]}")
        print(f"  API key file: ~/.ios-debug-server/api-key")
        if args.process:
            print(f"  Process filter: {args.process}")
        if enable_oslog:
            sub = args.subsystem or "(all)"
            print(f"  OSLog: enabled (subsystem: {sub})")
        if enable_crash:
            crash_path = args.crash_dir or "~/.ios-debug-server/crashes"
            print(f"  Crash watcher: enabled (dir: {crash_path})")
        if enable_proxy:
            print(f"  Proxy: enabled (port: {proxy_port})")
        print()

    app = create_app(
        config=config,
        process_filter=args.process,
        enable_oslog=enable_oslog,
        subsystem_filter=args.subsystem,
        enable_crash=enable_crash,
        crash_dir=args.crash_dir,
        pull_crashes=args.pull_crashes,
        enable_proxy=enable_proxy,
        proxy_port=proxy_port,
    )

    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=server_port,
        log_level="debug" if args.verbose else "info",
    )
    server = uvicorn.Server(uv_config)
    server.run()


def _cmd_stop(args: argparse.Namespace) -> None:
    """Stop the running server daemon."""
    state = read_state()
    if not state:
        print("No server running")
        return

    pid = state.get("pid")
    if not pid:
        remove_state()
        print("No server running (stale state cleaned up)")
        return

    # Check if process exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        remove_state()
        print("No server running (stale state cleaned up)")
        return

    # PID reuse guard
    if not _is_our_process(pid):
        remove_state()
        print(f"Warning: PID {pid} is not an ios-debug-server process (stale state cleaned up)")
        return

    # Send SIGTERM
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

    # Force kill
    print("Server didn't stop gracefully, sending SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
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
        print(f"  State file may be stale. Run 'ios-debug-server stop' to clean up.")
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


def cli() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="iOS Debug Server — capture device logs for AI agents",
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

    # regenerate-key (preserved)
    subparsers.add_parser("regenerate-key", help="Generate a new API key")

    args = parser.parse_args()

    # Backward compat: no subcommand → start --foreground
    if args.command is None:
        # Re-parse with start defaults + foreground=True
        # Check if any server flags were passed on the bare command
        start_parser.parse_args(sys.argv[1:], namespace=args)
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


if __name__ == "__main__":
    cli()
