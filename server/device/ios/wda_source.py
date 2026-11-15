"""WebDriverAgent's source, at the version quern pins (#479).

WDA used to be `git clone`d once from appium/WebDriverAgent and never updated,
so every install kept whatever was latest on its first setup -- the main dev
machine ran 11.4.0 while a fresh clone ran 16.15 -- and that repository has
since moved into the appium-ios monorepo and stopped. So the version lives in
`wda-pin.json`, beside this file, and the source is fetched from npm's
`appium-webdriveragent` package at exactly that version: one tarball, checked
against the pinned sha512 (npm's `integrity`), holding the same Xcode project
the monorepo tags. A marker in the source directory says which version it
holds, so a pin that moved is fetched again; the *build* the old version made
is what stays usable until a new one works (wda.py's build_wda).

To move the pin: `scripts/wda-pin.py --bump [VERSION]`, then a live test on a
simulator and a phone. That test is also what checks quern's patch still
applies (`_patch`): the script does not download the source, and a source the
patch cannot be made on is refused at fetch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PIN_FILE = Path(__file__).with_name("wda-pin.json")
REGISTRY = "https://registry.npmjs.org"
FETCH_TIMEOUT = 120.0
#: Marks a source directory as complete, and says which version it holds.
MARKER = ".quern-source"

#: How many of quern's own changes the unpacked source carries. It is part of
#: the version quern records and pins, so adding one makes every source and
#: build made without it out of date -- the path a pin bump takes.
PATCH_LEVEL = 1

#: The one line of the runner's setUp that turns auto-correction and predictive
#: text off, system-wide, every time WDA starts -- and nothing ever turns them
#: back on. On a phone those are its owner's settings, so the call is kept for
#: simulators only. It also marks the keyboard tutorials as seen, which a fresh
#: simulator needs and a phone in use has done long since (#687 review). On a
#: phone, WDA's typing then meets the owner's auto-correction: type_text reads
#: the field back and reports a rewrite as an error, which is the trade chosen
#: over changing their settings.
_KEYBOARD_PREFERENCES = "  [FBConfiguration.sharedInstance configureDefaultKeyboardPreferences];\n"
_SIMULATOR_ONLY = ("#if TARGET_OS_SIMULATOR\n" + _KEYBOARD_PREFERENCES
                   + "#endif  // quern: a phone's keyboard settings are its owner's\n")
_RUNNER_SETUP = Path("WebDriverAgentRunner") / "UITestingUITests.m"


class SourceError(RuntimeError):
    """The pinned WDA source could not be had; the message says why."""


@dataclass(frozen=True)
class Pin:
    package: str
    version: str
    integrity: str

    @property
    def tarball(self) -> str:
        return f"{REGISTRY}/{self.package}/-/{self.package}-{self.version}.tgz"


def pinned() -> Pin:
    data = json.loads(PIN_FILE.read_text())
    return Pin(package=data["package"], version=data["version"],
               integrity=data["integrity"])


def _check_integrity(data: bytes, integrity: str) -> None:
    algo, _, expected = integrity.partition("-")
    if algo != "sha512" or not expected:
        raise SourceError(f"the WDA pin's integrity {integrity!r} is not a sha512 one")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        raise SourceError("the WebDriverAgent download does not match the pinned sha512: "
                          "refusing to build it")


def source_version(pin: Pin) -> str:
    """The version quern's patched source of `pin` is recorded as."""
    return f"{pin.version}+quern{PATCH_LEVEL}"


def _patch(root: Path) -> None:
    """quern's changes to the unpacked source: SourceError if one cannot be
    made, so a moved pin that changed the line is caught rather than built
    without it."""
    runner = root / _RUNNER_SETUP
    try:
        text = runner.read_text()
    except (OSError, UnicodeError) as e:
        # UnicodeError too: it is a ValueError, which nothing above this turns
        # into a source that could not be had (#687 review).
        raise SourceError(f"WebDriverAgent's {_RUNNER_SETUP} could not be read: {e}") from None
    if text.count(_KEYBOARD_PREFERENCES) != 1:
        raise SourceError(
            f"{_RUNNER_SETUP} no longer calls configureDefaultKeyboardPreferences once, "
            "as quern's patch expects: check the patch against this WebDriverAgent")
    runner.write_text(text.replace(_KEYBOARD_PREFERENCES, _SIMULATOR_ONLY))


def fetched_version(dest: Path) -> str | None:
    """The version of the source in `dest`, or None: an old git clone, a
    partial unpack, or nothing at all."""
    try:
        return (dest / MARKER).read_text().strip() or None
    except OSError:
        return None


async def fetch(dest: Path, pin: Pin | None = None, *,
                client: httpx.AsyncClient | None = None) -> bool:
    """The pinned source in `dest`, downloaded and unpacked if it is not there
    already: True if it was fetched now. SourceError, saying why, if it cannot
    be had -- and then `dest` is as it was."""
    pin = pin or pinned()
    if fetched_version(dest) == source_version(pin):
        return False
    logger.info("Fetching WebDriverAgent %s from %s", pin.version, pin.tarball)
    own = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True)
    try:
        resp = await client.get(pin.tarball)
        resp.raise_for_status()
        data = resp.content
    except httpx.HTTPError as e:
        raise SourceError(f"could not download WebDriverAgent {pin.version} from npm: "
                          f"{e}") from None
    finally:
        if own:
            await client.aclose()
    _check_integrity(data, pin.integrity)
    import asyncio

    await asyncio.to_thread(_unpack, data, dest, source_version(pin))
    return True


def _unpack(data: bytes, dest: Path, version: str) -> None:
    """Unpack into a scratch directory beside `dest`, then move it into place:
    an interrupted unpack never leaves a directory that looks complete."""
    import io

    if not hasattr(tarfile, "data_filter"):
        raise SourceError("this Python cannot unpack an archive safely (it needs "
                          "3.11.4 or later)")
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    scratch = parent / f".{dest.name}.partial"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # npm packs everything under package/.
            members = [m for m in tar.getmembers()
                       if m.name.startswith("package/") and len(m.name) > len("package/")]
            for m in members:
                m.name = m.name[len("package/"):]
                # A hard link names its target by archive path, prefix and all:
                # unrewritten, extraction raised a bare KeyError (review).
                if m.islnk() and m.linkname.startswith("package/"):
                    m.linkname = m.linkname[len("package/"):]
            tar.extractall(scratch, members=members, filter="data")
        if not (scratch / "WebDriverAgent.xcodeproj" / "project.pbxproj").is_file():
            raise SourceError(f"WebDriverAgent {version}'s package holds no "
                              "WebDriverAgent.xcodeproj")
        _patch(scratch)
        (scratch / MARKER).write_text(version + "\n")
        # Swapped, not deleted then renamed: there is no moment with no source,
        # and a rename that fails puts the old one back (review).
        old = parent / f".{dest.name}.old"
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            dest.rename(old)
        try:
            scratch.rename(dest)
        except OSError:
            if old.exists():
                old.rename(dest)
            raise
        shutil.rmtree(old, ignore_errors=True)
    except (tarfile.TarError, OSError, KeyError) as e:
        shutil.rmtree(scratch, ignore_errors=True)
        raise SourceError(f"could not unpack WebDriverAgent {version}: {e}") from None
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
