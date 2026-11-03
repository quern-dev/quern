"""Disk use, visible (#581, part 3).

The case this exists for: a shared machine filled with simulator data and
build stores -- 17 GB across 44 simulators on romulus, 32 GB of build store on
another Mac -- and nothing in quern said so."""

from __future__ import annotations

import asyncio
import plistlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import disk_usage as du

GB = 1024 ** 3
SETTINGS = {"warn_bytes": 100 * GB, "warn_free_bytes": 20 * GB, "note": None}


def _sim(root: Path, udid: str, name: str, size: int) -> Path:
    device = root / udid
    (device / "data").mkdir(parents=True)
    (device / "data" / "blob").write_bytes(b"x" * size)
    (device / "device.plist").write_bytes(plistlib.dumps({"name": name, "UDID": udid}))
    return device


def _measure(tmp_path, **kw):
    sims = tmp_path / "Devices"
    sims.mkdir(exist_ok=True)
    config = tmp_path / "quern"
    config.mkdir(exist_ok=True)
    return du.measure(sim_devices=sims, config_dir=config,
                      settings=kw.pop("settings", SETTINGS), **kw)


# ----------------------------------------------------------------------------
# Measuring
# ----------------------------------------------------------------------------


def test_each_store_is_measured_and_the_largest_simulators_are_named(tmp_path):
    sims = tmp_path / "Devices"
    _sim(sims, "AAAA", "iPhone 16", 300_000)
    _sim(sims, "BBBB", "iPad Air", 100_000)
    builds = tmp_path / "quern" / "builds" / "P-0123456789"
    builds.mkdir(parents=True)
    (builds / "x").write_bytes(b"y" * 50_000)
    report = _measure(tmp_path)
    s = report["stores"]
    assert s["simulators"]["count"] == 2
    assert [d["name"] for d in s["simulators"]["largest"]] == ["iPhone 16", "iPad Air"]
    assert s["simulators"]["bytes"] >= 400_000 and s["builds"]["bytes"] >= 50_000
    assert s["recordings"]["bytes"] == 0, "a store not made yet takes nothing"
    assert report["used_bytes"] == sum(v["bytes"] for v in s.values())
    assert report["used_complete"] is True and report["disk"]["free_bytes"] > 0


def test_a_store_that_cannot_be_read_is_unknown_not_empty(tmp_path, monkeypatch):
    """Could not tell is not nothing there: a false all-clear otherwise."""
    (tmp_path / "quern" / "recordings").mkdir(parents=True)
    real = du.disk_size

    def unreadable(path):
        if path.name == "recordings":
            raise PermissionError("Operation not permitted")
        return real(path)

    monkeypatch.setattr(du, "disk_size", unreadable)
    report = _measure(tmp_path)
    rec = report["stores"]["recordings"]
    assert rec["bytes"] is None and "not permitted" in rec["error"]
    assert report["used_complete"] is False
    assert any("could not measure recordings" in w for w in report["warnings"])


def test_a_device_without_a_readable_plist_is_still_counted(tmp_path):
    sims = tmp_path / "Devices"
    device = _sim(sims, "CCCC", "x", 10_000)
    (device / "device.plist").write_text("not a plist")
    report = _measure(tmp_path)
    assert report["stores"]["simulators"]["largest"] == [
        {"udid": "CCCC", "name": None, "bytes": report["stores"]["simulators"]["largest"][0]
         ["bytes"]}]


@pytest.mark.parametrize("settings, used_warns, free_warns", [
    ({"warn_bytes": 1, "warn_free_bytes": 0, "note": None}, True, False),
    ({"warn_bytes": 0, "warn_free_bytes": 10 ** 18, "note": None}, False, True),
    ({"warn_bytes": 0, "warn_free_bytes": 0, "note": None}, False, False),
])
def test_warnings_over_the_use_and_under_the_free_space(tmp_path, settings, used_warns,
                                                        free_warns):
    _sim(tmp_path / "Devices", "AAAA", "x", 10_000)
    warnings = _measure(tmp_path, settings=settings)["warnings"]
    assert any("disk.warn_gb" in w for w in warnings) is used_warns
    assert any("disk.warn_free_gb" in w for w in warnings) is free_warns


def test_the_tests_never_walk_the_real_simulators():
    """conftest redirects it: the real one is tens of GB of someone's machine."""
    assert "quern-test-sims-" in str(du.SIM_DEVICES)


@pytest.mark.parametrize("raw, warn, free, noted", [
    ({}, 100, 20, False),
    ({"warn_gb": 0, "warn_free_gb": 5}, 0, 5, False),
    ({"warn_gb": "lots"}, 100, 20, True),
])
def test_the_settings_and_a_typo_falls_back_and_says_so(monkeypatch, raw, warn, free, noted):
    from server import config

    monkeypatch.setattr(config, "read_user_config", lambda **k: {"disk": raw})
    s = config.disk_settings()
    assert (s["warn_bytes"], s["warn_free_bytes"], bool(s["note"])) == (warn * GB, free * GB,
                                                                        noted)


