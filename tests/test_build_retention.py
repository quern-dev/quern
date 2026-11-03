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


def test_one_used_in_the_last_hour_survives_the_cap():
    """A second server on the same ~/.quern builds without this one seeing
    its lock; its build's stamp is what keeps the cap off it."""
    now = 100 * DAY
    found = _found(("newest", now - 60), ("building-elsewhere", now - 600),
                   ("idle", now - 2 * 3600))
    sizes = {Path("newest"): 10, Path("building-elsewhere"): 10, Path("idle"): 10}
    out = dr.plan(found, now=now, max_age_days=7, max_bytes=1, busy=lambda p: False,
                  size=_sizes(sizes))
    assert [d.path.name for d, _ in out] == ["idle"]


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


async def test_a_build_starting_during_the_removal_never_meets_it(tmp_path, monkeypatch):
    """The sweep holds the build's lock until the directory is moved aside;
    a build waiting on it then finds no directory, not one half deleted."""
    import weakref

    now = time.time()
    path = _derived(tmp_path / "P-0123456789" / "S" / "iphoneos", age_days=30, now=now)
    locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
    real_size = dr.disk_size

    def slow_size(p):                                   # runs under the lock
        time.sleep(0.3)
        return real_size(p)

    monkeypatch.setattr(dr, "disk_size", slow_size)
    sweeping = asyncio.create_task(dr.sweep(locks, base=tmp_path, max_age_days=7, max_bytes=0,
                                            now=now))
    lock = None
    for _ in range(200):
        lock = locks.get(path)
        if lock is not None and lock.locked():
            break
        await asyncio.sleep(0.01)
    assert lock is not None and lock.locked(), "the sweep holds the build's lock"
    async with lock:
        met = path.exists()
    await sweeping
    assert met is False, "the waiting build found the directory still there"
    assert not path.exists() and not list(tmp_path.glob(dr._REMOVING + "*"))


async def test_what_a_failed_delete_leaves_is_retried_and_never_used(tmp_path, monkeypatch):
    """A partial delete left a directory that read as used just now (its own
    mtime) and that the next build would build into (review). Moved aside
    first, the leftover is neither: it is deleted at the next pass."""
    now = time.time()
    path = _derived(tmp_path / "P-0123456789" / "S" / "iphoneos", age_days=30, now=now)
    monkeypatch.setattr(dr, "_remove", lambda p: ["blob: Operation not permitted"])
    done = await dr.sweep({}, base=tmp_path, max_age_days=7, max_bytes=0, now=now)
    assert done[0]["ok"] and "tried again" in done[0]["note"]
    assert not path.exists(), "nothing half deleted is left where a build would use it"
    [leftover] = list(tmp_path.glob(dr._REMOVING + "*"))
    assert dr.find(tmp_path) == []
    monkeypatch.undo()
    await dr.sweep({}, base=tmp_path, max_age_days=7, max_bytes=0, now=now)
    assert not leftover.exists()


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


def test_a_recent_stamp_beats_an_old_directory(tmp_path):
    """Xcode leaves an old DerivedData's own times alone on an incremental
    build; the stamp is what says it was just used."""
    now = time.time()
    path = _derived(tmp_path / "a", age_days=40, stamp=False, now=now)
    (path / dr.STAMP).touch()
    os.utime(path / dr.STAMP, (now - 2 * DAY, now - 2 * DAY))
    assert abs(dr.find(tmp_path)[0].last_used - (now - 2 * DAY)) < 5


async def test_a_build_that_took_it_after_the_plan_keeps_it(tmp_path):
    """Planned while free, taken by a build before its turn to be removed."""
    now = time.time()
    path = _derived(tmp_path / "P-0123456789" / "S" / "iphoneos", age_days=30, now=now)

    class TakenAfterThePlan(asyncio.Lock):
        checks = 0

        def locked(self):
            TakenAfterThePlan.checks += 1
            return TakenAfterThePlan.checks > 1          # free for the plan only

    assert await dr.sweep({path: TakenAfterThePlan()}, base=tmp_path, max_age_days=7,
                          max_bytes=0, now=now) == []
    assert path.exists()


async def test_emptying_the_store_keeps_the_store(tmp_path):
    store = tmp_path / "builds"
    _derived(store / "Only-0123456789" / "S" / "iphoneos", age_days=30)
    (store / ".DS_Store").write_text("finder")
    await dr.sweep({}, base=store, max_age_days=7, max_bytes=0)
    assert store.is_dir() and not (store / "Only-0123456789").exists()


