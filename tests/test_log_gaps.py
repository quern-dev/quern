"""An empty log answer for a running app says why it may be empty (#619).

An iOS app that logs with print() writes to its stdout, which unified logging
never carries and quern never reads. Filtered to that app's process, every log
answer came back empty, and an agent read that as "the app logged nothing".
Now, when the process is running on a simulator whose logs quern is
streaming, the empty answer says what could explain it -- decided from the
app's whole history in the buffer, not from the query's own filters, and only
when quern can tell.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from server import visibility
from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource
from server.processing import log_gaps
from server.processing.ingestion_filter import PRESETS, IngestionFilter, build_config
from server.processing.summarizer import make_cursor

HEADERS = {"Authorization": "Bearer test-key-12345"}
SIM = "1A2B3C4D-0000-0000-0000-000000000001"
OTHER = "1A2B3C4D-0000-0000-0000-000000000002"
APP_PATH = (f"/Users/dev/Library/Developer/CoreSimulator/Devices/{SIM}/data/Containers/"
            "Bundle/Application/ABCD/MyApp.app/MyApp")


def _ps(*rows):
    async def ps():
        return list(rows)
    return ps


async def _ps_fails():
    return None


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(log_gaps, "_cache", None)
    app = create_app(config=ServerConfig(api_key="test-key-12345", ring_buffer_size=50),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    app.state.sim_log_adapters[SIM] = object()     # logs are being streamed from SIM
    app.state.ingestion_filter = IngestionFilter()  # built at startup in the real app
    return app


def _running(monkeypatch, *paths, age=10.0):
    monkeypatch.setattr(log_gaps, "_ps_rows", _ps(*[(age, p) for p in paths]))


async def _get(app, path, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path, headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _entry(process="Other", level=LogLevel.INFO, device_id=SIM, message="m"):
    return LogEntry(id=uuid.uuid4().hex, timestamp=datetime.now(UTC), device_id=device_id,
                    process=process, level=level, message=message, source=LogSource.SIMULATOR)


def _future():
    return (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRunningOnSimulators:
    async def test_the_app_under_its_simulators_devices_folder_is_found(self):
        found = await log_gaps.running_on_simulators("MyApp", [SIM], ps=_ps((30.0, APP_PATH)))
        assert list(found) == [SIM]
        assert datetime.now(UTC) - found[SIM] >= timedelta(seconds=29)

    async def test_another_simulators_copy_does_not_count(self):
        assert await log_gaps.running_on_simulators("MyApp", [OTHER],
                                                    ps=_ps((1.0, APP_PATH))) == {}

    async def test_the_name_must_match_whole(self):
        helper = APP_PATH.replace("MyApp.app/MyApp", "MyApp.app/MyAppHelper")
        assert await log_gaps.running_on_simulators("MyApp", [SIM], ps=_ps((1.0, helper))) == {}

    async def test_could_not_tell_is_none_not_empty(self):
        assert await log_gaps.running_on_simulators("MyApp", [SIM], ps=_ps_fails) is None

    @pytest.mark.parametrize("text, seconds", [
        ("05:07", 307), ("01:02:03", 3723), ("2-08:11:17", 2 * 86400 + 8 * 3600 + 11 * 60 + 17),
        ("x", None), ("1:2:3:4", None)])
    def test_ps_etime_is_read(self, text, seconds):
        assert log_gaps.parse_etime(text) == seconds


class TestQueryNote:
    async def test_a_silent_running_app_is_told_about_print(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/query", process="MyApp")
        assert body["total"] == 0
        assert "print()" in body["note"] and SIM in body["note"]

    async def test_an_answer_with_entries_has_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        await app.state.ring_buffer.append(_entry("MyApp"))
        body = await _get(app, "/api/v1/logs/query", process="MyApp")
        assert body["total"] == 1 and body["note"] is None

    async def test_a_search_that_misses_on_an_app_that_logs_has_no_note(self, app, monkeypatch):
        """The query's own filters made it empty, not print() (review)."""
        _running(monkeypatch, APP_PATH)
        await app.state.ring_buffer.append(_entry("MyApp"))
        body = await _get(app, "/api/v1/logs/query", process="MyApp", search="zzz-not-there")
        assert body["total"] == 0 and body["note"] is None

    async def test_a_poll_for_new_lines_on_an_app_that_logs_has_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        await app.state.ring_buffer.append(_entry("MyApp"))
        body = await _get(app, "/api/v1/logs/query", process="MyApp", since=_future())
        assert body["total"] == 0 and body["note"] is None

    async def test_another_device_id_is_not_answered_for_this_simulator(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/query", process="MyApp", device_id=OTHER)
        assert body["note"] is None

    async def test_an_app_that_is_not_running_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, "/usr/libexec/something")
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_could_not_tell_gets_no_note(self, app, monkeypatch):
        monkeypatch.setattr(log_gaps, "_ps_rows", _ps_fails)
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_a_simulator_quern_is_not_streaming_gets_no_note(self, app, monkeypatch):
        app.state.sim_log_adapters.clear()
        _running(monkeypatch, APP_PATH)
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_a_simulator_the_caller_may_not_see_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        monkeypatch.setattr(visibility.View, "allows", lambda self, udid, when=None: False)
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_lines_only_below_the_level_asked_are_named(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        await app.state.ring_buffer.append(_entry("MyApp", level=LogLevel.INFO))
        note = (await _get(app, "/api/v1/logs/query", process="MyApp", level="error"))["note"]
        assert note.startswith("MyApp is logging on simulator")
        assert "only below level error" in note and "print()" not in note

    async def test_a_level_above_a_silent_app_still_says_print(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        note = (await _get(app, "/api/v1/logs/query", process="MyApp", level="error"))["note"]
        assert "print()" in note

    @pytest.mark.parametrize("config", [
        build_config(process="SomeoneElse"),
        build_config(min_level=LogLevel.WARNING),
        build_config(exclude_subsystems=["com.me.myapp"]),
        build_config(subsystems=["com.me.other"]),
    ], ids=["process allow-list", "min_level", "exclude own subsystem", "subsystem allow-list"])
    async def test_a_filter_that_may_drop_the_app_is_named_instead(self, app, monkeypatch, config):
        """The probe at error level missed min_level and subsystem rules, and
        the note then blamed print() (review)."""
        _running(monkeypatch, APP_PATH)
        app.state.ingestion_filter.update_filter(config, source=LogSource.SIMULATOR)
        note = (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"]
        assert "set_log_filter" in note and "print()" not in note

    async def test_simulator_quiet_alone_is_not_blamed(self, app, monkeypatch):
        """Its rules are Apple's frameworks only; the app's lines pass it."""
        _running(monkeypatch, APP_PATH)
        app.state.ingestion_filter.update_filter(PRESETS["simulator-quiet"],
                                                 source=LogSource.SIMULATOR)
        assert "print()" in (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"]

    async def test_lines_evicted_since_the_app_started_give_no_note(self, app, monkeypatch):
        """Nothing from it may be its lines evicted, not silence."""
        _running(monkeypatch, APP_PATH, age=3600)
        for _ in range(60):                    # past the 50-entry buffer
            await app.state.ring_buffer.append(_entry())
        assert (await _get(app, "/api/v1/logs/query", process="MyApp"))["note"] is None

    async def test_another_source_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/query", process="MyApp", source="device")
        assert body["note"] is None

    async def test_no_process_filter_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        assert (await _get(app, "/api/v1/logs/query"))["note"] is None

    async def test_ps_is_asked_once_for_a_burst_of_polls(self, app, monkeypatch):
        calls = []

        async def ps():
            calls.append(1)
            return [(10.0, APP_PATH)]

        monkeypatch.setattr(log_gaps, "_ps_rows", ps)
        for _ in range(4):
            await _get(app, "/api/v1/logs/query", process="MyApp")
        assert calls == [1]


class TestSummaryNote:
    async def test_an_empty_window_for_a_silent_app_names_print(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        body = await _get(app, "/api/v1/logs/summary", process="MyApp")
        assert body["total_count"] == 0 and "print()" in body["note"]

    async def test_a_delta_summary_gets_no_note(self, app, monkeypatch):
        _running(monkeypatch, APP_PATH)
        cursor = (await _get(app, "/api/v1/logs/summary", process="MyApp"))["cursor"]
        body = await _get(app, "/api/v1/logs/summary", process="MyApp", since_cursor=cursor)
        assert body["note"] is None

    async def test_a_timestamp_cursor_delta_gets_no_note_either(self, app, monkeypatch):
        """The guard read only arrival cursors; an old timestamp one is a
        delta too (review)."""
        _running(monkeypatch, APP_PATH)
        old = make_cursor(datetime.now(UTC) - timedelta(seconds=5))
        body = await _get(app, "/api/v1/logs/summary", process="MyApp", since_cursor=old)
        assert body["cursor_reset"] is False and body["note"] is None
