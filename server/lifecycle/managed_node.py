"""Quern's own Node.js: the official nodejs.org build, kept in ~/.quern/node (#760).

Node is needed for one thing, the MCP wrapper (`mcp/`), and finding a `node`
every part of quern can reach has been expensive: four shell contexts probed
by `node_env` because fnm and nvm put node where a Dock-launched app cannot see
it (#214, #447), a menu bar updater that hands over to Terminal when a manager
hides it (#339), and on Intel Macs a `brew install node` that compiles for over
an hour now Homebrew ships no Intel bottles (#757).

So quern installs a Node of its own and refers to it by absolute path:

    ~/.quern/node/<version>/        one unpacked release, minus include/
    ~/.quern/node/current -> <version>

Nothing here puts it on the user's PATH; their shell's `node` is theirs.

A release is only switched to after every check passes, in this order: its
SHA-256 against the release's SHASUMS256.txt, the code signature's team
(the Node.js Foundation's Developer ID), and `node --version` reporting the
version that was asked for. Measured on a 2018 MacBook Pro (x86_64): the
download takes about 2 s, and the wrapper builds and lists its tools with it.

Intel Macs top out at the 26 line: nodejs.org publishes `osx-x64` builds for
every release line through 26, and none for the 27 prerelease. Choosing from
the index by file rather than by version keeps that automatic.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from server.config import CONFIG_DIR

DIST_URL = "https://nodejs.org/dist"

#: The Node.js Foundation's Developer ID team, which signs the macOS builds.
TEAM_ID = "HX7739G8FX"

#: Bounds for each network fetch. The tarball is ~50 MB; a stalled connection
#: should fail rather than hang setup, which is a different thing from a slow
#: build (#757): nothing here builds.
FETCH_TIMEOUT = 120.0
RUN_TIMEOUT = 30.0

#: Parts of a release quern never runs: C headers for building native addons.
_SKIPPED_TOP_LEVEL = ("include",)


def node_dir() -> Path:
    """Where quern's Node versions live. A function so tests redirecting
    QUERN_STATE_DIR after import are honoured."""
    return CONFIG_DIR / "node"


def current_dir() -> Path:
    return node_dir() / "current"


def current_binary() -> Path:
    """The path every consumer uses. Stable across updates: only the
    `current` link moves."""
    return current_dir() / "bin" / "node"


def current_bin_dir() -> Path:
    return current_dir() / "bin"


class ManagedNodeError(Exception):
    """A step of installing quern's Node failed; the message says which."""


@dataclass(frozen=True)
class Release:
    version: str   # "v26.13.1"
    arch: str      # "x64" or "arm64"

    @property
    def dirname(self) -> str:
        return f"node-{self.version}-darwin-{self.arch}"

    @property
    def filename(self) -> str:
        return f"{self.dirname}.tar.gz"

    @property
    def url(self) -> str:
        return f"{DIST_URL}/{self.version}/{self.filename}"

    @property
    def shasums_url(self) -> str:
        return f"{DIST_URL}/{self.version}/SHASUMS256.txt"


def machine_arch(machine: str | None = None) -> str | None:
    """nodejs.org's name for this Mac's architecture, or None if it has none."""
    machine = machine if machine is not None else platform.machine()
    return {"arm64": "arm64", "x86_64": "x64"}.get(machine)


def _parse(version: str) -> tuple[int, int, int] | None:
    from server.lifecycle import node_env

    return node_env.parse_version(version)


def choose_release(index: list[dict], arch: str) -> Release | None:
    """The newest LTS release with a macOS build for `arch` that quern accepts.

    "Accepts" is the wrapper's floor and the security floor from `node_env`,
    so this never picks a Node the rest of quern would warn about. The index
    is newest-first, but this sorts anyway rather than trusting the order.
    """
    from server.lifecycle import node_env

    wanted = f"osx-{arch}-tar"
    best: tuple[tuple[int, int, int], str] | None = None
    for entry in index:
        version = entry.get("version") or ""
        if not entry.get("lts") or wanted not in (entry.get("files") or []):
            continue
        parsed = _parse(version)
        if parsed is None or node_env.meets_floor(version) is not True:
            continue
        if node_env.security_gap(version):
            continue
        if best is None or parsed > best[0]:
            best = (parsed, version)
    return Release(best[1], arch) if best else None


