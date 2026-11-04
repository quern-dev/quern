"""Retention for quern's iOS DerivedData, `~/.quern/builds/` (#581, part 2).

Every project, scheme and platform built through `build_and_install` keeps a
DerivedData of its own (#442), and nothing removed one: 4.8 GB on romulus for
one project, 32 GB on another machine for three -- every one untouched for
months. A DerivedData unused for `builds.max_age_days` (default 7) is removed,
and while the rest is over `builds.max_gb` (default 20), the least recently
used next -- never the most recently used, so a project bigger than the cap
keeps its cache.

Never removed: one a build holds (the sweep takes the build's own lock, so a
build cannot start in a directory being removed either), and one used in the
last hour, which covers a build by another server sharing the store, whose lock
this one cannot see. A directory is renamed aside under the lock and deleted
after, so a build never meets one half deleted; what a failed delete leaves is
retried at the next pass.

"Used" is when a build last took the directory, which `mark_used` stamps. A
directory from before the stamp existed -- the `builds/<scheme>` layout #442
replaced -- is dated by its own and its `Build/` directory's modification times.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from server import config as config_mod

logger = logging.getLogger(__name__)

#: The file a build touches in its DerivedData when it takes it.
STAMP = ".quern-last-used"

#: A DerivedData used more recently than this is never removed, whatever the
#: cap says. The build locks are one server's: a second server on the same
#: ~/.quern -- one started from a worktree without QUERN_STATE_DIR -- builds
#: in the store without this one seeing its lock, but its build stamps the
#: directory as it starts.
MIN_IDLE_S = 3600

#: How often a build holding its lock stamps its DerivedData again: well
#: inside MIN_IDLE_S, so a long build never reads as idle to another server.
STAMP_EVERY_S = 600

#: How often the sweep runs after the one at start.
SWEEP_SECONDS = 3600

#: A directory renamed aside to be deleted; one a failed delete left behind.
_REMOVING = ".removing-"

#: `<project>-<hash>`, as `build_app._derived_data` names a project's
#: directory: its children are schemes, whatever they are called, and only
#: their children -- the platforms -- are DerivedData. Matched on the name, not
#: on what is inside: a scheme called `Build` or `Logs` made the project
#: itself look like a DerivedData, and the whole of it was removed, a running
#: build's directory included (review).
_PROJECT = re.compile(r".+-[0-9a-f]{10}")

#: What marks a top-level directory of the layout before #442 as a DerivedData.
_MARKERS = (STAMP, "Build", "info.plist", "Index.noindex", "CompilationCache.noindex",
            "ModuleCache.noindex")


@dataclass
class Derived:
    path: Path
    last_used: float
    size: int = 0


def root() -> Path:
    return config_mod.CONFIG_DIR / "builds"


def mark_used(path: Path) -> None:
    """Stamp `path` as used now. Never raises: a stamp that cannot be written
    leaves the directory dated by its modification times."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / STAMP).touch()
    except OSError:
        logger.debug("Could not stamp %s", path, exc_info=True)


def _dirs(directory: Path) -> list[Path]:
    try:
        return sorted(e for e in directory.iterdir() if e.is_dir() and not e.is_symlink()
                      and not e.name.startswith(_REMOVING))
    except OSError:
        return []


def find(base: Path | None = None) -> list[Derived]:
    """Every DerivedData in the store: `<project>-<hash>/<scheme>/<platform>`
    as #442 lays them out, and `builds/<scheme>` from before it. Symlinks are
    not followed, so nothing outside the store is found."""
    base = base or root()
    found: list[Derived] = []
    for top in _dirs(base):
        if _PROJECT.fullmatch(top.name):
            for scheme in _dirs(top):
                found.extend(Derived(p, _last_used(p)) for p in _dirs(scheme))
        elif any((top / m).exists() for m in _MARKERS):
            found.append(Derived(top, _last_used(top)))
    return found


def _last_used(path: Path) -> float:
    with contextlib.suppress(OSError):
        return (path / STAMP).stat().st_mtime
    times = []
    for p in (path, path / "Build", path / "info.plist"):
        with contextlib.suppress(OSError):
            times.append(p.stat().st_mtime)
    return max(times, default=0.0)


def disk_size(path: Path) -> int:
    """Bytes `path` takes on disk, symlinks not followed -- the walk the disk
    report uses (`server.disk_usage`), with a directory that cannot be read
    counted as nothing: an estimate, not an audit."""
    from server import disk_usage

    try:
        return disk_usage.disk_size(path)
    except OSError:
        return 0


def plan(found: list[Derived], *, now: float, max_age_days: int, max_bytes: int,
         busy: Callable[[Path], bool], size: Callable[[Path], int] = disk_size,
         min_idle_s: float = MIN_IDLE_S) -> list[tuple[Derived, str]]:
    """What to remove, and why: past `max_age_days` (0: no limit), then the
    least recently used while the rest is over `max_bytes` (0: no cap), never
    the most recently used of all. A busy directory, or one used within
    `min_idle_s`, is never removed, but its size counts toward the cap:
    counted as nothing, the older caches stayed in a store over it."""
    out: list[tuple[Derived, str]] = []
    keep: list[Derived] = []
    protected: list[Derived] = []
    newest = max((d.last_used for d in found), default=None)
    for d in found:
        if busy(d.path) or now - d.last_used < min_idle_s:
            protected.append(d)
            continue
        if max_age_days and now - d.last_used > max_age_days * 86400:
            out.append((d, f"unused for {int((now - d.last_used) // 86400)} days"))
        else:
            keep.append(d)
    if max_bytes:
        for d in (*keep, *protected):
            d.size = size(d.path)
        total = sum(d.size for d in (*keep, *protected))
        for d in sorted(keep, key=lambda d: d.last_used):
            if total <= max_bytes or d.last_used == newest:
                break
            total -= d.size
            out.append((d, f"the store was over {max_bytes // 1024 ** 3} GB"))
    return out


