"""What external tools quern is actually using, and where they came from.

`check_tools()` used to answer with booleans, one per tool name. That is not
enough to settle a "works on mine": two machines comparing `pymobiledevice3` in
that form agreed they both had it while being three majors apart, and the
disagreement surfaced instead as one of them reading code the other did not
have.

A name is not one thing. Measured on a single machine, `pymobiledevice3` is
**two** installs at once -- an 11.3.1 library imported by `webinspector.py`, and
a 9.15.1 command-line binary used by `tunneld` and the device log -- serving
different code paths. Reporting one number for that name would be reporting a
number nobody is running. So the unit here is the install *site*, not the tool.

Paths are resolved the way quern resolves them rather than through `PATH`,
which does not find `idb` or `mitmdump` on at least one machine. Some are not
durable either: node arrives from fnm at a path containing a pid and a
timestamp, so it is recorded as provenance rather than as a location to revisit.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# A version anywhere in a line: every tool writes it differently.
#   pymobiledevice3  ->  9.15.1
#   idevice_id       ->  idevice_id 1.4.0
#   adb              ->  Android Debug Bridge version 1.0.41
#   node             ->  v22.22.2
#   mitmdump         ->  Mitmproxy: 12.2.3
_VERSION_RE = re.compile(r"\bv?(\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?)")

# Paths whose spelling changes between shells. fnm hands out a per-shell
# directory named for the pid that asked, so the path is evidence of where a
# tool came from and useless as somewhere to look again.
_VOLATILE_MARKERS = ("fnm_multishells", "/private/var/folders/", "/tmp/")


@dataclass
class ToolSite:
    """One place a tool is installed, and what quern uses it for."""

    name: str
    role: str
    """What quern uses this copy as: "library" or "cli"."""

    available: bool = False
    version: str | None = None
    path: str | None = None
    source: str = "unknown"
    """Where it came from: brew, pipx, venv, fnm, android-sdk, xcode, system."""

    detail: str | None = None
    """Anything a reader needs that the fields above cannot carry."""

    volatile_path: bool = False
    """The path will not survive a new shell, so it identifies provenance
    rather than a location."""

    requested: bool | None = None
    """For brew: installed deliberately (True), pulled in as a dependency
    (False), or not recorded (None -- older installs carry neither flag)."""

    required_by: list[str] = field(default_factory=list)
    """Other installed formulae that depend on this one, so a reader knows what
    an upgrade would touch."""

    def describe(self) -> str:
        if not self.available:
            return f"{self.name} ({self.role}): not found"
        where = self.path or self.source
        return f"{self.name} ({self.role}) {self.version or '?'} — {where}"


def parse_version(output: str) -> str | None:
    """The version in a tool's version output, whatever shape it takes."""
    for line in output.splitlines():
        match = _VERSION_RE.search(line)
        if match:
            return match.group(1)
    return None


def is_volatile(path: str | None) -> bool:
    return bool(path) and any(marker in path for marker in _VOLATILE_MARKERS)


def classify_source(path: str | None) -> str:
    """Where a binary came from, judged by where it sits.

    Symlinks are followed first. Brew links its binaries into `bin` directories
    that look like system paths, so classifying the link would report `system`
    for a formula and skip its provenance entirely.
    """
    if not path:
        return "unknown"
    with contextlib.suppress(OSError):
        path = str(Path(path).resolve())
    if "fnm_multishells" in path or "/fnm/" in path:
        return "fnm"
    if "/pipx/" in path:
        return "pipx"
    if "/Android/sdk/" in path or "/android-sdk/" in path:
        return "android-sdk"
    if "/Xcode.app/" in path or path.startswith("/Applications/Xcode"):
        return "xcode"
    # By segment, not prefix: brew's root moves between /opt/homebrew,
    # /usr/local, linuxbrew and whatever HOMEBREW_PREFIX says, but a formula's
    # files always live under a Cellar.
    if "/Cellar/" in path or path.startswith(("/opt/homebrew/", "/home/linuxbrew/")):
        return "brew"
    if "/.venv/" in path:
        return "venv"
    # Only meaningful inside a virtualenv. Run under a system interpreter,
    # sys.executable's directory is /usr/bin, and every binary there would be
    # reported as living in quern's environment.
    if _in_virtualenv() and path.startswith(str(Path(sys.executable).parent)):
        return "venv"
    if path.startswith(("/usr/bin/", "/usr/local/bin/", "/bin/")):
        return "system"
    return "unknown"


def _in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def package_version(distribution: str) -> str | None:
    """A Python package's version, from its metadata rather than its output.

    Exact, and it works for tools that have no --version at all: `idb` prints
    usage for one.
    """
    try:
        import importlib.metadata as metadata

        return metadata.version(distribution)
    except Exception:
        return None