def same_line(a: str | None, b: str | None) -> bool:
    pa, pb = (_parse(a) if a else None), (_parse(b) if b else None)
    return bool(pa and pb and pa[0] == pb[0])


# ── external effects, injectable ──────────────────────────────────────────

Fetch = Callable[[str, Path], None]
Runner = Callable[..., subprocess.CompletedProcess]


def _fetch(url: str, dest: Path) -> None:
    """Download `url` to `dest`, streaming."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=RUN_TIMEOUT, **kwargs)


def fetch_index(fetch: Fetch | None = None) -> list[dict]:
    fetch = fetch or _fetch
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "index.json"
        try:
            fetch(f"{DIST_URL}/index.json", dest)
            return json.loads(dest.read_text())
        except (OSError, ValueError) as exc:
            raise ManagedNodeError(f"could not read the Node release index: {exc}") from exc


# ── checks ────────────────────────────────────────────────────────────────

def _expected_sha256(shasums: str, filename: str) -> str | None:
    for line in shasums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == filename:
            return parts[0].lower()
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_signature(binary: Path, run: Runner) -> None:
    """The binary is signed, the signature is intact, and it is the Node.js
    Foundation's. `codesign` ships with macOS, not with the developer tools."""
    try:
        verify = run(["/usr/bin/codesign", "--verify", "--strict", str(binary)])
        info = run(["/usr/bin/codesign", "-dv", str(binary)])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedNodeError(f"could not check node's signature: {exc}") from exc
    if verify.returncode != 0:
        raise ManagedNodeError(f"node's signature does not verify: {verify.stderr.strip()}")
    details = (info.stdout or "") + (info.stderr or "")
    if f"TeamIdentifier={TEAM_ID}" not in details:
        raise ManagedNodeError(
            f"node is not signed by the Node.js Foundation (expected team {TEAM_ID})")


def version_of(binary: Path, run: Runner | None = None) -> str | None:
    """`node --version`, or None when it does not run."""
    run = run or _run
    try:
        result = run([str(binary), "--version"])
    except (OSError, subprocess.SubprocessError):
        return None
    out = (result.stdout or "").strip()
    return out if result.returncode == 0 and out.startswith("v") else None


def _safe_members(archive: tarfile.TarFile, top: str):
    """Members under `top/`, minus the parts quern does not use, refusing
    anything that would land outside the destination."""
    for member in archive.getmembers():
        name = member.name
        parts = Path(name).parts
        if not parts or parts[0] != top or name.startswith("/") or ".." in parts:
            raise ManagedNodeError(f"unexpected path in the Node archive: {name}")
        if len(parts) > 1 and parts[1] in _SKIPPED_TOP_LEVEL:
            continue
        if member.issym() or member.islnk():
            target = Path(member.linkname)
            resolved = (Path(name).parent / target) if member.issym() else target
            if target.is_absolute() or ".." in Path(os.path.normpath(resolved)).parts:
                raise ManagedNodeError(
                    f"link escapes the Node archive: {name} -> {member.linkname}")
        elif not (member.isfile() or member.isdir()):
            continue  # devices, fifos: nothing a Node release ships
        yield member


# ── install, update, remove ───────────────────────────────────────────────

def installed_version(run: Runner | None = None) -> str | None:
    """The version `current` runs, or None when there is no working one."""
    binary = current_binary()
    return version_of(binary, run) if binary.exists() else None


