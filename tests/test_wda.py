"""Tests for WebDriverAgent setup (server/device/wda.py and server/api/wda.py).

All subprocess calls are mocked — no real git/xcodebuild/devicectl/ideviceinstaller.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.device.wda import (
    ICON_PATH,
    WDA_APP,
    WDA_DERIVED,
    WDA_REPO,
    WDA_STATE_FILE,
    XCTESTRUN,
    _find_xctestrun,
    _is_process_alive,
    _parse_ios_major_version,
    _rename_xctestrun,
    build_wda,
    clone_wda,
    customize_wda,
    discover_signing_identities,
    install_wda,
    read_wda_state,
    save_wda_state,
    setup_wda,
    start_driver,
    stop_driver,
)
from server.main import create_app
from server.models import DeviceInfo, DeviceState, DeviceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _physical_device(
    udid: str = "00008030-AABBCCDD",
    name: str = "iPhone 15 Pro",
    os_version: str = "iOS 17.4",
) -> DeviceInfo:
    return DeviceInfo(
        udid=udid,
        name=name,
        state=DeviceState.BOOTED,
        device_type=DeviceType.DEVICE,
        os_version=os_version,
        connection_type="usb",
        is_connected=True,
    )


def _simulator(
    udid: str = "AAAA-1111",
    name: str = "iPhone 16 Pro",
) -> DeviceInfo:
    return DeviceInfo(
        udid=udid,
        name=name,
        state=DeviceState.BOOTED,
        device_type=DeviceType.SIMULATOR,
        os_version="iOS 18.6",
    )


def _mock_process(returncode=0, stdout=b"", stderr=b""):
    """Create a mock asyncio.Process.

    Uses MagicMock for the process itself (Process is not async) and
    AsyncMock only for the coroutine methods (communicate).
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


XCODE_PREFS_SINGLE_TEAM = {
    "IDEProvisioningTeamByIdentifier": {
        "acct-uuid-1": [
            {"teamID": "TEAM123", "teamName": "Acme Inc.", "teamType": "Company"},
        ],
    },
}

