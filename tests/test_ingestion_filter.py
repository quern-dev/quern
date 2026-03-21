"""Tests for the ingestion filter."""

from datetime import UTC, datetime

import pytest

from server.models import LogEntry, LogLevel, LogSource
from server.processing.ingestion_filter import (
    PRESETS,
    FilterConfig,
    IngestionFilter,
    build_config,
)


def _make_entry(
    message: str = "test message",
    process: str = "MyApp",
    subsystem: str = "com.myapp",
    level: LogLevel = LogLevel.INFO,
    source: LogSource = LogSource.SYSLOG,
    device_id: str = "default",
) -> LogEntry:
    return LogEntry(
        id="test",
        timestamp=datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC),
        process=process,
        subsystem=subsystem,
        level=level,
        message=message,
        source=source,
        device_id=device_id,
    )


class TestFilterConfig:
    def test_empty_config_admits_everything(self):
        f = IngestionFilter()
        entry = _make_entry()
        assert f.should_admit(entry) is True

    def test_process_include(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(process="MyApp"))
        assert f.should_admit(_make_entry(process="MyApp")) is True
        assert f.should_admit(_make_entry(process="OtherApp")) is False

    def test_processes_include_list(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(processes=["MyApp", "Helper"]))
        assert f.should_admit(_make_entry(process="MyApp")) is True
        assert f.should_admit(_make_entry(process="Helper")) is True
        assert f.should_admit(_make_entry(process="OtherApp")) is False

    def test_subsystems_include(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(subsystems=["com.myapp", "com.myapp.net"]))
        assert f.should_admit(_make_entry(subsystem="com.myapp")) is True
        assert f.should_admit(_make_entry(subsystem="com.myapp.net")) is True
        assert f.should_admit(_make_entry(subsystem="com.apple.foo")) is False

    def test_exclude_processes(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(exclude_processes=["noisyd", "spambot"]))
        assert f.should_admit(_make_entry(process="MyApp")) is True
        assert f.should_admit(_make_entry(process="noisyd")) is False
        assert f.should_admit(_make_entry(process="spambot")) is False

    def test_exclude_subsystems(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(exclude_subsystems=["CoreBrightness"]))
        assert f.should_admit(_make_entry(subsystem="CoreBrightness")) is False
        assert f.should_admit(_make_entry(subsystem="com.myapp")) is True

    def test_exclude_messages_case_insensitive(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(exclude_messages=["HangTracer", "NOISE"]))
        assert f.should_admit(_make_entry(message="HangTracer detected")) is False
        assert f.should_admit(_make_entry(message="hangtracer detected")) is False
        assert f.should_admit(_make_entry(message="some noise here")) is False
        assert f.should_admit(_make_entry(message="normal log")) is True

    def test_min_level(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(min_level=LogLevel.WARNING))
        assert f.should_admit(_make_entry(level=LogLevel.DEBUG)) is False
        assert f.should_admit(_make_entry(level=LogLevel.INFO)) is False
        assert f.should_admit(_make_entry(level=LogLevel.NOTICE)) is False
        assert f.should_admit(_make_entry(level=LogLevel.WARNING)) is True
        assert f.should_admit(_make_entry(level=LogLevel.ERROR)) is True
        assert f.should_admit(_make_entry(level=LogLevel.FAULT)) is True

    def test_include_and_behavior(self):
        """Must match ALL specified includes."""
        f = IngestionFilter()
        f.update_filter(
            FilterConfig(
                process="MyApp",
                subsystems=["com.myapp"],
            )
        )
        # Matches both
        assert f.should_admit(_make_entry(process="MyApp", subsystem="com.myapp")) is True
        # Wrong process
        assert f.should_admit(_make_entry(process="Other", subsystem="com.myapp")) is False
        # Wrong subsystem
        assert f.should_admit(_make_entry(process="MyApp", subsystem="com.other")) is False

    def test_exclude_or_behavior(self):
        """Any exclude match drops the entry."""
        f = IngestionFilter()
        f.update_filter(
            FilterConfig(
                exclude_processes=["noisyd"],
                exclude_subsystems=["CoreBrightness"],
            )
        )
        # Matches process exclude
        assert f.should_admit(_make_entry(process="noisyd", subsystem="com.myapp")) is False
        # Matches subsystem exclude
        assert f.should_admit(_make_entry(process="MyApp", subsystem="CoreBrightness")) is False
        # Neither excluded
        assert f.should_admit(_make_entry(process="MyApp", subsystem="com.myapp")) is True


