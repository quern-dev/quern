"""Retention for quern's iOS DerivedData, `~/.quern/builds/` (#581, part 2).

Every project, scheme and platform built through `build_and_install` keeps a
DerivedData of its own (#442), and nothing removed one: 4.8 GB on romulus for
one project, 32 GB on another machine for three -- every one untouched for
months. A DerivedData unused for `builds.max_age_days` (default 7) is removed,
and while the rest is over `builds.max_gb` (default 20), the least recently
used next. One a build holds is never removed: the sweep takes the build's own
lock, so a build cannot start in a directory being deleted either.

"Used" is when a build last took the directory, which `mark_used` stamps. A
directory from before the stamp existed -- the `builds/<scheme>` layout #442
replaced, among others -- is dated by its own and its `Build/` directory's
modification times instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from server import config as config_mod

logger = logging.getLogger(__name__)

#: The file a build touches in its DerivedData when it takes it.
STAMP = ".quern-last-used"

#: How often the sweep runs after the one at start.
SWEEP_SECONDS = 3600

#: What marks a directory as a DerivedData rather than one level above one.
_MARKERS = (STAMP, "Build", "info.plist", "Index.noindex", "CompilationCache.noindex",
            "ModuleCache.noindex", "SourcePackages", "Logs")


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


def _is_derived(path: Path) -> bool:
    return any((path / m).exists() for m in _MARKERS)


def find(base: Path | None = None) -> list[Derived]:
    """Every DerivedData under the store: `<project>-<hash>/<scheme>/<platform>`
    as #442 lays them out, or one level up for the layout before it. Symlinks
    are not followed, so nothing outside the store is found."""
    base = base or root()
    found: list[Derived] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = sorted(e for e in directory.iterdir() if e.is_dir() and not e.is_symlink())
        except OSError:
            return
        for entry in entries:
            if _is_derived(entry):
                found.append(Derived(entry, _last_used(entry)))
            elif depth < 3:
                walk(entry, depth + 1)

    walk(base, 1)
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
    """Bytes `path` takes on disk, symlinks not followed. Unreadable entries
    count as nothing: an estimate, not an audit."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(st, "st_blocks", 0) * 512 or st.st_size
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return total


def plan(found: list[Derived], *, now: float, max_age_days: int, max_bytes: int,
         busy: Callable[[Path], bool], size: Callable[[Path], int] = disk_size,
         ) -> list[tuple[Derived, str]]:
    """What to remove, and why: past `max_age_days` (0: no limit), then the
    least recently used while the rest is over `max_bytes` (0: no cap). A
    busy directory is neither removed nor sized; it counts toward the cap
    as nothing, since nothing can be done about it now."""
    out: list[tuple[Derived, str]] = []
    keep: list[Derived] = []
    for d in found:
        if busy(d.path):
            continue
        if max_age_days and now - d.last_used > max_age_days * 86400:
            out.append((d, f"unused for {int((now - d.last_used) // 86400)} days"))
        else:
            keep.append(d)
    if max_bytes:
        for d in keep:
            d.size = size(d.path)
        total = sum(d.size for d in keep)
        for d in sorted(keep, key=lambda d: d.last_used):
            if total <= max_bytes:
                break
            total -= d.size
            out.append((d, f"the store was over {max_bytes // 1024 ** 3} GB"))
    return out


async def sweep(locks: Mapping[Path, asyncio.Lock], *, base: Path | None = None,
                max_age_days: int, max_bytes: int, now: float | None = None,
                lock_factory: Callable[[], asyncio.Lock] = asyncio.Lock) -> list[dict]:
    """Remove what `plan` chooses, each under its build lock. Returns one
    entry per directory removed or failed. Never raises for one directory."""
    base = base or root()
    now = time.time() if now is None else now

    def busy(path: Path) -> bool:
        lock = locks.get(path)
        return lock is not None and lock.locked()

    chosen = await asyncio.to_thread(lambda: plan(
        find(base), now=now, max_age_days=max_age_days, max_bytes=max_bytes, busy=busy))
    done: list[dict] = []
    for d, why in chosen:
        lock = locks.get(d.path)
        if lock is None:
            lock = lock_factory()
            with contextlib.suppress(TypeError):
                locks[d.path] = lock               # a build now waits for the removal
        if lock.locked():
            continue                               # a build took it after the plan
        async with lock:
            if not _inside(d.path, base):
                continue
            size = d.size or await asyncio.to_thread(disk_size, d.path)
            await asyncio.to_thread(shutil.rmtree, d.path, True)
            if d.path.exists():
                done.append({"path": str(d.path), "ok": False, "reason": why,
                             "error": "could not be removed entirely"})
                continue
            _prune_empty_parents(d.path.parent, base)
            done.append({"path": str(d.path), "ok": True, "reason": why, "bytes": size})
    return done


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
    each pass, so an edited config.json takes effect without a restart."""
    while True:
        try:
            s = settings()
            done = await sweep(locks, max_age_days=s["max_age_days"], max_bytes=s["max_bytes"])
            if done:
                freed = sum(d.get("bytes", 0) for d in done if d["ok"])
                logger.info("Removed %d DerivedData from the build store (%.1f GB): %s",
                            sum(d["ok"] for d in done), freed / 1024 ** 3,
                            "; ".join(f"{Path(d['path']).relative_to(root())} ({d['reason']})"
                                      if d["ok"] else f"{d['path']}: {d['error']}"
                                      for d in done))
            if report is not None:
                report(done)
        except Exception:  # noqa: BLE001 -- the sweep must outlive one bad pass
            logger.exception("Build store sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)