async def _run(args: list[str], timeout: float) -> tuple[int, str]:
    """Run a command off the event loop, returning (exit code, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return 1, ""
    except OSError:
        return 1, ""
    return proc.returncode or 0, out.decode(errors="replace")


async def binary_version(path: str, args: list[str], timeout: float = 5.0) -> str | None:
    """Run a binary's version command and pull the version out of it."""
    try:
        proc = await asyncio.create_subprocess_exec(
            path, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # Kill it: communicate() leaves the child running, and a version
        # command that hangs would otherwise stay alive across requests.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    except OSError:
        return None
    return parse_version(out.decode(errors="replace"))


async def brew_provenance(formula: str) -> tuple[bool | None, list[str]]:
    """Whether a formula was asked for, and what else depends on it.

    Run off the event loop -- `brew info` and `brew uses` are slow enough that
    doing them synchronously would stall every other request for as long as
    they take.

    Both are best effort. `installed_on_request` is absent on installs old
    enough to predate it -- measured, `libusbmuxd` and `openssl@3` carry
    neither flag on this machine -- so "not recorded" is a real answer and not
    the same as "arrived as a dependency".
    """
    requested: bool | None = None
    try:
        import json

        code, stdout = await _run(["brew", "info", "--json=v2", formula], timeout=15)
        if code == 0:
            for entry in (json.loads(stdout).get("formulae") or []):
                for installed in entry.get("installed") or []:
                    if installed.get("installed_on_request"):
                        requested = True
                    elif installed.get("installed_as_dependency"):
                        requested = False
    except Exception:
        return None, []

    required_by: list[str] = []
    try:
        code, stdout = await _run(["brew", "uses", "--installed", formula], timeout=20)
        if code == 0:
            required_by = stdout.split()
    except Exception:
        pass
    return requested, required_by


def which(name: str) -> str | None:
    """The venv's copy first, then PATH -- how quern already resolves idb."""
    venv_copy = Path(sys.executable).parent / name
    if venv_copy.is_file():
        return str(venv_copy)
    return shutil.which(name)


# The sites quern actually uses, in the order a reader wants them. Two entries
# for pymobiledevice3 on purpose: the library and the binary are separate
# installs that drift apart, and collapsing them is what made a three-major gap
# look like agreement.
async def collect_sites() -> list[ToolSite]:
    """Every install site quern depends on, with versions and provenance."""
    sites: list[ToolSite] = []

    # --- pymobiledevice3, as a library ------------------------------------
    library = package_version("pymobiledevice3")
    sites.append(ToolSite(
        name="pymobiledevice3", role="library",
        available=library is not None, version=library,
        path=str(Path(sys.executable).parent.parent), source="venv",
        detail="imported by webinspector.py for web content on simulators",
    ))

    # --- pymobiledevice3, as a binary -------------------------------------
    from server.device.tunneld import find_pymobiledevice3_binary

    binary = find_pymobiledevice3_binary()
    binary_path = str(binary) if binary else None
    sites.append(ToolSite(
        name="pymobiledevice3", role="cli",
        available=binary_path is not None,
        version=await binary_version(binary_path, ["version"]) if binary_path else None,
        path=binary_path, source=classify_source(binary_path),
        volatile_path=is_volatile(binary_path),
        detail="run by tunneld and the device log; a separate install from the library",
    ))

    # --- idb --------------------------------------------------------------
    idb_path = which("idb")
    sites.append(ToolSite(
        name="idb", role="cli",
        available=idb_path is not None,
        # From metadata: `idb --version` prints usage rather than a version.
        version=package_version("fb-idb"),
        path=idb_path, source=classify_source(idb_path),
        volatile_path=is_volatile(idb_path),
    ))

    # --- mitmproxy --------------------------------------------------------
    mitm_path = which("mitmdump")
    sites.append(ToolSite(
        name="mitmproxy", role="cli",
        available=mitm_path is not None,
        version=package_version("mitmproxy"),
        path=mitm_path, source=classify_source(mitm_path),
        volatile_path=is_volatile(mitm_path),
    ))

    # --- adb --------------------------------------------------------------
    adb_path = shutil.which("adb") or _android_sdk_adb()
    sites.append(ToolSite(
        name="adb", role="cli",
        available=adb_path is not None,
        version=await binary_version(adb_path, ["version"]) if adb_path else None,
        path=adb_path, source=classify_source(adb_path),
        volatile_path=is_volatile(adb_path),
    ))

    # --- libimobiledevice -------------------------------------------------
    imd_path = shutil.which("idevice_id")
    site = ToolSite(
        name="libimobiledevice", role="cli",
        available=imd_path is not None,
        version=await binary_version(imd_path, ["--version"]) if imd_path else None,
        path=imd_path, source=classify_source(imd_path),
        volatile_path=is_volatile(imd_path),
    )
    if site.source == "brew":
        site.requested, site.required_by = await brew_provenance("libimobiledevice")
    sites.append(site)

    # --- node -------------------------------------------------------------
    node_path = shutil.which("node")
    sites.append(ToolSite(
        name="node", role="cli",
        available=node_path is not None,
        version=await binary_version(node_path, ["--version"]) if node_path else None,
        path=node_path, source=classify_source(node_path),
        volatile_path=is_volatile(node_path),
        detail="runs the MCP server",
    ))

    return sites


def _android_sdk_adb() -> str | None:
    candidate = Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb"
    return str(candidate) if candidate.is_file() else None


def upgrade_note(site: ToolSite) -> str | None:
    """What a reader should know before upgrading this one.

    Only for brew, and only when brew actually recorded something: an install
    predating the flag reports neither, and "not recorded" must not be read as
    "arrived as a dependency".
    """
    if site.source != "brew":
        return None
    parts: list[str] = []
    if site.requested is False:
        parts.append(
            "arrived as a dependency of something else, so upgrading it "
            "directly may be undone by whatever pulled it in"
        )
    elif site.requested is None:
        parts.append("brew did not record whether this was asked for")
    if site.required_by:
        parts.append("also required by " + ", ".join(sorted(site.required_by)))
    return "; ".join(parts) or None
