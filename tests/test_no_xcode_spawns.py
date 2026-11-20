"""Nothing a phone path reaches runs `xcrun` or `xcodebuild` with no Xcode (#738).

With no developer tools, `/usr/bin/xcrun` and `xcodebuild` are stubs that can
show macOS's "install Command Line Developer Tools" dialog, and most of these
sites did not remember a failure, so it came back on every call. Each is
refused before anything runs, and the refusal names Xcode on the answer: the
agent calling sees nothing else.
"""

from __future__ import annotations

import asyncio
import functools
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.ios import _xcode
from server.models import DeviceOperationUnsupportedError

NO_XCODE = functools.lru_cache(maxsize=1)(lambda: False)


@pytest.fixture
def no_spawns(monkeypatch):
    """Fails any process start: a gate that lets one through is the bug."""
    import subprocess

    started: list[list[str]] = []

    async def exec_(*argv, **kwargs):
        started.append([str(a) for a in argv])
        raise AssertionError(f"spawned {argv}")

    def run(argv, *a, **k):
        started.append([str(x) for x in argv])
        raise AssertionError(f"spawned {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(subprocess, "run", run)
    return started


class TestServerStart:
    def test_no_simctl_probe_without_developer_tools(self, monkeypatch, no_spawns):
        """It ran `xcrun simctl help` at every start, where setup's own copy
        of the same fix already skipped it."""
        import pathlib

        import server.main as m

        # Set first, so the undo is registered: delenv of an absent variable
        # records none, and a DEVELOPER_DIR the fix sets would outlive the
        # test (CodeRabbit on #740).
        monkeypatch.setenv("DEVELOPER_DIR", "")
        monkeypatch.delenv("DEVELOPER_DIR")
        monkeypatch.setattr(_xcode, "xcode_available", NO_XCODE)
        monkeypatch.setattr(pathlib.Path, "glob", lambda self, pattern: iter(()))
        # xcode-select itself never shows the dialog; let it answer.
        import subprocess

        def run(argv, *a, **k):
            if argv[0] == "xcode-select":
                return subprocess.CompletedProcess(argv, 2, "", "")
            no_spawns.append(list(argv))
            raise AssertionError(f"spawned {argv}")

        monkeypatch.setattr(subprocess, "run", run)

        assert m._fix_developer_dir() is None
        assert no_spawns == []

    def test_an_xcode_found_in_applications_clears_the_cached_answer(
            self, monkeypatch, tmp_path):
        """DEVELOPER_DIR set to an Xcode in /Applications: the cached "no
        Xcode" from before it would go on refusing everything (review)."""
        import os
        import pathlib
        import subprocess

        import server.main as m

        app = tmp_path / "Xcode.app"
        (app / "Contents" / "Developer").mkdir(parents=True)
        cleared = []
        answer = functools.lru_cache(maxsize=1)(lambda: False)
        monkeypatch.setattr(answer, "cache_clear", lambda: cleared.append(1))
        monkeypatch.setattr(_xcode, "xcode_available", answer)
        # Set first, so the undo is registered: delenv of an absent variable
        # records none, and a DEVELOPER_DIR the fix sets would outlive the
        # test (CodeRabbit on #740).
        monkeypatch.setenv("DEVELOPER_DIR", "")
        monkeypatch.delenv("DEVELOPER_DIR")
        monkeypatch.setattr(pathlib.Path, "glob", lambda self, pattern: iter([app]))
        ran = []

        def run(argv, *a, **k):
            ran.append((argv[0], os.environ.get("DEVELOPER_DIR")))
            return subprocess.CompletedProcess(argv, 0 if argv[0] == "xcrun" else 2, "", "")

        monkeypatch.setattr(subprocess, "run", run)

        assert "Using" in (m._fix_developer_dir() or "")
        assert os.environ["DEVELOPER_DIR"] == str(app / "Contents" / "Developer")
        assert cleared, "the cached answer from before DEVELOPER_DIR was kept"
        # xcrun only ever ran with a developer directory named: no dialog.
        assert all(dev for tool, dev in ran if tool == "xcrun")


class TestTheRunners:
    async def test_devicectl_names_xcode_and_runs_nothing(self, monkeypatch, no_spawns):
        from server.device.ios import devicectl

        monkeypatch.setattr(_xcode, "xcode_available", NO_XCODE)
        with pytest.raises(DeviceOperationUnsupportedError, match="needs Xcode") as caught:
            await devicectl.DevicectlBackend()._run_devicectl("device", "info", "details")
        assert "devicectl device info details" in str(caught.value), "the operation unnamed"
        assert no_spawns == []

    async def test_a_launch_is_not_told_to_sign_the_app(self, monkeypatch, no_spawns):
        """Wrapped, the refusal said "Physical devices require the app to be
        signed and installed", which is not why (review)."""
        from server.device.ios import devicectl

        monkeypatch.setattr(_xcode, "xcode_available", NO_XCODE)
        with pytest.raises(DeviceOperationUnsupportedError) as caught:
            await devicectl.DevicectlBackend().launch_app("UUID", "com.example.app")
        assert "needs Xcode" in str(caught.value)
        assert "signed" not in str(caught.value)

    async def test_simctl_names_xcode_and_runs_nothing(self, monkeypatch, no_spawns):
        """A phone whose type was not known yet fell into the simulator branch."""
        from server.device.ios import simctl

        monkeypatch.setattr(_xcode, "xcode_available", NO_XCODE)
        backend = simctl.SimctlBackend()
        with pytest.raises(DeviceOperationUnsupportedError, match="needs Xcode"):
            await backend._run_simctl("io", "UDID", "screenshot", "/dev/null")
        with pytest.raises(DeviceOperationUnsupportedError, match="needs Xcode"):
            await backend._run_shell("xcrun simctl listapps UDID | plutil -convert json -o - -")
        assert no_spawns == []

    @pytest.mark.parametrize("call", [
        lambda b: b.wait_until_booted("UDID", timeout=1),
        lambda b: b.launch_app("UDID", "com.example.app"),
        lambda b: b.terminate_app("UDID", "com.example.app"),
        lambda b: b.is_installed("UDID", "com.example.app"),
    ], ids=["bootstatus", "launch", "terminate", "get_app_container"])
    async def test_simctls_own_spawns_are_gated_too(self, monkeypatch, no_spawns, call):
        """Four methods start `xcrun simctl` themselves, not through
        `_run_simctl`, and a phone not yet identified reached them (review)."""
        from server.device.ios import simctl

        monkeypatch.setattr(_xcode, "xcode_available", NO_XCODE)
        with pytest.raises(DeviceOperationUnsupportedError, match="needs Xcode"):
            await call(simctl.SimctlBackend())
        assert no_spawns == []


class TestSymbolication:
    async def test_atos_is_not_run_and_the_note_says_why(self, monkeypatch, no_spawns):
        """Once per OS image of every thread-stack read, where DeviceSupport
        outlived the Xcode that copied it."""
        from server.sources import symbolicate

        monkeypatch.setattr(symbolicate, "xcode_available", NO_XCODE)
        with pytest.raises(OSError, match="needs Xcode"):
            await symbolicate._run(["xcrun", "atos", "-o", "/x", "-l", "0x0", "0x10"])
        assert no_spawns == []

    async def test_other_tools_are_not_gated(self, monkeypatch):
        """Only `xcrun`: retrace is Android's, and has no Xcode to need."""
        from server.sources import symbolicate

        monkeypatch.setattr(symbolicate, "xcode_available", NO_XCODE)
        proc = MagicMock(returncode=0)
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
        assert await symbolicate._run(["/opt/retrace", "map"]) == (0, "ok", "")


class TestProfiling:
    async def test_an_ios_profile_is_refused_before_anything_runs(
            self, monkeypatch, no_spawns, tmp_path):
        from server.profiling import manager as pm
        from server.profiling import xctrace as xt

        monkeypatch.setattr(pm, "xcode_available", NO_XCODE)
        warm_up = AsyncMock()
        m = pm.ProfileManager(settings=lambda: {"max_bytes": 2**31}, warm_up=warm_up)

        with pytest.raises(pm.ProfileError, match="needs Xcode") as caught:
            await m.start(udid="00008101-0000000000000001", platform="device",
                          device="00008101-0000000000000001", bundle_id="com.example.app",
                          preset=next(iter(xt.PRESETS)), time_limit_s=10)

        assert caught.value.status == 503
        warm_up.assert_not_called()
        assert m._active == {}, "the refused start kept the device"
        assert no_spawns == []


    async def test_a_resumed_profile_is_not_read_and_says_why(self, monkeypatch, no_spawns):
        """A profile recorded before Xcode went is summarised when the server
        starts again: `xctrace export` there showed the dialog (CodeRabbit)."""
        from server.profiling import manager as pm
        from server.profiling import xctrace as xt

        monkeypatch.setattr(pm, "xcode_available", NO_XCODE)
        m = pm.ProfileManager(settings=lambda: {"max_bytes": 2**31})
        profile = pm.Profile(id="prof_000000000001", udid="00008101-0000000000000001",
                             device="00008101-0000000000000001", platform="device",
                             bundle_id="com.example.app", preset=next(iter(xt.PRESETS)),
                             time_limit_s=10, started_at="2026-11-20T00:00:00+00:00")

        summary = await m.summarize(profile)

        assert "needs Xcode" in summary["unavailable"]["trace"]
        assert no_spawns == []


class TestWdaRoutes:
    """`start_driver` and `setup_wda` on a phone ran `xcodebuild` whenever a
    build or a signing profile was left from an earlier Xcode."""

    @pytest.fixture
    async def post(self, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        import server.main as m
        from server.config import ServerConfig
        from server.device.ios import wda
        from server.models import DeviceInfo, DeviceState, DeviceType
        from server.tooling import wda_availability

        app = m.create_app(ServerConfig(api_key="k" * 32))
        phone = DeviceInfo(udid="00008101-0000000000000001", name="iPhone",
                           state=DeviceState.BOOTED, device_type=DeviceType.DEVICE,
                           os_version="18.5", is_available=True)
        controller = MagicMock()
        controller.list_devices = AsyncMock(return_value=[phone])
        app.state.device_controller = controller
        monkeypatch.setattr(wda_availability, "wda_unavailable_reason",
                            AsyncMock(return_value=wda_availability.NO_XCODE))
        # Answers that serialise, so a missing gate fails on the call, not
        # on encoding a mock (review).
        self.start_driver = AsyncMock(return_value={"status": "ok"})
        self.setup_wda = AsyncMock(return_value={"status": "ok"})
        # The work behind the gate, so the gate tested is the real one.
        monkeypatch.setattr(wda, "_setup_wda", self.setup_wda)
        monkeypatch.setattr(wda, "_start_driver", self.start_driver)

        async def post(path: str):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.post(path, json={"udid": phone.udid},
                                    headers={"Authorization": "Bearer " + "k" * 32})
        return post

    async def test_start_driver_is_refused_naming_xcode(self, post):
        r = await post("/api/v1/device/wda/start")
        assert r.status_code == 400, r.text
        assert "Xcode is not installed" in r.json()["detail"]
        self.start_driver.assert_not_called()

    async def test_setup_wda_is_refused_naming_xcode(self, post):
        r = await post("/api/v1/device/wda/setup")
        assert r.status_code == 400, r.text
        assert "Xcode is not installed" in r.json()["detail"]
        self.setup_wda.assert_not_called()


class TestBuildAndInstall:
    """It ran `xcodebuild -list -json`, then the build, for any Xcode project:
    on a clean VM the install dialog opened (#738 review)."""

    @pytest.mark.parametrize("scheme", [None, "Internal"], ids=["listing schemes", "building"])
    async def test_an_xcode_project_is_refused_naming_xcode(
            self, monkeypatch, tmp_path, no_spawns, scheme):
        from httpx import ASGITransport, AsyncClient

        from server.api import build_app as route
        from server.config import ServerConfig
        from server.main import create_app
        from tests.test_build_records import HEADERS, FakeController

        monkeypatch.setattr(_xcode, "xcode_available", NO_XCODE)
        build = AsyncMock()
        monkeypatch.setattr(route, "_build", build)
        app = create_app(config=ServerConfig(api_key="test-key-12345"),
                         enable_oslog=False, enable_crash=False, enable_proxy=False)
        app.state.build_adapter = object()
        app.state.device_controller = FakeController({"SIM-UDID": "simulator"})
        project = tmp_path / "MyApp.xcodeproj"
        project.mkdir()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/device/build-and-install", headers=HEADERS,
                             json={"project_path": str(project), "scheme": scheme,
                                   "udids": ["SIM-UDID"]})

        assert r.status_code == 400, r.text
        assert "needs Xcode" in r.json()["detail"]
        build.assert_not_called()
        assert no_spawns == []
