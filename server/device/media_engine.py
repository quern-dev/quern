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
import logging
import shutil
import subprocess
from pathlib import Path

from server.config import CONFIG_DIR

logger = logging.getLogger(__name__)

QUERN_BIN_DIR = CONFIG_DIR / "bin"
BINARY_NAME = "quern-media"

# Build intermediates go outside the repo. SwiftPM puts a ~116 MB .build
# directory next to Package.swift by default, and quern's sources can live
# somewhere a user should not be written to -- a pip install puts them under
# site-packages. --scratch-path moves it without relying on an ignore rule.
SCRATCH_DIR = CONFIG_DIR / "build" / "QuernMedia"

_PACKAGE_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "macos" / "QuernMedia",
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


def _newest_source_mtime(package: Path) -> float:
    """Most recent mtime across the package's sources and manifest.

    Walks the tree rather than checking Package.swift alone: editing a source
    file without touching the manifest is the normal case, and a freshness
    test that misses it would serve a stale binary indefinitely.
    """
    newest = (package / "Package.swift").stat().st_mtime
    for path in (package / "Sources").rglob("*.swift"):
        newest = max(newest, path.stat().st_mtime)
    return newest


async def build_media_engine(force: bool = False) -> Path:
    """Build quern-media and install it, returning the binary path.

    Async because every caller inside the server is, and the work below
    blocks for up to BUILD_TIMEOUT seconds: called directly from a request
    path it would stall the event loop for three minutes. The blocking half
    is `build_media_engine_sync`, which `quern setup` and the tests call
    directly because neither has a loop to protect.
    """
    return await asyncio.to_thread(build_media_engine_sync, force)


def build_media_engine_sync(force: bool = False) -> Path:
    """The blocking half of `build_media_engine`.

    A no-op when the installed binary is newer than every source. Raises
    RuntimeError with an actionable message when sources or the toolchain are
    missing, so callers can decide whether that is fatal or a skipped
    optional step.
    """
    package = package_dir()
    if package is None:
        raise RuntimeError(
            "QuernMedia sources not found. Expected macos/QuernMedia/Package.swift "
            "relative to the project root."
        )

    installed = binary_path()
    if not force and installed.exists():
        if installed.stat().st_mtime >= _newest_source_mtime(package):
            return installed

    swift = shutil.which("swift")
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
    shutil.copy2(built, installed)
    logger.info("quern-media installed at %s", installed)
    return installed


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