XCODE_PREFS_MULTI_TEAM = {
    "IDEProvisioningTeamByIdentifier": {
        "acct-uuid-1": [
            {"teamID": "TEAM123", "teamName": "Acme Inc.", "teamType": "Company"},
            {"teamID": "TEAMABC", "teamName": "John Doe (Personal Team)", "teamType": "Personal Team"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Unit tests: discover_signing_identities
# ---------------------------------------------------------------------------


class TestDiscoverSigningIdentities:
    def test_single_team(self, tmp_path):
        import plistlib
        plist_path = tmp_path / "com.apple.dt.Xcode.plist"
        with open(plist_path, "wb") as f:
            plistlib.dump(XCODE_PREFS_SINGLE_TEAM, f)

        with patch("server.device.wda.Path.home", return_value=tmp_path / "fake_home"):
            # We need to patch the actual plist path
            fake_prefs = tmp_path / "com.apple.dt.Xcode.plist"
            with patch("server.device.wda.Path.home") as mock_home:
                # Build the path so home() / "Library" / ... resolves to our file
                mock_home.return_value = tmp_path
                # But our file is at tmp_path/com.apple.dt.Xcode.plist, not
                # tmp_path/Library/Preferences/...  So just patch at the open level.
                pass

        # Simpler: just patch plistlib.load to return our test data
        import plistlib as _plistlib
        with (
            patch("builtins.open", create=True),
            patch("server.device.wda.Path.exists", return_value=True),
            patch("server.device.wda.plistlib.load", return_value=XCODE_PREFS_SINGLE_TEAM),
        ):
            ids = discover_signing_identities()

        assert len(ids) == 1
        assert ids[0]["team_id"] == "TEAM123"
        assert ids[0]["team_name"] == "Acme Inc."

    def test_multiple_teams(self):
        with (
            patch("builtins.open", create=True),
            patch("server.device.wda.Path.exists", return_value=True),
            patch("server.device.wda.plistlib.load", return_value=XCODE_PREFS_MULTI_TEAM),
        ):
            ids = discover_signing_identities()

        assert len(ids) == 2
        assert ids[0]["team_id"] == "TEAM123"
        assert ids[1]["team_id"] == "TEAMABC"

    def test_no_xcode_prefs(self, tmp_path):
        with patch("server.device.wda.Path.home", return_value=tmp_path):
            ids = discover_signing_identities()

        assert ids == []

    def test_empty_prefs(self):
        with (
            patch("builtins.open", create=True),
            patch("server.device.wda.Path.exists", return_value=True),
            patch("server.device.wda.plistlib.load", return_value={}),
        ):
            ids = discover_signing_identities()

        assert ids == []


# ---------------------------------------------------------------------------
# Unit tests: clone_wda
# ---------------------------------------------------------------------------


class TestCloneWda:
    async def test_skips_if_already_cloned(self, tmp_path):
        repo = tmp_path / "WebDriverAgent"
        repo.mkdir()
        (repo / ".git").mkdir()

        with patch("server.device.wda.WDA_REPO", repo):
            result = await clone_wda()

        assert result is False

    async def test_clones_fresh(self, tmp_path):
        repo = tmp_path / "WebDriverAgent"
        proc = _mock_process()
        with (
            patch("server.device.wda.WDA_REPO", repo),
            patch("server.device.wda.WDA_DIR", tmp_path),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc) as mock_exec,
        ):
            result = await clone_wda()

        assert result is True
        mock_exec.assert_called_once()

    async def test_clone_failure(self, tmp_path):
        repo = tmp_path / "WebDriverAgent"
        proc = _mock_process(returncode=128, stderr=b"fatal: could not connect")
        with (
            patch("server.device.wda.WDA_REPO", repo),
            patch("server.device.wda.WDA_DIR", tmp_path),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="git clone failed"):
                await clone_wda()

    async def test_clone_timeout(self, tmp_path):
        import asyncio

        repo = tmp_path / "WebDriverAgent"

        async def _hang_forever():
            await asyncio.sleep(999)
            return (b"", b"")

        proc = MagicMock()
        proc.communicate = _hang_forever
        with (
            patch("server.device.wda.WDA_REPO", repo),
            patch("server.device.wda.WDA_DIR", tmp_path),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc),
            patch("server.device.wda.CLONE_TIMEOUT", 0.001),
        ):
            with pytest.raises(RuntimeError, match="git clone timed out"):
                await clone_wda()


# ---------------------------------------------------------------------------
# Unit tests: customize_wda
# ---------------------------------------------------------------------------


# Minimal project.pbxproj that matches the upstream WDA structure
_MINIMAL_PBXPROJ = """\
// !$*UTF8*$!
{
\tarchiveVersion = 1;
\tobjects = {

/* Begin PBXBuildFile section */
\t\tEE9AB8001CAEE048008C271F /* UITestingUITests.m in Sources */ = {isa = PBXBuildFile; fileRef = EE9AB7FD1CAEE048008C271F /* UITestingUITests.m */; };
/* End PBXBuildFile section */

/* Begin PBXFileReference section */
\t\tEE9AB7FC1CAEE048008C271F /* Info.plist */ = {isa = PBXFileReference; fileEncoding = 4; lastKnownFileType = text.plist.xml; name = Info.plist; path = WebDriverAgentRunner/Info.plist; sourceTree = SOURCE_ROOT; };
\t\tEE9AB7FD1CAEE048008C271F /* UITestingUITests.m */ = {isa = PBXFileReference; fileEncoding = 4; lastKnownFileType = sourcecode.c.objc; name = UITestingUITests.m; path = WebDriverAgentRunner/UITestingUITests.m; sourceTree = SOURCE_ROOT; };
/* End PBXFileReference section */

/* Begin PBXResourcesBuildPhase section */
\t\tEEF988281C486603005CA669 /* Resources */ = {
\t\t\tisa = PBXResourcesBuildPhase;
\t\t\tbuildActionMask = 2147483647;
\t\t\tfiles = (
\t\t\t);
\t\t\trunOnlyForDeploymentPostprocessing = 0;
\t\t};
/* End PBXResourcesBuildPhase section */

/* Begin PBXGroup section */
\t\tEEF988341C486655005CA669 /* WebDriverAgentRunner */ = {
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
\t\t\t\tEE9AB7FC1CAEE048008C271F /* Info.plist */,
\t\t\t\tEE9AB7FD1CAEE048008C271F /* UITestingUITests.m */,
\t\t\t);
\t\t\tpath = WebDriverAgentRunner;
\t\t\tsourceTree = "<group>";
\t\t};
/* End PBXGroup section */

/* Begin XCBuildConfiguration section */
\t\tEEF988321C486604005CA669 /* Debug */ = {
\t\t\tisa = XCBuildConfiguration;
\t\t\tbuildSettings = {
\t\t\t\tINFOPLIST_FILE = WebDriverAgentRunner/Info.plist;
\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;
\t\t\t};
\t\t\tname = Debug;
\t\t};
\t\tEEF988331C486604005CA669 /* Release */ = {
\t\t\tisa = XCBuildConfiguration;
\t\t\tbuildSettings = {
\t\t\t\tINFOPLIST_FILE = WebDriverAgentRunner/Info.plist;
\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;
\t\t\t};
\t\t\tname = Release;
\t\t};
/* End XCBuildConfiguration section */

\t};
}
"""


def _setup_wda_repo(tmp_path: Path) -> Path:
    """Create a minimal WDA repo structure for testing customize_wda.

    Includes upstream-like AppIcon assets in WebDriverAgentLib and PrivateHeaders,
    mimicking the real appium/WebDriverAgent layout.
    """
    repo = tmp_path / "WebDriverAgent"
    xcodeproj = repo / "WebDriverAgent.xcodeproj"
    xcodeproj.mkdir(parents=True)
    (xcodeproj / "project.pbxproj").write_text(_MINIMAL_PBXPROJ)

    # Create upstream-like asset catalogs with placeholder icons
    for subdir in ("WebDriverAgentLib", "PrivateHeaders"):
        appiconset = repo / subdir / "Assets.xcassets" / "AppIcon.appiconset"
        appiconset.mkdir(parents=True)
        (appiconset / "AppIcon-1024.png").write_bytes(b"UPSTREAM_ICON_DATA")

    return repo


class TestCustomizeWda:
    def test_replaces_upstream_icons(self, tmp_path):
        repo = _setup_wda_repo(tmp_path)
        result = customize_wda(repo)
        assert result is True

        # Both upstream icon files should now contain our icon (not the placeholder)
        for subdir in ("WebDriverAgentLib", "PrivateHeaders"):
            icon = repo / subdir / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon-1024.png"
            assert icon.exists()
            assert icon.read_bytes() != b"UPSTREAM_ICON_DATA"

    def test_patches_build_settings(self, tmp_path):
        repo = _setup_wda_repo(tmp_path)
        customize_wda(repo)

        content = (repo / "WebDriverAgent.xcodeproj" / "project.pbxproj").read_text()
        assert content.count("PRODUCT_NAME = QuernDriver") == 2

    def test_idempotent(self, tmp_path):
        repo = _setup_wda_repo(tmp_path)

        assert customize_wda(repo) is True
        assert customize_wda(repo) is False

        # PRODUCT_NAME should appear exactly twice (Debug + Release)
        content = (repo / "WebDriverAgent.xcodeproj" / "project.pbxproj").read_text()
        assert content.count("PRODUCT_NAME = QuernDriver") == 2

    def test_icon_file_exists_in_resources(self):
        """The wda-icon.png must be checked into the repo."""
        assert ICON_PATH.exists(), f"Icon not found at {ICON_PATH}"


# ---------------------------------------------------------------------------
# Unit tests: build_wda
# ---------------------------------------------------------------------------


class TestBuildWda:
    async def test_skips_if_already_built(self, tmp_path):
        state = {
            "cloned": True,
            "build_team_id": "TEAM123",
            "built_at": "2026-01-01T00:00:00+00:00",
        }
        with patch("server.device.wda.read_wda_state", return_value=state):
            result = await build_wda("TEAM123")

        assert result is False

    async def test_builds_fresh(self, tmp_path):
        repo = tmp_path / "WebDriverAgent"
        repo.mkdir()
        (repo / "WebDriverAgent.xcodeproj").mkdir()

        proc = _mock_process()
        with (
            patch("server.device.wda.read_wda_state", return_value={"cloned": True}),
            patch("server.device.wda.save_wda_state") as mock_save,
            patch("server.device.wda.WDA_REPO", repo),
            patch("server.device.wda.WDA_DERIVED", tmp_path / "build"),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc),
        ):
            result = await build_wda("TEAM123")

        assert result is True
        mock_save.assert_called_once()

    async def test_rebuilds_if_different_team(self, tmp_path):
        state = {
            "cloned": True,
            "build_team_id": "OLD_TEAM",
            "built_at": "2026-01-01T00:00:00+00:00",
        }
        repo = tmp_path / "WebDriverAgent"
        repo.mkdir()

        proc = _mock_process()
        with (
            patch("server.device.wda.read_wda_state", return_value=state),
            patch("server.device.wda.save_wda_state"),
            patch("server.device.wda.WDA_REPO", repo),
            patch("server.device.wda.WDA_DERIVED", tmp_path / "build"),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc),
        ):
            result = await build_wda("NEW_TEAM")

        assert result is True

    async def test_build_failure(self, tmp_path):
        repo = tmp_path / "WebDriverAgent"
        repo.mkdir()

        proc = _mock_process(returncode=65, stdout=b"BUILD FAILED\n", stderr=b"signing error")
        with (
            patch("server.device.wda.read_wda_state", return_value={"cloned": True}),
            patch("server.device.wda.WDA_REPO", repo),
            patch("server.device.wda.WDA_DERIVED", tmp_path / "build"),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="xcodebuild failed"):
                await build_wda("TEAM123")


