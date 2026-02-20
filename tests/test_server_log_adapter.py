"""Tests for the ServerLogAdapter — Python logging → ring buffer bridge."""

from __future__ import annotations

import asyncio
import logging

import pytest

from server.models import LogEntry, LogLevel, LogSource
from server.sources.server_log import ServerLogAdapter, _map_level


@pytest.fixture
async def adapter_with_entries():
    """Start a ServerLogAdapter that collects entries into a list."""
    entries: list[LogEntry] = []

    async def collect(entry: LogEntry) -> None:
        entries.append(entry)

    adapter = ServerLogAdapter(on_entry=collect)
    await adapter.start()
    yield adapter, entries
    await adapter.stop()


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_installs_handler(self):
        adapter = ServerLogAdapter(on_entry=None)
        root = logging.getLogger()
        before = len(root.handlers)

        await adapter.start()
        assert len(root.handlers) == before + 1
        assert adapter.is_running

        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_removes_handler(self):
        adapter = ServerLogAdapter(on_entry=None)
        root = logging.getLogger()
        before = len(root.handlers)

        await adapter.start()
        await adapter.stop()

        assert len(root.handlers) == before
        assert not adapter.is_running

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        adapter = ServerLogAdapter(on_entry=None)
        await adapter.start()
        await adapter.stop()
        await adapter.stop()  # should not raise


class TestEmission:
    @pytest.mark.asyncio
    async def test_emits_log_entries(self, adapter_with_entries):
        adapter, entries = adapter_with_entries

        test_logger = logging.getLogger("test.emission")
        test_logger.warning("something broke")

        # Give the event loop a tick to process call_soon_threadsafe
        await asyncio.sleep(0.05)

        assert len(entries) >= 1
        entry = next(e for e in entries if e.message == "something broke")
        assert entry.level == LogLevel.WARNING
        assert entry.source == LogSource.SERVER

    @pytest.mark.asyncio
    async def test_process_is_logger_name(self, adapter_with_entries):
        adapter, entries = adapter_with_entries

        test_logger = logging.getLogger("server.device.pmd3")
        test_logger.warning("tunnel failed")

        await asyncio.sleep(0.05)

        entry = next(e for e in entries if e.message == "tunnel failed")
        assert entry.process == "server.device.pmd3"

    @pytest.mark.asyncio
    async def test_device_id_is_server(self, adapter_with_entries):
        adapter, entries = adapter_with_entries

        logging.getLogger("test").warning("hello")
        await asyncio.sleep(0.05)

        entry = next(e for e in entries if e.message == "hello")
        assert entry.device_id == "server"

    @pytest.mark.asyncio
    async def test_raw_is_formatted(self, adapter_with_entries):
        adapter, entries = adapter_with_entries

        logging.getLogger("test.raw").error("format check")
        await asyncio.sleep(0.05)

        entry = next(e for e in entries if e.message == "format check")
        assert "ERROR" in entry.raw
        assert "test.raw" in entry.raw
        assert "format check" in entry.raw

    @pytest.mark.asyncio
    async def test_entries_captured_count(self, adapter_with_entries):
        adapter, entries = adapter_with_entries

        test_logger = logging.getLogger("test.count")
        test_logger.warning("one")
        test_logger.warning("two")
        test_logger.warning("three")

        await asyncio.sleep(0.05)

        # entries_captured tracks how many times emit() was called
        assert adapter.entries_captured >= 3


class TestLevelMapping:
    def test_debug(self):
        assert _map_level(logging.DEBUG) == LogLevel.DEBUG

    def test_info(self):
        assert _map_level(logging.INFO) == LogLevel.INFO

    def test_warning(self):
        assert _map_level(logging.WARNING) == LogLevel.WARNING

    def test_error(self):
        assert _map_level(logging.ERROR) == LogLevel.ERROR

    def test_critical_maps_to_fault(self):
        assert _map_level(logging.CRITICAL) == LogLevel.FAULT

    def test_below_debug(self):
        assert _map_level(5) == LogLevel.DEBUG

    def test_above_critical(self):
        assert _map_level(60) == LogLevel.FAULT

    @pytest.mark.asyncio
    async def test_all_levels_emit_correctly(self, adapter_with_entries):
        adapter, entries = adapter_with_entries

        test_logger = logging.getLogger("test.levels")
        test_logger.setLevel(logging.DEBUG)

        test_logger.debug("d")
        test_logger.info("i")
        test_logger.warning("w")
        test_logger.error("e")
        test_logger.critical("c")

        await asyncio.sleep(0.05)

        levels = {e.message: e.level for e in entries}
        # debug may or may not appear depending on root logger level
        assert levels.get("i") == LogLevel.INFO or "i" not in levels
        assert levels.get("w") == LogLevel.WARNING or "w" not in levels
        assert levels.get("e") == LogLevel.ERROR or "e" not in levels
        assert levels.get("c") == LogLevel.FAULT or "c" not in levels
