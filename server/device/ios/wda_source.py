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
simulator and a phone.
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
    if fetched_version(dest) == pin.version:
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

    await asyncio.to_thread(_unpack, data, dest, pin.version)
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
