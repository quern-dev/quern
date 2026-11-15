"""Builds the QuernMedia Swift package and locates its binary.

Separate from preview.py on purpose. `ios-preview` is the shipping preview
app -- a single swiftc invocation, driving the menu-bar preview windows over
its JSON-lines protocol -- and it keeps doing that job untouched.
`quern-media` is additive: simulator capture, streaming and recording, none
of which ios-preview can do.

Two Swift binaries with overlapping jobs is a real cost, and deliberate. The
alternative was porting ios-preview's interactive protocol under time
pressure to consolidate, which risks breaking a working feature nobody has
complained about. Retire ios-preview when there is a reason beyond tidiness.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from server.config import CONFIG_DIR
from server.device.ios._xcode import xcode_available

logger = logging.getLogger(__name__)

QUERN_BIN_DIR = CONFIG_DIR / "bin"
BINARY_NAME = "quern-media"

# Build intermediates go outside the repo. SwiftPM puts a ~116 MB .build
# directory next to Package.swift by default, and quern's sources can live
# somewhere a user should not be written to -- a pip install puts them under
# site-packages. --scratch-path moves it without relying on an ignore rule.
SCRATCH_DIR = CONFIG_DIR / "build" / "QuernMedia"

_PACKAGE_CANDIDATES = [
    Path(__file__).resolve().parents[3] / "macos" / "QuernMedia",
]

# A cold build is ~7s against ~1.5s for ios-preview's single swiftc call.
# That is the price of a test target that runs with no device attached.
BUILD_TIMEOUT = 180


def package_dir() -> Path | None:
    """The QuernMedia package directory, or None when sources are absent."""
    for candidate in _PACKAGE_CANDIDATES:
        if (candidate / "Package.swift").exists():
            return candidate
    return None


def binary_path() -> Path:
    """Where the built binary is installed."""
    return QUERN_BIN_DIR / BINARY_NAME


# Bumped by hand when the build itself changes what it produces from the same
# sources -- the swift build arguments below, say. Part of the digest so that
# change rebuilds too; the sources alone would not notice it.
_DIGEST_RECIPE = "quern-media build 1"

# One build at a time. The server's start-up check and a first simulator
# stream can both find the binary out of date, and two `swift build`s into one
# scratch directory wait on SwiftPM's lock and then both install. The second
# caller, let in after the first, finds the stamp current and returns.
_build_lock = threading.Lock()


def _sources(package: Path) -> list[Path]:
    """What the binary is built from: the manifest and everything under
    Sources, not Tests. Every file, not only *.swift, so a resource added
    later is covered without anyone remembering this list -- except hidden
    ones: Finder's .DS_Store would rebuild on a folder opened in Finder, and
    no shipped copy (#663) would ever match it."""
    sources = package / "Sources"
    return [package / "Package.swift",
            *sorted(p for p in sources.rglob("*")
                    if p.is_file()
                    and not any(part.startswith(".")
                                for part in p.relative_to(sources).parts))]


def source_digest(package: Path) -> str:
    """A hash of what quern-media is built from: each file's path within the
    package and its bytes, then the build recipe.

    Content, not file times. A release tarball is made by `git archive`, which
    stamps every file with its commit's time, and extracting it keeps that --
    so an extracted source can look older than a binary built last week, and
    the old mtime check kept the stale binary. The same defeat sim-bridge's
    stamp exists for.

    The local build's paths and toolchain are left out, so a signed copy built
    elsewhere from the same sources (#663) is current here: its release step
    writes this digest beside it.
    """
    h = hashlib.sha256()
    for path in _sources(package):
        h.update(path.relative_to(package).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    h.update(_DIGEST_RECIPE.encode())
    return h.hexdigest()


def expected_digest() -> str | None:
    """The digest these sources build, or None when they are not here."""
    package = package_dir()
    return None if package is None else source_digest(package)


def stamp_path() -> Path:
    """The digest of the sources the installed binary was built from. Beside
    the binary rather than inside it, as sim-bridge's is: a signed binary
    cannot be written to, and a missing stamp and a half-installed binary
    both read as "rebuild"."""
    return binary_path().with_name(f"{BINARY_NAME}.sha256")


def installed_digest() -> str | None:
    """The digest recorded for the installed binary, or None when there is no
    binary or no readable stamp."""
    if not binary_path().exists():
        return None
    try:
        return stamp_path().read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


async def build_media_engine(force: bool = False) -> Path:
    """Build quern-media and install it, returning the binary path.

    Async because every caller inside the server is, and the work below
    blocks for up to BUILD_TIMEOUT seconds: called directly from a request
    path it would stall the event loop for three minutes. The blocking half
    is `build_media_engine_sync`, which the tests call directly because they
    have no loop to protect.

    `quern setup` builds it too (`build_simulator_preview_engine`, #285), and
    the server checks it at start, so this is normally a no-op digest check.
    It still builds when the sources changed since the installed binary was
    built -- a pull or a branch switch -- or when nothing else could.
    """
    return await asyncio.to_thread(build_media_engine_sync, force)


def build_media_engine_sync(force: bool = False) -> Path:
    """The blocking half of `build_media_engine`.

    A no-op when the installed binary's stamp matches the sources. Raises
    RuntimeError with an actionable message when sources or the toolchain are
    missing, so callers can decide whether that is fatal or a skipped
    optional step.
    """
    with _build_lock:
        return _build_locked(force)


def _build_locked(force: bool) -> Path:
    package = package_dir()
    if package is None:
        raise RuntimeError(
            "QuernMedia sources not found. Expected macos/QuernMedia/Package.swift "
            "relative to the project root."
        )

    installed = binary_path()
    try:
        digest = source_digest(package)
    except OSError as exc:
        raise RuntimeError(f"Could not read the QuernMedia sources: {exc}") from exc
    if not force and installed_digest() == digest:
        return installed

    # Asked before `which`: /usr/bin/swift is a stub on every Mac, and running
    # it without the developer tools shows macOS's install dialog (#659).
    swift = shutil.which("swift") if xcode_available() else None
    if swift is None:
        raise RuntimeError(
            "swift not found. Install Xcode or the Command Line Tools: "
            "xcode-select --install"
        )

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Building quern-media (%s)", package)
    try:
        proc = subprocess.run(  # noqa: S603
            [
                swift, "build",
                "--package-path", str(package),
                "--scratch-path", str(SCRATCH_DIR),
                "-c", "release",
                "--product", BINARY_NAME,
            ],
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"swift build did not finish within {BUILD_TIMEOUT}s. "
            "Check the active toolchain with `xcode-select -p`."
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"Failed to build quern-media:\n{detail}")

    built = _locate_built_binary(swift, package)
    QUERN_BIN_DIR.mkdir(parents=True, exist_ok=True)
    # Copy rather than symlink into the scratch tree: the binary should
    # survive a `rm -rf` of the build directory, and it sits alongside
    # ios-preview and sim-bridge where the rest of quern looks for tools.
    #
    # Written to a neighbour and renamed, never over the top. `copy2` keeps
    # the inode and rewrites in place, and macOS validates code-signature
    # pages per vnode -- so rewriting the file underneath a running
    # `quern-media` can kill it with a signature fault, and a concurrent
    # `add_simulator` can exec a half-written one. The trigger is ordinary:
    # edit QuernMedia with one preview open, then open a second.
    #
    # `os.replace` is atomic within a filesystem, and the temporary is made
    # in the destination directory so it is the same one. A running process
    # keeps the old inode and finishes on it; the next launch sees a complete
    # file.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{BINARY_NAME}.", suffix=".new", dir=str(QUERN_BIN_DIR)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(built, tmp_path)
        os.replace(tmp_path, installed)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    # After the install, never before: a stamp written first would mark a
    # failed build current. Renamed into place for the same reason as the
    # binary -- a half-written stamp would read as some other digest.
    stamp = stamp_path()
    tmp_stamp = stamp.with_name(f".{stamp.name}.new")
    try:
        tmp_stamp.write_text(digest)
        os.replace(tmp_stamp, stamp)
    except OSError:
        # A stamp that cannot be written costs a rebuild next time, which is
        # the safe direction. It must not fail the build that worked.
        tmp_stamp.unlink(missing_ok=True)
        logger.warning("Could not write %s; quern-media is rebuilt next time", stamp,
                       exc_info=True)
    logger.info("quern-media installed at %s", installed)
    return installed


def refresh_at_server_start() -> bool | None:
    """The server's start-up check of quern-media: whether it was rebuilt, or
    None where there is nothing to check (not a Mac, no developer tools, no
    sources, or a checkout that does not own the bin dir).

    The server builds it before every simulator stream anyway. This is for the
    Screen Mirror app run without a server, which starts the quern-media
    installed beside it as it finds it -- and a checkout moved by `git pull`
    never ran setup to rebuild it.
    """
    import platform

    from server.device.media import preview

    if platform.system() != "Darwin" or not xcode_available() or package_dir() is None:
        return None
    if not preview._owns_the_bin_dir():
        return None
    before = file_identity(binary_path())
    installed = build_media_engine_sync()
    # The file replaced, not the stamp changed: a binary deleted under an
    # intact stamp rebuilds to the same digest.
    return file_identity(installed) != before


def file_identity(path: Path) -> tuple[int, int] | None:
    """A file's inode and mtime, None when it is not there: whether a build
    replaced it."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


def _locate_built_binary(swift: str, package: Path) -> Path:
    """Ask SwiftPM where it put the binary rather than guessing the path.

    The layout under the scratch directory includes the target triple, so
    hardcoding it would break on a different architecture.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [
                swift, "build",
                "--package-path", str(package),
                "--scratch-path", str(SCRATCH_DIR),
                "-c", "release",
                "--show-bin-path",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        # Otherwise this escapes as TimeoutExpired while the build step next
        # door raises RuntimeError, so a caller handling the documented
        # contract sees an unhandled exception from the same function.
        raise RuntimeError(
            "swift build --show-bin-path did not finish within 60s. "
            "Check the active toolchain with `xcode-select -p`."
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(f"Could not locate the built binary:\n{proc.stderr.strip()}")
    built = Path(proc.stdout.strip()) / BINARY_NAME
    if not built.exists():
        raise RuntimeError(f"swift build reported success but {built} is missing")
    return built


def is_available() -> bool:
    """Whether a built binary is present. Does not attempt a build."""
    return binary_path().exists()