async def sweep(locks: Mapping[Path, asyncio.Lock], *, base: Path | None = None,
                max_age_days: int, max_bytes: int, now: float | None = None,
                lock_factory: Callable[[], asyncio.Lock] = asyncio.Lock) -> list[dict]:
    """Remove what `plan` chooses. Returns one entry per directory removed or
    failed. Never raises for one directory."""
    base = base or root()
    now = time.time() if now is None else now
    done: list[dict] = []
    for leftover in await asyncio.to_thread(_leftovers, base):
        # Said when it fails again, as a first failure is: silent, it kept its
        # space with nothing to show for it (CodeRabbit).
        if errors := await asyncio.to_thread(_remove, leftover):
            done.append({"path": str(leftover), "ok": False, "reason": "left by a failed delete",
                         "error": f"still could not be deleted ({errors[0]})"})

    def busy(path: Path) -> bool:
        # Any lock on it, inside it or around it: a key that is not exactly
        # this path is still this directory's build.
        return any(lock.locked() and (held == path or held.is_relative_to(path)
                                      or path.is_relative_to(held))
                   for held, lock in list(locks.items()))

    chosen = await asyncio.to_thread(lambda: plan(
        find(base), now=now, max_age_days=max_age_days, max_bytes=max_bytes, busy=busy))
    for d, why in chosen:
        lock = locks.get(d.path)
        if lock is None:
            lock = lock_factory()
            with contextlib.suppress(TypeError):
                locks[d.path] = lock               # a build now waits for the removal
        if busy(d.path):
            continue                               # a build took it after the plan
        async with lock:
            # Used since the plan -- a build that took the lock and let it go
            # in the meantime stamped it -- or no longer the store's.
            if _last_used(d.path) > d.last_used or not _inside(d.path, base):
                continue
            aside = base / f"{_REMOVING}{uuid.uuid4().hex[:8]}"
            try:
                d.size = d.size or await asyncio.to_thread(disk_size, d.path)
                d.path.rename(aside)
            except OSError as e:
                done.append({"path": str(d.path), "ok": False, "reason": why,
                             "error": f"could not be moved aside to remove: {e}"})
                continue
        _prune_empty_parents(d.path.parent, base)
        errors = await asyncio.to_thread(_remove, aside)
        entry = {"path": str(d.path), "ok": True, "reason": why, "bytes": d.size}
        if errors:
            entry["note"] = (f"{len(errors)} file(s) could not be deleted yet ({errors[0]}); "
                             "tried again at the next pass")
        done.append(entry)
    return done


def _remove(path: Path) -> list[str]:
    """Delete `path`, carrying on past what cannot be deleted; returns those."""
    errors: list[str] = []
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda f, p, e: errors.append(f"{p}: {e}"))
    else:  # 3.11: onexc does not exist yet
        shutil.rmtree(path, onerror=lambda f, p, info: errors.append(f"{p}: {info[1]}"))
    return errors


def _leftovers(base: Path) -> list[Path]:
    try:
        return [e for e in base.iterdir()
                if e.name.startswith(_REMOVING) and e.is_dir() and not e.is_symlink()]
    except OSError:
        return []


def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve()) and path.resolve() != base.resolve()
    except OSError:
        return False


def _prune_empty_parents(directory: Path, base: Path) -> None:
    while _inside(directory, base):
        try:
            leftovers = [e for e in directory.iterdir() if e.name != ".DS_Store"]
            if leftovers:
                return
            with contextlib.suppress(OSError):
                (directory / ".DS_Store").unlink()
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


async def run_forever(locks: Mapping[Path, asyncio.Lock], settings: Callable[[], dict],
                      report: Callable[[list[dict]], None] | None = None) -> None:
    """The sweep at start and every SWEEP_SECONDS after. Settings are read
    each pass, so an edited config.json takes effect without a restart. A
    config.json that cannot be read skips the pass: falling back to the
    defaults would turn on a retention its owner turned off."""
    while True:
        try:
            s = settings()
            if s.get("note"):
                logger.warning("%s", s["note"])
            done = await sweep(locks, max_age_days=s["max_age_days"], max_bytes=s["max_bytes"])
            if done:
                freed = sum(d.get("bytes", 0) for d in done if d["ok"])
                logger.info("Removed %d DerivedData from the build store (%.1f GB): %s",
                            sum(d["ok"] for d in done), freed / 1024 ** 3,
                            "; ".join(f"{_relative(d['path'])} ({d['reason']})"
                                      + (f" -- {d['note']}" if d.get("note") else "")
                                      if d["ok"] else f"{d['path']}: {d['error']}"
                                      for d in done))
            if report is not None:
                report(done)
        except config_mod.ConfigUnreadable as e:
            logger.warning("Build store sweep skipped: %s", e)
        except Exception:  # noqa: BLE001 -- the sweep must outlive one bad pass
            logger.exception("Build store sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


def _relative(path: str) -> str:
    with contextlib.suppress(ValueError):
        return str(Path(path).relative_to(root()))
    return path
