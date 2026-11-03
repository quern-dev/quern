"""Retention for the build store's DerivedData (#581, part 2).

The case this exists for: `~/.quern/builds/` kept every DerivedData it ever
made -- 4.8 GB for one project on romulus, 32 GB for three on another Mac,
each untouched for months -- and nothing removed any of it."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from server.builds import derived_retention as dr

DAY = 86400


def _derived(path: Path, *, age_days: float | None = None, size: int = 0,
             stamp: bool = True, now: float | None = None) -> Path:
    """A DerivedData: a Build/ with `size` bytes, used `age_days` ago."""
    now = time.time() if now is None else now
    (path / "Build").mkdir(parents=True)
    (path / "Build" / "blob").write_bytes(b"x" * size)
    if age_days is not None:
        when = now - age_days * DAY
        if stamp:
            (path / dr.STAMP).touch()
            os.utime(path / dr.STAMP, (when, when))
        for p in (path / "Build", path):
            os.utime(p, (when, when))
    return path


def _sizes(paths):
    return lambda p: paths[p]


# ----------------------------------------------------------------------------
# Finding them
# ----------------------------------------------------------------------------


def test_both_layouts_are_found_and_nothing_else(tmp_path):
    new = _derived(tmp_path / "Metatext-0123456789" / "Metatext" / "iphonesimulator")
    old = _derived(tmp_path / "SeattleSirens")                 # builds/<scheme>, before #442
    (tmp_path / "stray-file").write_text("x")
    (tmp_path / "empty-project" / "scheme").mkdir(parents=True)
    assert {d.path for d in dr.find(tmp_path)} == {new, old}


def test_a_symlink_out_of_the_store_is_never_followed(tmp_path):
    outside = _derived(tmp_path / "outside" / "Precious")
    store = tmp_path / "builds"
    store.mkdir()
    (store / "link").symlink_to(outside.parent, target_is_directory=True)
    assert dr.find(store) == []


def test_the_stamp_dates_it_and_without_one_the_build_directory_does(tmp_path):
    now = time.time()
    stamped = _derived(tmp_path / "a", age_days=2, now=now)
    legacy = _derived(tmp_path / "b", age_days=40, stamp=False, now=now)
    found = {d.path: d.last_used for d in dr.find(tmp_path)}
    assert abs(found[stamped] - (now - 2 * DAY)) < 5
    assert abs(found[legacy] - (now - 40 * DAY)) < 5


def test_a_build_taking_its_directory_stamps_it(tmp_path, monkeypatch):
    from server.api import build_app as route

    path = tmp_path / "P-1234567890" / "S" / "iphonesimulator"

    async def take():
        async with route._holding([path]):
            pass

    asyncio.run(take())
    assert (path / dr.STAMP).exists()
    assert dr.find(tmp_path)[0].path == path


# ----------------------------------------------------------------------------
# Choosing
# ----------------------------------------------------------------------------


def _found(*specs):
    return [dr.Derived(Path(name), last_used=used) for name, used in specs]


def test_past_the_age_limit_goes_and_the_rest_stays():
    now = 100 * DAY
    found = _found(("old", now - 8 * DAY), ("recent", now - 2 * DAY))
    out = dr.plan(found, now=now, max_age_days=7, max_bytes=0, busy=lambda p: False)
    assert [(d.path.name, why) for d, why in out] == [("old", "unused for 8 days")]


def test_over_the_cap_the_least_recently_used_goes_first():
    now = 100 * DAY
    found = _found(("a", now - 3 * DAY), ("b", now - 1 * DAY), ("c", now - 2 * DAY))
    sizes = {Path("a"): 10, Path("b"): 10, Path("c"): 10}
    out = dr.plan(found, now=now, max_age_days=7, max_bytes=15, busy=lambda p: False,
                  size=_sizes(sizes))
    assert [d.path.name for d, _ in out] == ["a", "c"]


def test_a_directory_a_build_holds_is_never_chosen():
    now = 100 * DAY
    found = _found(("held", now - 30 * DAY), ("old", now - 30 * DAY))
    out = dr.plan(found, now=now, max_age_days=7, max_bytes=0,
                  busy=lambda p: p.name == "held")
    assert [d.path.name for d, _ in out] == ["old"]


def test_zero_turns_each_limit_off():
    now = 100 * DAY
    found = _found(("ancient", now - 365 * DAY))
    assert dr.plan(found, now=now, max_age_days=0, max_bytes=0, busy=lambda p: False) == []


# ----------------------------------------------------------------------------
# Removing
# ----------------------------------------------------------------------------


async def test_the_sweep_removes_the_old_keeps_the_recent_and_tidies_up(tmp_path):
    now = time.time()
    old = _derived(tmp_path / "Old-0123456789" / "Old" / "iphoneos", age_days=30, size=4096,
                   now=now)
    recent = _derived(tmp_path / "New-0123456789" / "New" / "iphonesimulator", age_days=1,
                      now=now)
    legacy = _derived(tmp_path / "SeattleSirens", age_days=160, stamp=False, now=now)
    done = await dr.sweep({}, base=tmp_path, max_age_days=7, max_bytes=0, now=now)
    assert {Path(d["path"]) for d in done if d["ok"]} == {old, legacy}
    assert all(d["bytes"] >= 4096 for d in done if Path(d["path"]) == old)
    assert recent.exists() and not old.exists() and not legacy.exists()
    assert not (tmp_path / "Old-0123456789").exists(), "the emptied project goes too"
    assert tmp_path.exists(), "never the store itself"


async def test_a_directory_whose_build_is_running_is_left(tmp_path):
    now = time.time()
    held = _derived(tmp_path / "P-0123456789" / "S" / "iphoneos", age_days=30, now=now)
    lock = asyncio.Lock()
    await lock.acquire()
    assert await dr.sweep({held: lock}, base=tmp_path, max_age_days=7, max_bytes=0,
                          now=now) == []
    assert held.exists()


async def test_a_build_starting_during_the_removal_waits_for_it(tmp_path, monkeypatch):
    """The sweep holds the build's own lock while it deletes, so a build
    cannot start writing into a directory being removed."""
    import weakref

    now = time.time()
    path = _derived(tmp_path / "P-0123456789" / "S" / "iphoneos", age_days=30, now=now)
    locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
    order = []
    real_rmtree = dr.shutil.rmtree

    def slow_rmtree(p, ignore_errors=False):
        time.sleep(0.2)
        order.append("removed")
        real_rmtree(p, ignore_errors)

    monkeypatch.setattr(dr.shutil, "rmtree", slow_rmtree)
    sweeping = asyncio.create_task(dr.sweep(locks, base=tmp_path, max_age_days=7, max_bytes=0,
                                            now=now))
    for _ in range(100):
        if path in locks and locks[path].locked():
            break
        await asyncio.sleep(0.01)
    async with locks[path]:
        order.append("built")
    await sweeping
    assert order == ["removed", "built"]


async def test_the_sweep_never_deletes_through_a_symlink(tmp_path):
    outside = _derived(tmp_path / "outside" / "Precious", age_days=300)
    store = tmp_path / "builds"
    store.mkdir()
    (store / "Linked").symlink_to(outside, target_is_directory=True)
    await dr.sweep({}, base=store, max_age_days=7, max_bytes=0)
    assert (outside / "Build" / "blob").exists()


async def test_a_pass_that_fails_does_not_stop_the_next(monkeypatch):
    calls = []

    async def failing(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        raise asyncio.CancelledError

    monkeypatch.setattr(dr, "sweep", failing)
    monkeypatch.setattr(dr, "SWEEP_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        await dr.run_forever({}, lambda: {"max_age_days": 7, "max_bytes": 0})
    assert len(calls) == 2


# ----------------------------------------------------------------------------
# Settings, and the server running it
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw, age, gb, notes", [
    ({}, 7, 20, 0),
    ({"max_age_days": 3, "max_gb": 0}, 3, 0, 0),
    ({"max_age_days": "a week", "max_gb": -1}, 7, 20, 2),
])
def test_the_settings_and_a_typo_falls_back_and_says_so(monkeypatch, raw, age, gb, notes):
    from server import config

    monkeypatch.setattr(config, "read_user_config", lambda **k: {"builds": raw})
    s = config.builds_settings()
    assert (s["max_age_days"], s["max_bytes"], bool(s["note"])) == (age, gb * 1024 ** 3,
                                                                    bool(notes))


async def test_the_server_sweeps_its_own_build_store_at_start(monkeypatch):
    """Pinned against the real lifespan: the task runs, on CONFIG_DIR's store
    (the test sandbox), and what it did is kept for status."""
    from server.config import CONFIG_DIR, ServerConfig
    from server.lifecycle import network_monitor, update_check
    from server.main import create_app, lifespan

    async def _never(*a, **k):
        return None

    monkeypatch.setattr(network_monitor, "update_network_state", lambda *a: None)
    monkeypatch.setattr(network_monitor, "network_monitor_loop", _never)
    monkeypatch.setattr(update_check, "periodic_update_check", _never)
    stale = _derived(CONFIG_DIR / "builds" / "Stale-0123456789" / "S" / "iphoneos",
                     age_days=30)
    app = create_app(config=ServerConfig(api_key="k"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    async with lifespan(app):
        for _ in range(200):
            if app.state.build_store_sweep is not None:
                break
            await asyncio.sleep(0.05)
        assert "build-store-sweep" in {t.get_name() for t in asyncio.all_tasks()}
        assert app.state.build_store_sweep["removed"][0]["path"] == str(stale)
    assert not stale.exists()