def install(release: Release, *, fetch: Fetch | None = None,
            run: Runner | None = None) -> Path:
    """Download, verify and unpack `release`, then point `current` at it.

    `current` moves only after every check has passed, so a failure at any
    step leaves the previous Node in place and working. Returns the binary.
    """
    fetch, run = fetch or _fetch, run or _run
    root = node_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".download-") as tmp:
        tmp_path = Path(tmp)
        tarball = tmp_path / release.filename
        shasums = tmp_path / "SHASUMS256.txt"
        try:
            fetch(release.shasums_url, shasums)
            fetch(release.url, tarball)
        except OSError as exc:
            raise ManagedNodeError(f"could not download Node {release.version}: {exc}") from exc

        expected = _expected_sha256(shasums.read_text(), release.filename)
        if expected is None:
            raise ManagedNodeError(f"{release.filename} is not listed in SHASUMS256.txt")
        if _sha256(tarball) != expected:
            raise ManagedNodeError(f"{release.filename} does not match its published SHA-256")

        unpacked = tmp_path / "unpacked"
        unpacked.mkdir()
        try:
            with tarfile.open(tarball, "r:gz") as archive:
                members = list(_safe_members(archive, release.dirname))
                # The stdlib's "data" filter where it exists (3.11.4+), on top
                # of the checks above: it also drops setuid bits and absolute
                # modes, and 3.14 refuses to extract without a filter choice.
                extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                archive.extractall(unpacked, members=members, **extra)  # noqa: S202
        except (OSError, tarfile.TarError) as exc:
            raise ManagedNodeError(f"could not unpack Node {release.version}: {exc}") from exc

        staged = unpacked / release.dirname
        binary = staged / "bin" / "node"
        _check_signature(binary, run)
        reported = version_of(binary, run)
        if reported != release.version:
            raise ManagedNodeError(
                f"the downloaded node reports {reported or 'nothing'}, expected {release.version}")

        final = root / release.version
        if final.exists():
            shutil.rmtree(final)
        staged.rename(final)

    _point_current_at(release.version)
    _remove_other_versions(keep=release.version)
    return current_binary()


def _point_current_at(version: str) -> None:
    """Swap the `current` link in one step: a client starting during an
    update sees the old Node or the new one, never neither."""
    root = node_dir()
    link = current_dir()
    tmp_link = root / f".current-{os.getpid()}"
    tmp_link.unlink(missing_ok=True)
    os.symlink(version, tmp_link)
    os.replace(tmp_link, link)


def _remove_other_versions(keep: str) -> None:
    """Old versions go once `current` has moved. A process still running one
    keeps its open binary; nothing new can start it."""
    for entry in node_dir().iterdir():
        if entry.name.startswith(".") or entry.name in (keep, "current"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def remove() -> bool:
    """Delete quern's Node entirely (uninstall). True when there was one."""
    root = node_dir()
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


@dataclass(frozen=True)
class Outcome:
    """What `ensure` did, for setup and update to report."""

    version: str | None      # what `current` runs afterwards, None if nothing
    action: str              # "installed", "updated", "current", "unsupported", "failed"
    detail: str = ""


def ensure(*, allow_install: bool = True, fetch: Fetch | None = None,
           run: Runner | None = None, arch: str | None = None) -> Outcome:
    """Make `current` the newest acceptable release in its line, installing
    one if there is none (when `allow_install`).

    Staying within a line on update is deliberate: a security fix lands as a
    patch, and a new major can wait for a release of quern that has been
    tested on it. A failure never removes a working Node.
    """
    fetch, run = fetch or _fetch, run or _run
    arch = arch if arch is not None else machine_arch()
    have = installed_version(run)
    if arch is None:
        return Outcome(have, "unsupported", f"no Node build for {platform.machine()}")
    if have is None and not allow_install:
        return Outcome(None, "failed", "quern's Node is not installed")
    try:
        index = fetch_index(fetch)
        if have:
            same = [e for e in index if same_line(e.get("version"), have)]
            target = choose_release(same, arch)
            if target is None or (_parse(target.version) or ()) <= (_parse(have) or ()):
                return Outcome(have, "current")
            install(target, fetch=fetch, run=run)
            return Outcome(target.version, "updated", f"from {have}")
        target = choose_release(index, arch)
        if target is None:
            return Outcome(None, "failed", f"nodejs.org lists no LTS build for darwin-{arch}")
        install(target, fetch=fetch, run=run)
        return Outcome(target.version, "installed")
    except ManagedNodeError as exc:
        return Outcome(have, "failed", str(exc))
