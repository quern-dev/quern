"""WebDriverAgent's source at the pinned version (#479): fetched from npm,
checked against the pinned sha512, unpacked whole or not at all. Never from
the network here: httpx's MockTransport answers.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile

import httpx
import pytest

from server.device.ios import wda_source
from server.device.ios.wda_source import MARKER, Pin, SourceError, fetch


def _tgz(members: dict[str, bytes | None], prefix: str = "package/") -> bytes:
    """A gzipped tar like npm's: everything under package/ (None: a directory)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(prefix + name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


PROJECT = {"WebDriverAgent.xcodeproj": None,
           "WebDriverAgent.xcodeproj/project.pbxproj": b"// pbxproj",
           "WebDriverAgentLib": None,
           "WebDriverAgentLib/FBAlert.m": b"// alert",
           "package.json": b'{"version": "9.9.9"}'}


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _client(data: bytes, seen: list, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, content=data)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _pin(data: bytes, version: str = "9.9.9") -> Pin:
    return Pin("appium-webdriveragent", version, _integrity(data))


async def test_the_pinned_tarball_is_fetched_checked_and_unpacked(tmp_path):
    data = _tgz(PROJECT)
    seen: list = []
    dest = tmp_path / "WebDriverAgent"
    async with _client(data, seen) as client:
        assert await fetch(dest, _pin(data), client=client) is True
    assert seen == ["https://registry.npmjs.org/appium-webdriveragent/-/"
                    "appium-webdriveragent-9.9.9.tgz"]
    assert (dest / "WebDriverAgent.xcodeproj" / "project.pbxproj").read_bytes() == b"// pbxproj"
    assert not (dest / "package").exists(), "npm's package/ prefix is stripped"
    assert (dest / MARKER).read_text().strip() == "9.9.9"
    assert wda_source.fetched_version(dest) == "9.9.9"


async def test_the_version_already_there_is_not_fetched_again(tmp_path):
    data = _tgz(PROJECT)
    dest = tmp_path / "WebDriverAgent"
    async with _client(data, []) as client:
        await fetch(dest, _pin(data), client=client)
    seen: list = []
    async with _client(data, seen) as client:
        assert await fetch(dest, _pin(data), client=client) is False
    assert seen == []


async def test_an_old_git_clone_is_replaced_by_the_pinned_version(tmp_path):
    """It carries no marker: every install made before #479 has one."""
    dest = tmp_path / "WebDriverAgent"
    (dest / ".git").mkdir(parents=True)
    (dest / "old-file").write_text("11.4.0")
    data = _tgz(PROJECT)
    async with _client(data, []) as client:
        assert await fetch(dest, _pin(data), client=client) is True
    assert not (dest / ".git").exists() and not (dest / "old-file").exists()
    assert wda_source.fetched_version(dest) == "9.9.9"


async def test_a_moved_pin_fetches_the_new_version(tmp_path):
    dest = tmp_path / "WebDriverAgent"
    old = _tgz(PROJECT)
    async with _client(old, []) as client:
        await fetch(dest, _pin(old, "9.9.8"), client=client)
    new = _tgz({**PROJECT, "package.json": b'{"version": "9.9.9"}', "NEW": b"x"})
    async with _client(new, []) as client:
        assert await fetch(dest, _pin(new, "9.9.9"), client=client) is True
    assert (dest / "NEW").exists() and wda_source.fetched_version(dest) == "9.9.9"


async def test_a_download_that_does_not_match_the_pin_is_refused_and_changes_nothing(tmp_path):
    dest = tmp_path / "WebDriverAgent"
    (dest / ".git").mkdir(parents=True)
    pin = _pin(_tgz(PROJECT))
    tampered = _tgz({**PROJECT, "WebDriverAgentLib/FBAlert.m": b"// something else"})
    async with _client(tampered, []) as client:
        with pytest.raises(SourceError, match="sha512"):
            await fetch(dest, pin, client=client)
    assert (dest / ".git").exists(), "the source there before is untouched"
    assert not list(tmp_path.glob(".*partial"))


async def test_a_failed_download_says_so_and_changes_nothing(tmp_path):
    dest = tmp_path / "WebDriverAgent"
    (dest / ".git").mkdir(parents=True)
    async with _client(b"not found", [], status=404) as client:
        with pytest.raises(SourceError, match="could not download"):
            await fetch(dest, _pin(b"x"), client=client)
    assert (dest / ".git").exists()


async def test_a_package_without_the_project_is_refused_and_leaves_nothing(tmp_path):
    data = _tgz({"README.md": b"moved"})
    dest = tmp_path / "WebDriverAgent"
    async with _client(data, []) as client:
        with pytest.raises(SourceError, match="no WebDriverAgent.xcodeproj"):
            await fetch(dest, _pin(data), client=client)
    assert not dest.exists() and not list(tmp_path.glob(".*partial"))


async def test_a_member_that_would_leave_the_directory_is_refused(tmp_path):
    """The checksum is the main line; the data filter is the second."""
    data = _tgz({**PROJECT, "../../escape": b"x"})
    dest = tmp_path / "wda" / "WebDriverAgent"
    async with _client(data, []) as client:
        with pytest.raises(SourceError):
            await fetch(dest, _pin(data), client=client)
    assert not (tmp_path / "escape").exists() and not dest.exists()


def test_the_pin_in_the_repo_is_well_formed():
    """What every install builds: a version npm publishes, and its sha512."""
    pin = wda_source.pinned()
    assert pin.package == "appium-webdriveragent"
    assert pin.version.count(".") == 2 and all(p.isdigit() for p in pin.version.split("."))
    algo, _, digest = pin.integrity.partition("-")
    assert algo == "sha512" and len(base64.b64decode(digest)) == 64
    assert json.loads(wda_source.PIN_FILE.read_text())["version"] == pin.version