@pytest.mark.parametrize("scheme", ["Build", "build", "Logs", "Index.noindex"])
def test_a_scheme_named_like_a_marker_is_still_a_scheme(tmp_path, scheme):
    """A scheme called Build made the project look like a DerivedData, and the
    whole project was removed, a running build's directory with it (review)."""
    project = tmp_path / "App-0123456789"
    platform = _derived(project / scheme / "iphoneos")
    sibling = _derived(project / "Other" / "iphonesimulator")
    assert {d.path for d in dr.find(tmp_path)} == {platform, sibling}


async def test_a_lock_on_a_path_inside_or_around_it_keeps_it(tmp_path):
    """The guard rested on exact path equality between the store's listing and
    the build's lock key; any mismatch turned it off silently (review)."""
    now = time.time()
    old = _derived(tmp_path / "SeattleSirens", age_days=30, now=now)
    lock = asyncio.Lock()
    await lock.acquire()
    assert await dr.sweep({old / "Build": lock}, base=tmp_path, max_age_days=7, max_bytes=0,
                          now=now) == []
    assert old.exists()


async def test_one_used_between_the_plan_and_its_turn_is_kept(tmp_path, monkeypatch):
    """A build that took the lock and let it go while the plan ran stamped
    the directory; it was removed all the same (review)."""
    now = time.time()
    path = _derived(tmp_path / "P-0123456789" / "S" / "iphoneos", age_days=30, now=now)
    real_plan = dr.plan

    def plan_then_a_build(*a, **k):
        out = real_plan(*a, **k)
        dr.mark_used(path)
        return out

    monkeypatch.setattr(dr, "plan", plan_then_a_build)
    assert await dr.sweep({}, base=tmp_path, max_age_days=7, max_bytes=0, now=now) == []
    assert path.exists()


def test_the_cap_never_takes_the_most_recently_used():
    """A project bigger than the cap on its own lost its cache every hour."""
    now = 100 * DAY
    found = _found(("SeattleSirens", now - 2 * DAY), ("older", now - 3 * DAY))
    sizes = {Path("SeattleSirens"): 27, Path("older"): 1}
    out = dr.plan(found, now=now, max_age_days=7, max_bytes=20, busy=lambda p: False,
                  size=_sizes(sizes))
    assert [d.path.name for d, _ in out] == ["older"]


async def test_an_unreadable_config_skips_the_pass(monkeypatch, caplog):
    """Falling back to the defaults would turn on a retention its owner had
    turned off, on any pass that met a half-written config.json (review)."""
    from server import config

    calls = []

    async def sweep(*a, **k):
        calls.append(k)
        raise asyncio.CancelledError

    def unreadable():
        if not calls and not getattr(unreadable, "seen", False):
            unreadable.seen = True
            raise config.ConfigUnreadable("config.json cannot be read")
        return {"max_age_days": 0, "max_bytes": 0, "note": None}

    monkeypatch.setattr(dr, "sweep", sweep)
    monkeypatch.setattr(dr, "SWEEP_SECONDS", 0)
    with caplog.at_level("WARNING", logger=dr.__name__), pytest.raises(asyncio.CancelledError):
        await dr.run_forever({}, unreadable)
    assert calls == [{"max_age_days": 0, "max_bytes": 0}], "skipped once, then the real setting"
    assert "sweep skipped" in caplog.text and "failed" not in caplog.text


def test_builds_settings_raises_on_an_unreadable_config(monkeypatch, tmp_path):
    from server import config

    broken = tmp_path / "config.json"
    broken.write_text('{"builds": {"max_age_days": 0,')
    monkeypatch.setattr(config, "USER_CONFIG_FILE", broken)
    with pytest.raises(config.ConfigUnreadable):
        config.builds_settings()


async def test_nothing_outside_the_store_is_removed_whatever_find_says(tmp_path, monkeypatch):
    """The last guard, apart from find's symlink filter."""
    outside = _derived(tmp_path / "outside" / "Precious", age_days=300)
    store = tmp_path / "builds"
    store.mkdir()
    monkeypatch.setattr(dr, "find", lambda base: [dr.Derived(outside, dr._last_used(outside))])
    assert await dr.sweep({}, base=store, max_age_days=7, max_bytes=0) == []
    assert outside.exists()
