"""An empty log answer for a running app says why it may be empty (#619).

An iOS app that logs with print() writes to its stdout, which unified logging
never carries and quern never reads. Filtered to that app's process, every log
answer came back empty, and an agent read that as "the app logged nothing".
Now, when the process is running on a simulator whose logs quern is
streaming, the empty answer says what could explain it -- and when quern
cannot tell whether it is running, it says nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource
from server.processing import log_gaps
from server.processing.ingestion_filter import IngestionFilter, build_config

HEADERS = {"Authorization": "Bearer test-key-12345"}
SIM = "1A2B3C4D-0000-0000-0000-000000000001"
OTHER = "1A2B3C4D-0000-0000-0000-000000000002"
APP_PATH = (f"/Users/dev/Library/Developer/CoreSimulator/Devices/{SIM}/data/Containers/"
            "Bundle/Application/ABCD/MyApp.app/MyApp")


def _ps(*paths):
    async def ps():
        return list(paths)
    return ps


async def _ps_fails():
    return None


@pytest.fixture
def app(monkeypatch):
    app = create_app(config=ServerConfig(api_key="test-key-12345", ring_buffer_size=50),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    app.state.sim_log_adapters[SIM] = object()     # logs are being streamed from SIM
    return app


def _running(monkeypatch, *paths):
    monkeypatch.setattr(log_gaps, "_ps_paths", _ps(*paths))


async def _get(app, path, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path, headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _entry(process="Other", level=LogLevel.INFO):
    return LogEntry(id=uuid.uuid4().hex, timestamp=datetime.now(UTC), device_id=SIM,
                    process=process, level=level, message="m", source=LogSource.SIMULATOR)


class TestRunningOnSimulators:
    async def test_the_app_under_its_simulators_devices_folder_is_found(self):
        assert await log_gaps.running_on_simulators("MyApp", [SIM], ps=_ps(APP_PATH)) == [SIM]

    async def test_another_simulators_copy_does_not_count(self):
        assert await log_gaps.running_on_simulators("MyApp", [OTHER], ps=_ps(APP_PATH)) == []

    async def test_the_name_must_match_whole(self):
        helper = APP_PATH.replace("MyApp.app/MyApp", "MyApp.app/MyAppHelper")
        assert await log_gaps.running_on_simulators("MyApp", [SIM], ps=_ps(helper)) == []

    async def test_could_not_tell_is_none_not_an_empty_list(self):
        assert await log_gaps.running_on_simulators("MyApp", [SIM], ps=_ps_fails) is None


class TestQueryNote:
    async def test_an_empty_answer_for_a_running_app_names_print(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/query", process="MyApp")
        assert body["total"] == 0
        assert "print()" in body["note"] and SIM in body["note"]

    async def test_an_answer_with_entries_has_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        await app.state.ring_buffer.append(_entry("MyApp"))
        body = await _get(app, "/api/v1/logs/query", process="MyApp")
        assert body["total"] == 1 and body["note"] is None

    async def test_an_app_that_is_not_running_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, "/usr/libexec/something")
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_could_not_tell_gets_no_note(self, app, monkeypatch):
        monkeypatch.setattr(log_gaps, "_ps_paths", _ps_fails)
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_a_simulator_quern_is_not_streaming_gets_no_note(self, app, monkeypatch):
        app.state.sim_log_adapters.clear()
        _running(monkeypatch, APP_PATH)
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_a_level_filter_is_named_before_print(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        note = (await _get(app, "/api/v1/logs/query", process="MyApp", level="error"))["note"]
        assert note.startswith("No entries from MyApp at level error or above")
        assert "ask without level" in note

    async def test_a_log_filter_that_drops_the_app_is_named_instead(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        app.state.ingestion_filter = IngestionFilter()    # built at startup in the real app
        app.state.ingestion_filter.update_filter(build_config(process="SomeoneElse"),
                                                 source=LogSource.SIMULATOR)
        note = (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"]
        assert "set_log_filter drops its entries" in note and "print()" not in note

    async def test_an_answer_cut_short_by_eviction_gets_no_note(self, app, monkeypatch):
        """Truncated, empty is not evidence the app is silent: its lines may
        be among those evicted."""
        _running(monkeypatch, APP_PATH)
        for _ in range(60):                    # past the 50-entry buffer
            await app.state.ring_buffer.append(_entry())
        body = await _get(app, "/api/v1/logs/query", process="MyApp",
                          since="2000-01-01T00:00:00Z")
        assert body["truncated"] is True and body["note"] is None

    async def test_another_source_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/query", process="MyApp", source="device")
        assert body["note"] is None

    async def test_no_process_filter_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        assert (await _get(app, "/api/v1/logs/query"))["note"] is None


class TestSummaryNote:
    async def test_an_empty_window_for_a_running_app_names_print(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/summary", process="MyApp")
        assert body["total_count"] == 0 and "print()" in body["note"]

    async def test_a_delta_summary_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        cursor = (await _get(app, "/api/v1/logs/summary", process="MyApp"))["cursor"]
        body = await _get(app, "/api/v1/logs/summary", process="MyApp", since_cursor=cursor)
        assert body["note"] is None

