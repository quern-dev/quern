"""What quern's stores and the simulators take on disk (#581, part 3).

A shared machine filled without anyone seeing it: 17 GB of simulator data
across 44 simulators on romulus, 4.8 GB of build store, and nothing in quern
said so. The owner should see 120 GB before it becomes 300.

Measured in the background -- walking a simulator directory of tens of GB
takes seconds to minutes -- and served from the last measurement, with its
time. A store that could not be measured is reported as unknown, with why,
never as empty: "could not tell" reading as "nothing there" is a false
all-clear.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import plistlib
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server import config as config_mod

logger = logging.getLogger(__name__)

#: Every simulator's data, whoever made it: quern does not create simulators,
#: but what its clients install into them is what grows.
#: QUERN_SIM_DEVICES_DIR redirects it, for tests: walking the real one takes
#: seconds to minutes and reads someone's machine.
SIM_DEVICES = Path(os.environ.get("QUERN_SIM_DEVICES_DIR")
                   or Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices")

#: How often the measurement is taken after the one at start.
MEASURE_SECONDS = 15 * 60

#: The largest simulators named in the report.
LARGEST = 5


class Stopped(Exception):
    """The server is stopping: the walk ends rather than hold up its exit."""


def disk_size(path: Path, stop: threading.Event | None = None,
              unreadable: list[str] | None = None) -> int:
    """Bytes `path` takes on disk, symlinks not followed. Raises OSError when
    `path` itself cannot be read. An entry inside it that cannot be read is
    left out and, when `unreadable` is given, named there -- so a caller can
    say the total is partial rather than present it as complete (CodeRabbit).
    One gone before it could be read is simply not there. Raises Stopped once `stop` is
    set: a walk of tens of GB runs in a thread nothing can cancel, and
    `quern stop` waited for it, then killed the server (review)."""
    total = 0
    stack = [path]
    first = True
    while stack:
        if stop is not None and stop.is_set():
            raise Stopped()
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as e:
            if first:
                raise
            if unreadable is not None and not isinstance(e, FileNotFoundError):
                unreadable.append(f"{current}: {e.strerror or e}")
            continue
        first = False
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    if unreadable is not None and not isinstance(e, FileNotFoundError):
                        unreadable.append(f"{entry.path}: {e.strerror or e}")
                    continue
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
    return total


def _store(path: Path, stop: threading.Event | None = None) -> dict:
    """One store's size: 0 when it does not exist yet, unknown with the reason
    when it exists and cannot be read -- or when whether it exists cannot be
    told, which a parent that cannot be searched raises (review)."""
    unreadable: list[str] = []
    try:
        if not path.exists():
            return {"path": str(path), "bytes": 0}
        out = {"path": str(path), "bytes": disk_size(path, stop, unreadable)}
    except OSError as e:
        return {"path": str(path), "bytes": None, "error": str(e)}
    if unreadable:
        out["partial"] = True
        out["note"] = (f"{len(unreadable)} entries could not be read and are left out "
                       f"(first: {unreadable[0]})")
    return out


def _simulators(root: Path, stop: threading.Event | None = None) -> dict:
    """The simulators' data, in one walk: each device sized once, and the
    total their sum with whatever else the directory holds. Walking the root
    and then each device read every file twice (review)."""
    try:
        if not root.exists():
            return {"path": str(root), "bytes": 0, "count": 0, "largest": []}
        entries = list(os.scandir(root))
    except OSError as e:
        return {"path": str(root), "bytes": None, "error": str(e)}
    total = 0
    sizes = []
    partial = False
    unreadable: list[str] = []
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
            if not entry.is_dir(follow_symlinks=False):
                continue
            size = disk_size(Path(entry.path), stop, unreadable)
        except OSError:
            partial = True                       # gone mid-walk, or unreadable
            continue
        partial = partial or bool(unreadable)
        total += size
        name = None
        with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
            info = plistlib.loads((Path(entry.path) / "device.plist").read_bytes())
            name = info.get("name") if isinstance(info, dict) else None
        sizes.append({"udid": entry.name, "name": name, "bytes": size})
    sizes.sort(key=lambda d: -d["bytes"])
    out = {"path": str(root), "bytes": total, "count": len(sizes), "largest": sizes[:LARGEST]}
    if partial:
        out["partial"] = True
        out["note"] = "some simulators could not be read: the total leaves them out"
    return out


def measure(*, sim_devices: Path | None = None, config_dir: Path | None = None,
            settings: dict | None = None, now: Callable[[], float] = time.time,
            stop: threading.Event | None = None) -> dict:
    """Measure every store now. Blocking: run it in a thread."""
    config_dir = config_dir or config_mod.CONFIG_DIR
    settings = settings or config_mod.disk_settings()
    stores = {
        "simulators": _simulators(sim_devices or SIM_DEVICES, stop),
        "builds": _store(config_dir / "builds", stop),
        "recordings": _store(config_dir / "recordings", stop),
        "profiles": _store(config_dir / "profiles", stop),
        "uploads": _store(config_dir / "uploads", stop),
    }
    try:
        usage = shutil.disk_usage(config_dir if config_dir.exists() else config_dir.parent)
        free: int | None = usage.free
        disk = {"total_bytes": usage.total, "free_bytes": usage.free}
    except OSError as e:
        free = None
        disk = {"total_bytes": None, "free_bytes": None, "error": str(e)}
    known = [s["bytes"] for s in stores.values() if s["bytes"] is not None]
    used = sum(known)
    unknown = [name for name, s in stores.items() if s["bytes"] is None]
    # Read in part -- a simulator gone mid-walk, or unreadable -- is not
    # complete either: counted as complete, the shortfall read as nothing
    # there (CodeRabbit).
    partial = [name for name, s in stores.items()
               if s["bytes"] is not None and s.get("partial")]
    warnings = []
    if settings["warn_bytes"] and used > settings["warn_bytes"]:
        warnings.append(f"simulators and quern's stores take {_gb(used)}, over "
                        f"disk.warn_gb ({_gb(settings['warn_bytes'])})")
    if settings["warn_free_bytes"] and free is not None and free < settings["warn_free_bytes"]:
        warnings.append(f"{_gb(free)} free on the disk, under disk.warn_free_gb "
                        f"({_gb(settings['warn_free_bytes'])})")
    if unknown:
        warnings.append(f"could not measure {', '.join(unknown)}: the total leaves "
                        f"{'it' if len(unknown) == 1 else 'them'} out")
    if partial:
        warnings.append(f"could not read all of {', '.join(partial)}: the total leaves "
                        "some of it out")
    return {"measured_at": now(), "stores": stores, "used_bytes": used,
            "used_complete": not unknown and not partial, "disk": disk, "warnings": warnings,
            "note": settings.get("note")}


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def fetch(port: int, *, host: str = "127.0.0.1", timeout: float = 10.0) -> tuple[dict | None, str]:
    """The running server's disk report, for the CLI; None and why when it
    could not be asked -- distinct from a report, which may say "measuring"."""
    import http.client
    import json
    import urllib.error
    import urllib.request

    try:
        key = config_mod.API_KEY_FILE.read_text().strip()
    except OSError as e:
        return None, f"no API key at {config_mod.API_KEY_FILE}: {e}"
    req = urllib.request.Request(f"http://{host}:{port}/api/v1/system/disk",
                                 headers={"Authorization": f"Bearer {key}",
                                          "X-Quern-Session": "cli"})
    class NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None   # the 3xx is then an HTTPError, said below

    # Never through a proxy from the environment, nor after a redirect: either
    # would be handed the owner's key (CodeRabbit).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return None, f"the server answered {e.code}"
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return None, f"could not ask the server: {e}"
    return (body, "") if isinstance(body, dict) else (None, "the server's answer was not a report")


def report_lines(report: dict, *, now: float | None = None) -> list[str]:
    """The report as indented terminal lines: what is used, free, the largest
    simulators, warnings, and the build store's last sweep."""
    now = time.time() if now is None else now
    if report.get("measuring"):
        return ["measuring (the first measurement runs at start and takes a while)"]
    if not isinstance(report.get("stores"), dict):
        return [f"not measured: {report.get('error') or 'the server answered without a report'}"]
    lines = [summary_line(report)]
    if report.get("last_error"):
        lines.append(f"! the latest measurement failed, so this is an older one: "
                     f"{report['last_error']}")
    sims = report["stores"].get("simulators", {})
    if sims.get("largest"):
        named = ", ".join(f"{d.get('name') or d['udid'][:8]} {_gb(d['bytes'])}"
                          for d in sims["largest"][:3])
        lines.append(f"largest simulators ({sims.get('count', '?')} in all): {named}")
    lines += [f"! {w}" for w in report.get("warnings", [])]
    age = max(0, int(now - report.get("measured_at", now)))
    lines.append(f"measured {age // 60} min ago" if age >= 60 else "measured just now")
    sweep = report.get("build_store_sweep")
    if sweep:
        removed = [d for d in sweep.get("removed", []) if d.get("ok")]
        freed = sum(d.get("bytes", 0) for d in removed)
        lines.append(f"build store: last sweep removed {len(removed)} DerivedData "
                     f"({_gb(freed)})" if removed else "build store: last sweep removed nothing")
    return lines


