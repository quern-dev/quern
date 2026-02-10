"""iOS Debug Server — main entry point.

Usage:
    ios-debug-server                  Start the server with defaults
    ios-debug-server --port 9200      Start on a custom port
    ios-debug-server --process MyApp  Filter logs to a specific process
    ios-debug-server regenerate-key   Generate a new API key
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from server.auth import APIKeyMiddleware
from server.config import ServerConfig
from server.storage.ring_buffer import RingBuffer
from server.sources.syslog import SyslogAdapter
from server.api.logs import router as logs_router

logger = logging.getLogger("ios-debug-server")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage server startup and shutdown."""
    config: ServerConfig = app.state.config
    buffer: RingBuffer = app.state.ring_buffer

    # Start source adapters
    adapters: dict[str, SyslogAdapter] = {}

    syslog = SyslogAdapter(
        device_id=config.default_device_id,
        on_entry=buffer.append,
        process_filter=app.state.process_filter,
    )
    adapters["syslog"] = syslog
    await syslog.start()

    app.state.source_adapters = adapters

    logger.info(
        "Server started on http://%s:%d — API key: %s...%s",
        config.host,
        config.port,
        config.api_key[:8],
        config.api_key[-4:],
    )

    yield

    # Shutdown: stop all adapters
    for adapter in adapters.values():
        await adapter.stop()
    logger.info("Server stopped")


def create_app(config: ServerConfig | None = None, process_filter: str | None = None) -> FastAPI:
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
    app.state.source_adapters = {}

    # Auth middleware
    app.add_middleware(APIKeyMiddleware, api_key=config.api_key)

    # Routes
    app.include_router(logs_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/v1/health")
    async def api_health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app


def cli() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="iOS Debug Server — capture device logs for AI agents"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "regenerate-key"],
        help="Command to run (default: serve)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="Bind port (default: 9100)")
    parser.add_argument("--process", "-p", default=None, help="Filter logs to this process name")
    parser.add_argument(
        "--buffer-size", type=int, default=10_000, help="Ring buffer size (default: 10000)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.command == "regenerate-key":
        key = ServerConfig.regenerate_api_key()
        print(f"New API key: {key}")
        return

    config = ServerConfig(
        host=args.host,
        port=args.port,
        ring_buffer_size=args.buffer_size,
    )

    print(f"🔧 iOS Debug Server v0.1.0")
    print(f"   http://{config.host}:{config.port}")
    print(f"   API key: {config.api_key[:8]}...{config.api_key[-4:]}")
    print(f"   API key file: ~/.ios-debug-server/api-key")
    if args.process:
        print(f"   Process filter: {args.process}")
    print()

    app = create_app(config=config, process_filter=args.process)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    cli()