# ---------------------------------------------------------------------------
# Unit tests: install_wda
# ---------------------------------------------------------------------------


class TestInstallWda:
    async def test_install_ios17_uses_devicectl(self, tmp_path):
        app = tmp_path / "WDA.app"
        app.mkdir()

        proc = _mock_process()
        with (
            patch("server.device.wda.WDA_APP", app),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc) as mock_exec,
            patch("server.device.wda.read_wda_state", return_value={}),
            patch("server.device.wda.save_wda_state"),
        ):
            await install_wda("DEV1", "iOS 17.4")

        # Should use devicectl
        args = mock_exec.call_args[0]
        assert "devicectl" in args

    async def test_install_ios16_uses_ideviceinstaller(self, tmp_path):
        app = tmp_path / "WDA.app"
        app.mkdir()

        proc = _mock_process()
        with (
            patch("server.device.wda.WDA_APP", app),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc) as mock_exec,
            patch("server.device.wda.read_wda_state", return_value={}),
            patch("server.device.wda.save_wda_state"),
        ):
            await install_wda("DEV1", "iOS 16.7")

        args = mock_exec.call_args[0]
        assert "ideviceinstaller" in args

    async def test_install_ios15_uses_ideviceinstaller(self, tmp_path):
        app = tmp_path / "WDA.app"
        app.mkdir()

        proc = _mock_process()
        with (
            patch("server.device.wda.WDA_APP", app),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc) as mock_exec,
            patch("server.device.wda.read_wda_state", return_value={}),
            patch("server.device.wda.save_wda_state"),
        ):
            await install_wda("DEV1", "iOS 15.8.6")

        args = mock_exec.call_args[0]
        assert "ideviceinstaller" in args

    async def test_install_failure(self, tmp_path):
        app = tmp_path / "WDA.app"
        app.mkdir()

        proc = _mock_process(returncode=1, stderr=b"install failed")
        with (
            patch("server.device.wda.WDA_APP", app),
            patch("server.device.wda.asyncio.create_subprocess_exec", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="install failed"):
                await install_wda("DEV1", "iOS 17.0")

    async def test_install_no_app_file(self):
        fake_path = Path("/nonexistent/WDA.app")
        with patch("server.device.wda.WDA_APP", fake_path):
            with pytest.raises(RuntimeError, match="WDA app not found"):
                await install_wda("DEV1", "iOS 17.0")


# ---------------------------------------------------------------------------
# Unit tests: _parse_ios_major_version
# ---------------------------------------------------------------------------


class TestParseIosMajorVersion:
    def test_ios_17(self):
        assert _parse_ios_major_version("iOS 17.4") == 17

    def test_ios_15_patch(self):
        assert _parse_ios_major_version("iOS 15.8.6") == 15

    def test_ios_26(self):
        assert _parse_ios_major_version("iOS 26.3") == 26

    def test_invalid(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_ios_major_version("unknown")


# ---------------------------------------------------------------------------
# Unit tests: state persistence
# ---------------------------------------------------------------------------


class TestWdaState:
    def test_read_empty(self, tmp_path):
        state_file = tmp_path / "wda-state.json"
        with patch("server.device.wda.WDA_STATE_FILE", state_file):
            state = read_wda_state()
        assert state == {"cloned": False, "builds": {}}

    def test_roundtrip(self, tmp_path):
        state_file = tmp_path / "wda-state.json"
        with patch("server.device.wda.WDA_STATE_FILE", state_file), \
             patch("server.device.wda.CONFIG_DIR", tmp_path):
            save_wda_state({"cloned": True, "builds": {"DEV1": {"team_id": "T"}}})
            state = read_wda_state()
        assert state["cloned"] is True
        assert state["builds"]["DEV1"]["team_id"] == "T"

    def test_read_corrupt_json(self, tmp_path):
        state_file = tmp_path / "wda-state.json"
        state_file.write_text("not valid json{{{")
        with patch("server.device.wda.WDA_STATE_FILE", state_file):
            state = read_wda_state()
        assert state == {"cloned": False, "builds": {}}


# ---------------------------------------------------------------------------
# Unit tests: setup_wda orchestrator
# ---------------------------------------------------------------------------


class TestSetupWda:
    async def test_no_identities(self):
        with patch("server.device.wda.discover_signing_identities", return_value=[]):
            result = await setup_wda("DEV1", "iOS 17.4")

        assert result["status"] == "error"
        assert "No provisioning teams" in result["error"]

    async def test_multiple_identities_no_team_id(self):
        identities = [
            {"team_id": "TEAM1", "team_name": "Acme", "team_type": "Company"},
            {"team_id": "TEAM2", "team_name": "Personal", "team_type": "Personal Team"},
        ]
        with patch("server.device.wda.discover_signing_identities", return_value=identities):
            result = await setup_wda("DEV1", "iOS 17.4")

        assert result["status"] == "needs_identity_selection"
        assert len(result["identities"]) == 2

    async def test_single_identity_auto_selects(self):
        identities = [{"team_id": "TEAM1", "team_name": "Acme", "team_type": "Company"}]
        with (
            patch("server.device.wda.discover_signing_identities", return_value=identities),
            patch("server.device.wda.clone_wda", return_value=False),
            patch("server.device.wda.read_wda_state", return_value={"cloned": False}),
            patch("server.device.wda.save_wda_state"),
            patch("server.device.wda.customize_wda", return_value=False),
            patch("server.device.wda.build_wda", return_value=False),
            patch("server.device.wda.install_wda", return_value=None),
        ):
            result = await setup_wda("DEV1", "iOS 17.4")

        assert result["status"] == "ok"
        assert result["team_id"] == "TEAM1"

    async def test_explicit_team_id(self):
        identities = [
            {"team_id": "TEAM1", "team_name": "Acme", "team_type": "Company"},
            {"team_id": "TEAM2", "team_name": "Personal", "team_type": "Personal Team"},
        ]
        with (
            patch("server.device.wda.discover_signing_identities", return_value=identities),
            patch("server.device.wda.clone_wda", return_value=True),
            patch("server.device.wda.read_wda_state", return_value={"cloned": False}),
            patch("server.device.wda.save_wda_state"),
            patch("server.device.wda.customize_wda", return_value=True),
            patch("server.device.wda.build_wda", return_value=True),
            patch("server.device.wda.install_wda", return_value=None),
        ):
            result = await setup_wda("DEV1", "iOS 17.4", team_id="TEAM2")

        assert result["status"] == "ok"
        assert result["team_id"] == "TEAM2"
        assert result["cloned"] is True
        assert result["built"] is True

    async def test_invalid_team_id(self):
        identities = [{"team_id": "TEAM1", "team_name": "Acme", "team_type": "Company"}]
        with patch("server.device.wda.discover_signing_identities", return_value=identities):
            result = await setup_wda("DEV1", "iOS 17.4", team_id="BOGUS")

        assert result["status"] == "error"
        assert "BOGUS" in result["error"]


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def mock_controller(app):
    ctrl = DeviceController()
    ctrl._active_udid = None
    ctrl.list_devices = AsyncMock(return_value=[
        _physical_device(),
        _simulator(),
    ])
    ctrl.check_tools = AsyncMock(return_value={"simctl": True, "idb": False})
    app.state.device_controller = ctrl
    return ctrl


class TestWdaApi:
    async def test_setup_wda_success(self, app, auth_headers, mock_controller):
        mock_result = {"status": "ok", "udid": "00008030-AABBCCDD", "team_id": "TEAM1"}
        with patch("server.device.wda.setup_wda", return_value=mock_result):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/device/wda/setup",
                    json={"udid": "00008030-AABBCCDD"},
                    headers=auth_headers,
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_setup_wda_simulator_rejected(self, app, auth_headers, mock_controller):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/device/wda/setup",
                json={"udid": "AAAA-1111"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
        assert "simulator" in resp.json()["detail"].lower()

    async def test_setup_wda_device_not_found(self, app, auth_headers, mock_controller):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/device/wda/setup",
                json={"udid": "nonexistent-udid"},
                headers=auth_headers,
            )

        assert resp.status_code == 404

    async def test_setup_wda_needs_identity_selection(self, app, auth_headers, mock_controller):
        mock_result = {
            "status": "needs_identity_selection",
            "identities": [
                {"hash": "A" * 40, "name": "Dev", "team_id": "TEAM1"},
                {"hash": "B" * 40, "name": "Dist", "team_id": "TEAM2"},
            ],
            "message": "Multiple signing identities found.",
        }
        with patch("server.device.wda.setup_wda", return_value=mock_result):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/device/wda/setup",
                    json={"udid": "00008030-AABBCCDD"},
                    headers=auth_headers,
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_identity_selection"
        assert len(data["identities"]) == 2

    async def test_setup_wda_with_team_id(self, app, auth_headers, mock_controller):
        mock_result = {"status": "ok", "udid": "00008030-AABBCCDD", "team_id": "TEAM2"}
        with patch("server.device.wda.setup_wda", return_value=mock_result):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/device/wda/setup",
                    json={"udid": "00008030-AABBCCDD", "team_id": "TEAM2"},
                    headers=auth_headers,
                )

        assert resp.status_code == 200
        assert resp.json()["team_id"] == "TEAM2"

    async def test_setup_wda_runtime_error(self, app, auth_headers, mock_controller):
        with patch("server.device.wda.setup_wda", side_effect=RuntimeError("xcodebuild exploded")):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/device/wda/setup",
                    json={"udid": "00008030-AABBCCDD"},
                    headers=auth_headers,
                )

        assert resp.status_code == 500
        assert "xcodebuild exploded" in resp.json()["detail"]

    async def test_setup_wda_no_controller(self, app, auth_headers):
        """Should return 503 when device controller isn't initialized."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/device/wda/setup",
                json={"udid": "00008030-AABBCCDD"},
                headers=auth_headers,
            )

        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Unit tests: _rename_xctestrun / _find_xctestrun
# ---------------------------------------------------------------------------


class TestXctestrunRename:
    def test_rename_xctestrun(self, tmp_path):
        products = tmp_path / "Build" / "Products"
        products.mkdir(parents=True)
        original = products / "WebDriverAgentRunner_iphonesimulator17.4-arm64.xctestrun"
        original.write_text("test")

        with patch("server.device.wda.WDA_DERIVED", tmp_path):
            _rename_xctestrun()

        assert not original.exists()
        assert (products / "quern-driver.xctestrun").exists()
        assert (products / "quern-driver.xctestrun").read_text() == "test"

    def test_rename_skips_if_already_named(self, tmp_path):
        products = tmp_path / "Build" / "Products"
        products.mkdir(parents=True)
        stable = products / "quern-driver.xctestrun"
        stable.write_text("test")

        with patch("server.device.wda.WDA_DERIVED", tmp_path):
            _rename_xctestrun()

        assert stable.exists()

    def test_rename_noop_if_no_products_dir(self, tmp_path):
        with patch("server.device.wda.WDA_DERIVED", tmp_path):
            _rename_xctestrun()  # Should not raise

    def test_find_xctestrun_stable_name(self, tmp_path):
        products = tmp_path / "Build" / "Products"
        products.mkdir(parents=True)
        stable = products / "quern-driver.xctestrun"
        stable.write_text("test")

        with patch("server.device.wda.XCTESTRUN", stable):
            result = _find_xctestrun()
        assert result == stable

    def test_find_xctestrun_fallback_glob(self, tmp_path):
        products = tmp_path / "Build" / "Products"
        products.mkdir(parents=True)
        other = products / "SomeOtherName.xctestrun"
        other.write_text("test")
        fake_stable = tmp_path / "nonexistent" / "quern-driver.xctestrun"

        with (
            patch("server.device.wda.XCTESTRUN", fake_stable),
            patch("server.device.wda.WDA_DERIVED", tmp_path),
        ):
            result = _find_xctestrun()
        assert result == other

    def test_find_xctestrun_not_found(self, tmp_path):
        fake_stable = tmp_path / "nonexistent" / "quern-driver.xctestrun"
        with (
            patch("server.device.wda.XCTESTRUN", fake_stable),
            patch("server.device.wda.WDA_DERIVED", tmp_path),
        ):
            with pytest.raises(RuntimeError, match="No .xctestrun file found"):
                _find_xctestrun()


# ---------------------------------------------------------------------------
# Unit tests: start_driver / stop_driver
# ---------------------------------------------------------------------------


class TestStartDriver:
    async def test_start_driver_spawns_xcodebuild(self, tmp_path):
        products = tmp_path / "Build" / "Products"
        products.mkdir(parents=True)
        xctestrun = products / "quern-driver.xctestrun"
        xctestrun.write_text("test")

        proc = MagicMock()
        proc.pid = 42

        with (
            patch("server.device.wda.XCTESTRUN", xctestrun),
            patch("server.device.wda.WDA_DERIVED", tmp_path),
            patch("server.device.wda.WDA_LOG_DIR", tmp_path / "logs"),
            patch("server.device.wda.read_wda_state", return_value={}),
            patch("server.device.wda.save_wda_state") as mock_save,
            patch("server.device.tunneld.resolve_tunnel_udid", new_callable=AsyncMock, return_value="hw-udid-123"),
            patch("server.device.tunneld.get_tunneld_devices", new_callable=AsyncMock, return_value={
                "hw-udid-123": [{"tunnel-address": "fd35::1"}]
            }),
            patch("server.device.wda.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
            patch("server.device.wda._poll_wda_status", new_callable=AsyncMock, return_value=True),
        ):
            result = await start_driver("DEV-UUID-123", "iOS 17.4")

        assert result["status"] == "started"
        assert result["pid"] == 42
        assert result["ready"] is True
        mock_save.assert_called()

    async def test_start_driver_already_running(self):
        state = {"runners": {"DEV1": {"pid": 999}}}
        with (
            patch("server.device.wda.read_wda_state", return_value=state),
            patch("server.device.wda._is_process_alive", return_value=True),
            patch("server.device.wda._find_xctestrun", return_value=Path("/fake")),
        ):
            result = await start_driver("DEV1", "iOS 17.4")

        assert result["status"] == "already_running"
        assert result["pid"] == 999

    async def test_start_driver_cleans_stale_pid(self, tmp_path):
        products = tmp_path / "Build" / "Products"
        products.mkdir(parents=True)
        xctestrun = products / "quern-driver.xctestrun"
        xctestrun.write_text("test")

        proc = MagicMock()
        proc.pid = 100

        state = {"runners": {"DEV1": {"pid": 999}}}
        with (
            patch("server.device.wda.XCTESTRUN", xctestrun),
            patch("server.device.wda.WDA_DERIVED", tmp_path),
            patch("server.device.wda.WDA_LOG_DIR", tmp_path / "logs"),
            patch("server.device.wda.read_wda_state", return_value=state),
            patch("server.device.wda.save_wda_state"),
            patch("server.device.wda._is_process_alive", return_value=False),
            patch("server.device.tunneld.resolve_tunnel_udid", new_callable=AsyncMock, return_value=None),
            patch("server.device.tunneld.get_tunneld_devices", new_callable=AsyncMock, return_value={}),
            patch("server.device.wda.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
            patch("server.device.wda._poll_wda_status", new_callable=AsyncMock, return_value=True),
        ):
            result = await start_driver("DEV1", "iOS 17.4")

        assert result["status"] == "started"
        assert result["pid"] == 100


class TestStopDriver:
    async def test_stop_driver_not_running(self):
        with patch("server.device.wda.read_wda_state", return_value={}):
            result = await stop_driver("DEV1")

        assert result["status"] == "not_running"

    async def test_stop_driver_sigterm(self):
        state = {"runners": {"DEV1": {"pid": 42}}}
        with (
            patch("server.device.wda.read_wda_state", return_value=state),
            patch("server.device.wda.save_wda_state"),
            patch("server.device.wda._is_process_alive", side_effect=[True, False]),
            patch("server.device.wda.os.kill") as mock_kill,
        ):
            result = await stop_driver("DEV1")

        assert result["status"] == "stopped"
        mock_kill.assert_called_once_with(42, __import__("signal").SIGTERM)

    async def test_stop_driver_dead_pid(self):
        state = {"runners": {"DEV1": {"pid": 42}}}
        with (
            patch("server.device.wda.read_wda_state", return_value=state),
            patch("server.device.wda.save_wda_state"),
            patch("server.device.wda._is_process_alive", return_value=False),
        ):
            result = await stop_driver("DEV1")

        assert result["status"] == "not_running"


# ---------------------------------------------------------------------------
# API integration tests: /start and /stop
# ---------------------------------------------------------------------------


class TestWdaStartStopApi:
    async def test_start_driver_api(self, app, auth_headers, mock_controller):
        mock_result = {"status": "started", "udid": "00008030-AABBCCDD", "pid": 42, "ready": True}
        with patch("server.device.wda.start_driver", new_callable=AsyncMock, return_value=mock_result):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/device/wda/start",
                    json={"udid": "00008030-AABBCCDD"},
                    headers=auth_headers,
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    async def test_start_driver_simulator_rejected(self, app, auth_headers, mock_controller):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/device/wda/start",
                json={"udid": "AAAA-1111"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
        assert "simulator" in resp.json()["detail"].lower()

    async def test_start_driver_not_found(self, app, auth_headers, mock_controller):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/device/wda/start",
                json={"udid": "nonexistent"},
                headers=auth_headers,
            )

        assert resp.status_code == 404

    async def test_stop_driver_api(self, app, auth_headers, mock_controller):
        mock_result = {"status": "stopped", "udid": "00008030-AABBCCDD"}
        with patch("server.device.wda.stop_driver", new_callable=AsyncMock, return_value=mock_result):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/device/wda/stop",
                    json={"udid": "00008030-AABBCCDD"},
                    headers=auth_headers,
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

    async def test_stop_driver_simulator_rejected(self, app, auth_headers, mock_controller):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/device/wda/stop",
                json={"udid": "AAAA-1111"},
                headers=auth_headers,
            )

        assert resp.status_code == 400