def test_an_unreadable_config_raises_rather_than_warning_on_defaults(monkeypatch, tmp_path):
    from server import config

    broken = tmp_path / "config.json"
    broken.write_text('{"disk": {')
    monkeypatch.setattr(config, "USER_CONFIG_FILE", broken)
    with pytest.raises(config.ConfigUnreadable):
        config.disk_settings()


async def test_a_pass_that_fails_does_not_stop_the_next(monkeypatch):
    calls = []

    def measure():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        raise asyncio.CancelledError

    monkeypatch.setattr(du, "measure", measure)
    monkeypatch.setattr(du, "MEASURE_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        await du.run_forever(SimpleNamespace(state=SimpleNamespace()))
    assert len(calls) == 2


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def _report(**over):
    stores = {
        "simulators": {"path": "/Users/o/Devices", "bytes": 27 * GB, "count": 44,
                       "largest": [{"udid": "AAAA", "name": "iPhone 16", "bytes": 4 * GB}]},
        "builds": {"path": "/Users/o/.quern/builds", "bytes": 32 * GB},
        "recordings": {"path": "/Users/o/.quern/recordings", "bytes": None, "error": "denied"},
        "profiles": {"path": "/p", "bytes": 0}, "uploads": {"path": "/u", "bytes": 0},
    }
    return {"measured_at": time.time() - 180, "stores": stores, "used_bytes": 59 * GB,
            "used_complete": False, "disk": {"total_bytes": 1000 * GB, "free_bytes": 12 * GB},
            "warnings": ["12.0 GB free on the disk, under disk.warn_free_gb (20.0 GB)"],
            "note": None, **over}


def test_the_terminal_lines_say_use_free_largest_warnings_and_age():
    lines = du.report_lines({**_report(), "build_store_sweep": {"removed": [
        {"ok": True, "bytes": 25 * GB}, {"ok": False}]}})
    text = "\n".join(lines)
    assert "59.0 GB used" in lines[0] and "recordings unknown" in lines[0]
    assert "12.0 GB free" in lines[0]
    assert "iPhone 16 4.0 GB" in text and "44 in all" in text
    assert "! 12.0 GB free on the disk" in text
    assert "measured 3 min ago" in text
    assert "removed 1 DerivedData (25.0 GB)" in text


def test_measuring_is_said_as_measuring():
    assert "measuring" in du.report_lines({"measuring": True})[0]


async def _route(report, *, principal="owner", mode="open", sweep=None):
    from server import visibility
    from server.api import system

    app = SimpleNamespace(state=SimpleNamespace(disk_usage=report, build_store_sweep=sweep))
    caller = visibility.Caller(session="s", principal=principal)
    old = visibility.MODE
    visibility.MODE = mode
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(visibility, "current_caller", lambda: caller)
            return await system.disk_use(SimpleNamespace(app=app))
    finally:
        visibility.MODE = old


async def test_the_owner_gets_everything():
    out = await _route(_report(), sweep={"removed": []})
    assert out["stores"]["builds"]["path"] and out["stores"]["simulators"]["largest"]
    assert out["build_store_sweep"] == {"removed": []} and out["measuring"] is False


async def test_a_client_gets_sizes_without_the_owners_paths():
    out = await _route(_report(), principal="client:a", sweep={"removed": []})
    assert all("path" not in s for s in out["stores"].values())
    assert out["stores"]["builds"]["bytes"] == 32 * GB
    assert out["stores"]["simulators"]["largest"], "named on an open server"
    assert out["build_store_sweep"] is None


async def test_a_blind_client_does_not_see_the_largest_simulators():
    """They name other keys' devices."""
    out = await _route(_report(), principal="client:a", mode="blind")
    assert "largest" not in out["stores"]["simulators"]
    assert out["stores"]["simulators"]["count"] == 44


async def test_before_the_first_measurement_it_says_measuring():
    assert (await _route(None))["measuring"] is True


def test_cli_fetch_says_why_it_could_not_ask(monkeypatch, tmp_path):
    from server import config

    monkeypatch.setattr(config, "API_KEY_FILE", tmp_path / "missing")
    report, reason = du.fetch(1)
    assert report is None and "no API key" in reason
    (tmp_path / "key").write_text("k")
    monkeypatch.setattr(config, "API_KEY_FILE", tmp_path / "key")
    report, reason = du.fetch(1, timeout=0.5)
    assert report is None and "could not ask" in reason


# ----------------------------------------------------------------------------
# Each paired machine
# ----------------------------------------------------------------------------


def _remote():
    from server.remote import client

    return client.Remote(name="romulus", url="https://r:9130", fingerprint="", key="k",
                         label="scorpius")


@pytest.mark.parametrize("answer, disk, error", [
    ({"measuring": True}, {"measuring": True}, None),
    ({"measuring": False, "used_bytes": 59 * GB, "used_complete": True,
      "disk": {"free_bytes": 12 * GB, "total_bytes": 1000 * GB}, "warnings": ["w"],
      "measured_at": 1.0},
     {"used_bytes": 59 * GB, "used_complete": True, "free_bytes": 12 * GB,
      "total_bytes": 1000 * GB, "warnings": ["w"], "measured_at": 1.0}, None),
])
def test_a_remotes_disk_use_is_in_its_row(monkeypatch, answer, disk, error):
    from server.remote import status

    def get(remote, path, timeout):
        return {"/health": {"status": "ok", "version": "0.25.0"},
                "/api/v1/device/list": {"devices": []},
                "/api/v1/system/disk": answer}[path]

    monkeypatch.setattr(status, "_get", get)
    row = status.probe(_remote(), "0.25.0")
    assert (row["disk"], row["disk_error"]) == (disk, error)


def test_a_remote_from_before_it_is_not_reported_not_empty(monkeypatch):
    """A server without the route answers 404: 'not reported', never '0 used'."""
    import urllib.error

    from server.remote import status

    def get(remote, path, timeout):
        if path == "/api/v1/system/disk":
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        return {"/health": {"status": "ok", "version": "0.25.0"},
                "/api/v1/device/list": {"devices": []}}[path]

    monkeypatch.setattr(status, "_get", get)
    row = status.probe(_remote(), "0.25.0")
    assert row["disk"] is None and "could not be read" in row["disk_error"]
    from server.remote_cli import _status_line
    assert "disk use not reported" in _status_line(row)


def test_the_remote_list_line_shows_use_and_free_and_flags_a_warning():
    from server.remote_cli import _status_line

    row = {"name": "romulus", "url": "https://r", "label": "s", "reachable": True,
           "version": "0.25.0", "devices": [], "free": 0,
           "disk": {"used_bytes": 59 * GB, "free_bytes": 12 * GB, "warnings": ["w"]}}
    assert _status_line(row).endswith(", disk 59.0 GB used / 12.0 GB free !")


async def test_the_server_measures_at_start(monkeypatch):
    """Pinned against the real lifespan, on the test sandbox's simulators."""
    from server.config import ServerConfig
    from server.lifecycle import network_monitor, update_check
    from server.main import create_app, lifespan

    async def _never(*a, **k):
        return None

    monkeypatch.setattr(network_monitor, "update_network_state", lambda *a: None)
    monkeypatch.setattr(network_monitor, "network_monitor_loop", _never)
    monkeypatch.setattr(update_check, "periodic_update_check", _never)
    app = create_app(config=ServerConfig(api_key="k"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    async with lifespan(app):
        for _ in range(200):
            if app.state.disk_usage is not None:
                break
            await asyncio.sleep(0.05)
        assert "disk-measure" in {t.get_name() for t in asyncio.all_tasks()}
        assert app.state.disk_usage["stores"]["simulators"]["path"] == str(du.SIM_DEVICES)


def test_a_store_it_cannot_open_raises_and_one_inside_it_does_not(tmp_path):
    """The store itself unreadable is 'could not tell'; a file inside it that
    cannot be read is an estimate's rounding."""
    with pytest.raises(OSError):
        du.disk_size(tmp_path / "gone")
    store = tmp_path / "store"
    (store / "locked").mkdir(parents=True)
    (store / "file").write_bytes(b"x" * 8192)
    (store / "locked").chmod(0)
    try:
        assert du.disk_size(store) >= 8192
    finally:
        (store / "locked").chmod(0o755)


def test_doctor_says_disk_use_was_not_checked_without_a_server(monkeypatch, capsys):
    from server import main

    monkeypatch.setattr(main, "read_state", lambda: None)
    main._report_disk()
    out = capsys.readouterr().out
    assert out.startswith("Disk:") and "not checked" in out and "no server running" in out


def test_doctor_prints_the_servers_report(monkeypatch, capsys):
    from server import main

    monkeypatch.setattr(main, "read_state", lambda: {"server_port": 9999})
    monkeypatch.setattr(main, "is_server_healthy", lambda port: True)
    monkeypatch.setattr(du, "fetch", lambda port: (_report(), ""))
    main._report_disk()
    out = capsys.readouterr().out
    assert "59.0 GB used" in out and "! 12.0 GB free on the disk" in out