def summary_line(report: dict) -> str:
    """One line for a terminal: used, free, and warnings."""
    s = report["stores"]
    parts = [f"{name} {_gb(v['bytes'])}" if v["bytes"] is not None else f"{name} unknown"
             for name, v in s.items()]
    free = report["disk"].get("free_bytes")
    line = (f"{_gb(report['used_bytes'])} used ({', '.join(parts)}); "
            f"{_gb(free) + ' free' if free is not None else 'free space unknown'}")
    return line


async def run_forever(app: Any) -> None:
    """Measure at start and every MEASURE_SECONDS after, into
    `app.state.disk_usage`. A pass that fails leaves the last good report and
    says why in `app.state.disk_usage_error`: left as None, a failure read as
    "measuring" for good (review). Cancelled, it stops the walk in its thread
    rather than hold up the server's exit. Never raises."""
    stop = threading.Event()
    while True:
        try:
            report = await asyncio.to_thread(measure, stop=stop)
            app.state.disk_usage = report
            app.state.disk_usage_error = None
            for warning in report["warnings"]:
                logger.warning("Disk: %s", warning)
        except asyncio.CancelledError:
            stop.set()
            raise
        except config_mod.ConfigUnreadable as e:
            app.state.disk_usage_error = {"error": f"skipped: {e}", "at": time.time()}
            logger.warning("Disk measurement skipped: %s", e)
        except Exception as e:  # noqa: BLE001 -- the measurement must outlive one bad pass
            app.state.disk_usage_error = {"error": str(e) or type(e).__name__, "at": time.time()}
            logger.exception("Disk measurement failed")
        await asyncio.sleep(MEASURE_SECONDS)