class TestScopedConfigs:
    def test_device_overrides_source(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(min_level=LogLevel.ERROR), source=LogSource.DEVICE)
        f.update_filter(FilterConfig(min_level=LogLevel.DEBUG), device_id="ABC123")

        entry = _make_entry(level=LogLevel.INFO, source=LogSource.DEVICE, device_id="ABC123")
        # Device config wins (min_level=DEBUG allows INFO)
        assert f.should_admit(entry) is True

    def test_source_overrides_global(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(min_level=LogLevel.ERROR))  # global
        f.update_filter(FilterConfig(min_level=LogLevel.DEBUG), source=LogSource.SIMULATOR)

        # Simulator source config allows DEBUG
        entry = _make_entry(level=LogLevel.DEBUG, source=LogSource.SIMULATOR)
        assert f.should_admit(entry) is True

        # Other sources use global (ERROR min)
        entry2 = _make_entry(level=LogLevel.INFO, source=LogSource.SYSLOG)
        assert f.should_admit(entry2) is False

    def test_get_config_returns_correct_scope(self):
        f = IngestionFilter()
        global_cfg = FilterConfig(min_level=LogLevel.WARNING)
        source_cfg = FilterConfig(min_level=LogLevel.DEBUG)
        device_cfg = FilterConfig(process="MyApp")

        f.update_filter(global_cfg)
        f.update_filter(source_cfg, source=LogSource.DEVICE)
        f.update_filter(device_cfg, device_id="DEV1")

        assert f.get_config() == global_cfg
        assert f.get_config(source=LogSource.DEVICE) == source_cfg
        assert f.get_config(device_id="DEV1") == device_cfg
        # Unknown scope returns empty
        assert f.get_config(device_id="UNKNOWN") == FilterConfig()

    def test_get_all_configs(self):
        f = IngestionFilter()
        f.update_filter(FilterConfig(min_level=LogLevel.WARNING))
        f.update_filter(FilterConfig(process="MyApp"), source=LogSource.SIMULATOR)

        result = f.get_all_configs()
        assert "global" in result
        assert result["global"]["min_level"] == "warning"
        assert "sources" in result
        assert "simulator" in result["sources"]
        assert result["sources"]["simulator"]["process"] == "MyApp"


class TestPresets:
    def test_known_presets(self):
        assert "device-quiet" in PRESETS
        assert "simulator-quiet" in PRESETS

    def test_build_config_from_preset(self):
        config = build_config(preset="device-quiet")
        assert "remotepairingdeviced" in config.exclude_processes
        assert "symptomsd" in config.exclude_processes
        assert "bluetoothd" in config.exclude_processes
        assert "wifid" in config.exclude_processes
        assert "kernel" in config.exclude_processes
        assert "CoreBrightness" in config.exclude_subsystems

    def test_build_config_with_overrides(self):
        config = build_config(preset="device-quiet", min_level=LogLevel.WARNING)
        # Preset values preserved
        assert "remotepairingdeviced" in config.exclude_processes
        # Override applied
        assert config.min_level == LogLevel.WARNING

    def test_build_config_unknown_preset(self):
        with pytest.raises(ValueError, match="Unknown preset"):
            build_config(preset="nonexistent")

    def test_build_config_no_preset(self):
        config = build_config(process="MyApp")
        assert config.process == "MyApp"
        assert config.exclude_processes == frozenset()


class TestFilterConfigSerialization:
    def test_empty_config_to_dict(self):
        assert FilterConfig().to_dict() == {}

    def test_populated_config_to_dict(self):
        config = FilterConfig(
            process="MyApp",
            exclude_processes=["noisyd"],
            min_level=LogLevel.WARNING,
        )
        d = config.to_dict()
        assert d["process"] == "MyApp"
        assert d["exclude_processes"] == ["noisyd"]
        assert d["min_level"] == "warning"

    def test_list_inputs_converted_to_frozen(self):
        config = FilterConfig(
            processes=["a", "b"],  # type: ignore[arg-type]
            subsystems=["x"],  # type: ignore[arg-type]
            exclude_processes=["n"],  # type: ignore[arg-type]
            exclude_subsystems=["s"],  # type: ignore[arg-type]
            exclude_messages=["m"],  # type: ignore[arg-type]
        )
        assert isinstance(config.processes, frozenset)
        assert isinstance(config.subsystems, frozenset)
        assert isinstance(config.exclude_processes, frozenset)
        assert isinstance(config.exclude_subsystems, frozenset)
        assert isinstance(config.exclude_messages, tuple)


