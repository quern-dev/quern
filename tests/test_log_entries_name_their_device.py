"""A log line has to say which device it came from.

The trace joins app logs to actions by device. Every one of its 84 tests
hand-set `device_id="SIM-A"` — a shape the product never produced. In
production `LogEntry.device_id` was the literal string `"default"`, because
the adapters never passed one and the model's default looked like a device
name. `owns("SIM-A", "default")` is FOREIGN, so every scoped action rejected
every app log line, and the half of the trace that joins logs to actions did
not work outside the suite.

Found by an independent review. These assert the shape the product actually
produces, which is what the trace tests could not.
"""

from __future__ import annotations

from server.models import LogEntry


class TestTheSentinelIsGone:
    def test_an_unset_device_id_is_empty_not_a_name(self):
        """Empty fails a truthiness check, which every consumer already reads
        as "unknown, do not filter". `"default"` passed truthiness and failed
        equality, so it was treated as a real device that matched nothing."""
        entry = LogEntry(
            id="x", timestamp=__import__("datetime").datetime.now(
                __import__("datetime").UTC,
            ),
            level="info", message="m", source="simulator",
        )

        assert entry.device_id == ""
        assert not entry.device_id

    def test_the_configured_default_is_empty_too(self):
        """oslog and crash pass this explicitly, so emptying the model default
        alone would have left them naming a device that does not exist."""
        from server.config import ServerConfig

        assert ServerConfig().default_device_id == ""


class TestAdaptersNameTheirDevice:
    """The adapters knew their udid all along and never passed it on."""

    async def test_simulator_logging_tags_entries_with_the_udid(self, monkeypatch):
        from server.sources.simulator_log import SimulatorLogAdapter

        adapter = SimulatorLogAdapter(udid="SIM-A", device_id="SIM-A")

        assert adapter.device_id == "SIM-A"

    def test_the_simulator_handler_passes_it(self):
        """Asserted on the source, because the alternative is booting a
        simulator in a unit test. The bug was precisely that this argument
        was absent."""
        import inspect

        from server.api import device as device_api

        source = inspect.getsource(device_api.start_simulator_logging)
        assert "device_id=udid" in source, (
            "entries would carry no device, and the trace could not attribute "
            "a single app log line"
        )

    def test_the_physical_device_handler_passes_it(self):
        import inspect

        from server.api import device as device_api

        source = inspect.getsource(device_api.start_device_logging)
        assert "device_id=udid" in source

    def test_android_is_not_forgotten(self):
        """Three device kinds reach this codebase — iOS simulators, physical
        devices and Android emulators — and the third is the one that gets
        missed, because the first two are what anyone tests against. The
        logcat adapter had the same omission as the other two."""
        import inspect

        from server.api import device as device_api

        source = inspect.getsource(device_api.start_device_logging)
        logcat = source[source.index("LogcatAdapter("):]
        assert "device_id=udid" in logcat[:300], (
            "Android log lines would name no device"
        )