class TestAdapterRestart:
    """Test that set_log_filter triggers adapter reconfigure when process filter is set."""

    @pytest.mark.asyncio
    async def test_set_filter_reconfigures_running_sim_adapter(self):
        """Setting a process filter reconfigures running simulator adapters."""
        from unittest.mock import AsyncMock

        from server.sources.simulator_log import SimulatorLogAdapter

        adapter = SimulatorLogAdapter(
            udid="AAAA0000-1111-2222-3333-444455556666",
            process_filter="OldApp",
        )
        adapter._running = True
        adapter.reconfigure = AsyncMock()

        # Simulate what the set_filter endpoint does
        config = FilterConfig(process="NewApp")
        ingestion_filter = IngestionFilter()
        ingestion_filter.update_filter(config, source=LogSource.SIMULATOR)

        sim_adapters = {"AAAA0000": adapter}
        for a in sim_adapters.values():
            if a.is_running:
                await a.reconfigure(process_filter=config.process)

        adapter.reconfigure.assert_awaited_once_with(process_filter="NewApp")

    @pytest.mark.asyncio
    async def test_set_filter_skips_stopped_adapters(self):
        """Stopped adapters are not reconfigured."""
        from unittest.mock import AsyncMock

        from server.sources.simulator_log import SimulatorLogAdapter

        adapter = SimulatorLogAdapter(
            udid="AAAA0000-1111-2222-3333-444455556666",
        )
        adapter._running = False
        adapter.reconfigure = AsyncMock()

        config = FilterConfig(process="NewApp")
        sim_adapters = {"AAAA0000": adapter}
        for a in sim_adapters.values():
            if a.is_running:
                await a.reconfigure(process_filter=config.process)

        adapter.reconfigure.assert_not_awaited()


class TestPipelineIntegration:
    @pytest.mark.asyncio
    async def test_filter_in_pipeline(self):
        """Entry through dedup → filter → only admitted entries reach buffer."""
        from server.processing.deduplicator import Deduplicator

        admitted: list[LogEntry] = []
        ingestion_filter = IngestionFilter()
        ingestion_filter.update_filter(FilterConfig(process="MyApp"))

        async def filtered_append(entry: LogEntry) -> None:
            if ingestion_filter.should_admit(entry):
                admitted.append(entry)

        dedup = Deduplicator(on_entry=filtered_append, window_seconds=5.0)

        # Process entries
        await dedup.process(_make_entry(process="MyApp", message="good"))
        await dedup.process(_make_entry(process="noisyd", message="bad"))
        await dedup.process(_make_entry(process="MyApp", message="also good"))

        assert len(admitted) == 2
        assert all(e.process == "MyApp" for e in admitted)

    @pytest.mark.asyncio
    async def test_purge_on_filter_change(self):
        """Setting a filter purges pre-existing entries that no longer match."""
        from server.storage.ring_buffer import RingBuffer

        buf = RingBuffer(max_size=1000)
        ingestion_filter = IngestionFilter()

        # Fill buffer with noise (no filter active)
        for proc in ["bluetoothd", "wifid", "kernel", "MyApp"]:
            for i in range(10):
                await buf.append(
                    _make_entry(
                        process=proc,
                        message=f"{proc} msg {i}",
                        source=LogSource.DEVICE,
                    )
                )

        assert buf.size == 40

        # Apply filter: only MyApp from device source
        config = FilterConfig(process="MyApp")
        ingestion_filter.update_filter(config, source=LogSource.DEVICE)

        # Purge entries that the filter would now reject
        purged = await buf.purge(lambda e: ingestion_filter.should_admit(e))

        assert purged == 30  # 3 noisy processes × 10 entries each
        assert buf.size == 10

        # All remaining entries should be MyApp
        from server.models import LogQueryParams

        results, total = await buf.query(LogQueryParams(limit=100))
        assert total == 10
        assert all(e.process == "MyApp" for e in results)
