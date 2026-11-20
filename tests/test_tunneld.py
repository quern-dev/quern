"""Tests for tunneld module — binary discovery, health check, plist generation, CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import server.device.ios.tunneld as tunneld_module
from server.device.ios.tunneld import (
    LAUNCHCTL,
    LOG_PATH,
    TUNNELD_LABEL,
    _restart_daemon,
    _run_sudo,
    _tunnel_udid_cache,
    can_recover_unattended,
    cli_tunneld,
    find_pymobiledevice3_binary,
    generate_plist,
    get_tunneld_devices,
    install_daemon,
    install_recovery_grant,
    installed_plist_is_current,
    installed_plist_log_path,
    is_tunneld_running,
    recover_wedged_tunneld,
    recovery_grant_line,
    resolve_tunnel_udid,
    tunneld_health,
)

# ---------------------------------------------------------------------------
# find_pymobiledevice3_binary
# ---------------------------------------------------------------------------


class TestFindBinary:
    # `excluded_roots=[]` says these are about the lookup, not the exclusion.
    # They patch `Path.resolve` to a single fixed value, so without it the
    # shadowing roots resolve to the same path as the candidate and everything
    # is excluded -- the blanket patch colliding with a later feature, not a
    # real behaviour. The exclusion has its own tests.
    def test_found_on_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/pymobiledevice3"):
            with patch.object(Path, "resolve", return_value=Path("/usr/local/bin/pymobiledevice3")):
                result = find_pymobiledevice3_binary(excluded_roots=[])
                assert result == Path("/usr/local/bin/pymobiledevice3")

    def test_found_in_pipx(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=True):
                with patch.object(Path, "resolve", return_value=Path("/resolved/pymobiledevice3")):
                    result = find_pymobiledevice3_binary(excluded_roots=[])
                    assert result == Path("/resolved/pymobiledevice3")

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = find_pymobiledevice3_binary()
                assert result is None


# ---------------------------------------------------------------------------
# is_tunneld_running
# ---------------------------------------------------------------------------


class TestIsTunneldRunning:
    async def test_running(self):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("server.device.ios.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await is_tunneld_running() is True

    async def test_not_running(self):
        with patch("server.device.ios.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await is_tunneld_running() is False


# ---------------------------------------------------------------------------
# get_tunneld_devices
# ---------------------------------------------------------------------------


class TestGetTunneldDevices:
    async def test_returns_devices(self):
        devices = {
            "00008130-AAAA": [{"tunnel-address": "fd35::1", "tunnel-port": 61952}],
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = devices

        with patch("server.device.ios.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_tunneld_devices()
            assert result == devices
            assert "00008130-AAAA" in result

    async def test_connection_error_returns_empty(self):
        with patch("server.device.ios.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_tunneld_devices()
            assert result == {}


# ---------------------------------------------------------------------------
# resolve_tunnel_udid
# ---------------------------------------------------------------------------


class TestResolveTunnelUdid:
    def setup_method(self):
        _tunnel_udid_cache.clear()

    async def test_cache_hit(self):
        _tunnel_udid_cache["53DA57AA-1234"] = "00008130-AAAA"
        result = await resolve_tunnel_udid("53DA57AA-1234")
        assert result == "00008130-AAAA"

    async def test_single_tunnel_maps_via_devicectl(self):
        """With one tunneled device, devicectl JSON maps the CoreDevice UUID."""
        devices = {"00008130-AAAA": [{"tunnel-address": "fd35::1"}]}
        devicectl_output = {
            "result": {
                "devices": [
                    {
                        "identifier": "53DA57AA-1234",
                        "hardwareProperties": {"udid": "00008130-AAAA"},
                    },
                ]
            }
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def fake_subprocess(*args, **kwargs):
            json_path = args[-1]
            Path(json_path).write_text(json.dumps(devicectl_output))
            return mock_proc

        with (
            patch("server.device.ios.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1234")
            assert result == "00008130-AAAA"
            assert _tunnel_udid_cache["53DA57AA-1234"] == "00008130-AAAA"

    async def test_not_found(self):
        with patch("server.device.ios.tunneld.get_tunneld_devices", return_value={}):
            result = await resolve_tunnel_udid("UNKNOWN-UUID")
            assert result is None

    async def test_multiple_tunnels_maps_via_devicectl(self, tmp_path):
        """With multiple tunneled devices, devicectl JSON maps CoreDevice UUIDs."""
        devices = {
            "00008130-AAAA1111": [{"tunnel-address": "fd35::1"}],
            "00008130-BBBB2222": [{"tunnel-address": "fd35::2"}],
        }
        devicectl_output = {
            "result": {
                "devices": [
                    {
                        "identifier": "53DA57AA-1111",
                        "hardwareProperties": {"udid": "00008130-AAAA1111"},
                    },
                    {
                        "identifier": "53DA57AA-2222",
                        "hardwareProperties": {"udid": "00008130-BBBB2222"},
                    },
                ]
            }
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def fake_subprocess(*args, **kwargs):
            # Write devicectl JSON to the temp file (second-to-last arg)
            json_path = args[-1]
            Path(json_path).write_text(json.dumps(devicectl_output))
            return mock_proc

        with (
            patch("server.device.ios.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1111")
            assert result == "00008130-AAAA1111"
            assert _tunnel_udid_cache["53DA57AA-1111"] == "00008130-AAAA1111"
            assert _tunnel_udid_cache["53DA57AA-2222"] == "00008130-BBBB2222"

    async def test_multiple_tunnels_devicectl_fails_gracefully(self):
        """When devicectl fails with multiple tunnels, returns None."""
        devices = {
            "00008130-AAAA1111": [{"tunnel-address": "fd35::1"}],
            "00008130-BBBB2222": [{"tunnel-address": "fd35::2"}],
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with (
            patch("server.device.ios.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1111")
            assert result is None



PHONES = {"00008130-AAAA": [{"tunnel-address": "fd35::1"}]}
LISTED = {"result": {"devices": [
    {"identifier": "53DA57AA-1234", "hardwareProperties": {"udid": "00008130-AAAA"}}]}}

#: Listings devicectl did not write, each of which must map nothing. The last
#: two are unhashable where a string is looked up, which raised TypeError out
#: of every caller (review on #737).
OTHER_SHAPES = [
    [],
    {"result": []},
    {"result": {"devices": {}}},
    {"result": {"devices": [7, {"identifier": "x", "hardwareProperties": []}]}},
    {"result": {"devices": [
        {"identifier": "53DA57AA-1234", "hardwareProperties": {"udid": ["x"]}}]}},
    {"result": {"devices": [
        {"identifier": ["53DA57AA-1234"], "hardwareProperties": {"udid": "00008130-AAAA"}}]}},
]

_FAKE_XCRUN = """#!{python}
import json, os, sys, time
# xcrun devicectl list devices --json-output <path>
with open(os.environ["FAKE_DEVICECTL_LOG"], "a") as log:
    log.write(f"{{os.getpid()}} {{sys.argv[5]}}\\n")
mode = os.environ["FAKE_DEVICECTL_MODE"]
if mode == "hang":
    time.sleep(30)          # then writes the file it was given, as devicectl would
if mode == "slow":
    time.sleep(0.6)         # slow, but answers
if mode == "gated":         # answers once the test says so, at most 10 s on
    gate = os.environ["FAKE_DEVICECTL_GATE"]
    for _ in range(400):
        if os.path.exists(gate):
            break
        time.sleep(0.025)
if mode == "fail":
    sys.stderr.write("devicectl: boom")
    sys.exit(1)
with open(sys.argv[5], "w") as out:
    out.write(os.environ.get("FAKE_DEVICECTL_JSON", "{{}}"))
"""


class TestDevicectlIsBounded:
    """`resolve_tunnel_udid`'s devicectl had no bound, outlived a caller that
    gave up, and was run again on every call after failing (#737). A real
    process stands in for it, so killed and reaped can be checked."""

    @pytest.fixture
    def devicectl(self, monkeypatch, tmp_path):
        import sys

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        xcrun = bin_dir / "xcrun"
        xcrun.write_text(_FAKE_XCRUN.format(python=sys.executable))
        xcrun.chmod(0o755)
        log = tmp_path / "devicectl.log"
        log.touch()
        monkeypatch.setattr(tunneld_module, "XCRUN", str(xcrun))
        monkeypatch.setenv("FAKE_DEVICECTL_LOG", str(log))
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "ok")
        monkeypatch.setenv("FAKE_DEVICECTL_JSON", json.dumps(LISTED))
        monkeypatch.setattr(tunneld_module, "get_tunneld_devices", AsyncMock(return_value=PHONES))
        _tunnel_udid_cache.clear()

        def runs() -> list[tuple[int, Path]]:
            return [(int(pid), Path(path)) for pid, path in
                    (line.split(" ", 1) for line in log.read_text().splitlines())]
        yield runs
        _tunnel_udid_cache.clear()

    @staticmethod
    def _gone(pid: int) -> bool:
        """Killed and reaped: a zombie still answers signal 0."""
        import os

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    @staticmethod
    async def _started(runs) -> tuple[int, Path]:
        for _ in range(200):
            if runs():
                return runs()[0]
            await asyncio.sleep(0.025)
        pytest.fail("devicectl never started")

    async def test_it_maps_a_phone(self, devicectl):
        assert await resolve_tunnel_udid("53DA57AA-1234") == "00008130-AAAA"
        (_, listing), = devicectl()
        assert not listing.exists(), "its JSON was left behind"
        assert not listing.parent.exists(), "its directory was left behind"

    @staticmethod
    async def _run_ended() -> None:
        for _ in range(400):
            if not tunneld_module._devicectl_runs:
                return
            await asyncio.sleep(0.025)
        pytest.fail("the devicectl run never ended")

    async def test_a_caller_that_gives_up_leaves_the_run_to_answer(
            self, monkeypatch, devicectl):
        """Killed with each caller that gave up, a slow devicectl never
        answered, and the next caller started another (CodeRabbit on #739)."""
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "slow")
        lookup = asyncio.create_task(resolve_tunnel_udid("53DA57AA-1234"))
        pid, listing = await self._started(devicectl)

        lookup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(lookup, 0.3)          # the caller is let go at once

        assert await asyncio.wait_for(resolve_tunnel_udid("53DA57AA-1234"), 5) == "00008130-AAAA"
        assert len(devicectl()) == 1, "a second devicectl was started beside the first"
        assert self._gone(pid)
        assert not listing.parent.exists()
        assert tunneld_module._devicectl_runs == {}, "the ended run was kept"

    async def test_callers_at_once_share_one_run(self, monkeypatch, devicectl):
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "slow")
        found = await asyncio.wait_for(asyncio.gather(
            *(resolve_tunnel_udid("53DA57AA-1234") for _ in range(3))), 5)
        assert found == ["00008130-AAAA"] * 3
        assert len(devicectl()) == 1

    async def test_a_caller_that_joins_brings_its_own_tunnels(
            self, monkeypatch, devicectl, tmp_path):
        """Joined with a phone that connected after the run began, it came back
        None: the run filtered by the first caller's tunnels (CodeRabbit on #739)."""
        gate = tmp_path / "release"
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "gated")
        monkeypatch.setenv("FAKE_DEVICECTL_GATE", str(gate))
        monkeypatch.setenv("FAKE_DEVICECTL_JSON", json.dumps({"result": {"devices": [
            {"identifier": "53DA57AA-1234", "hardwareProperties": {"udid": "00008130-AAAA"}},
            {"identifier": "53DA57AA-5678", "hardwareProperties": {"udid": "00008130-BBBB"}},
        ]}}))
        first = asyncio.create_task(resolve_tunnel_udid("53DA57AA-1234"))
        await self._started(devicectl)
        both = {**PHONES, "00008130-BBBB": [{"tunnel-address": "fd35::2"}]}
        monkeypatch.setattr(tunneld_module, "get_tunneld_devices", AsyncMock(return_value=both))

        later = asyncio.create_task(resolve_tunnel_udid("53DA57AA-5678"))
        # Released only once it has joined: a fixed sleep let a stalled runner
        # end the first run before the second caller arrived (CodeRabbit).
        for _ in range(200):
            entries = list(tunneld_module._devicectl_runs.values())
            if entries and "00008130-BBBB" in entries[0][1]:
                break
            await asyncio.sleep(0.025)
        else:
            pytest.fail("the second caller never joined the run")
        gate.touch()

        assert await asyncio.wait_for(later, 5) == "00008130-BBBB"
        assert await asyncio.wait_for(first, 5) == "00008130-AAAA"
        assert len(devicectl()) == 1, "it started a second run instead of joining"

    async def test_a_run_that_has_finished_is_not_joined(self, monkeypatch, devicectl):
        """Its entry outlives it until a callback runs; joined then, a new
        phone's tunnels were added after the filtering (CodeRabbit on #739)."""
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_result(None)
        tunneld_module._devicectl_runs[loop] = (done, set())

        assert await asyncio.wait_for(resolve_tunnel_udid("53DA57AA-1234"), 5) == "00008130-AAAA"
        assert len(devicectl()) == 1, "it joined a finished run instead of starting one"

    async def test_a_finished_run_leaves_a_newer_run_in_place(self):
        loop = asyncio.get_running_loop()
        old, new = loop.create_future(), loop.create_future()
        old.set_result(None)
        tunneld_module._devicectl_runs[loop] = (new, set())

        tunneld_module._devicectl_run_ended(old)

        assert tunneld_module._devicectl_runs[loop][0] is new
        new.cancel()

    async def test_a_hung_run_no_one_waits_for_is_killed_at_its_bound(
            self, monkeypatch, devicectl):
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "hang")
        monkeypatch.setattr(tunneld_module, "DEVICECTL_LIST_S", 1.0)

        async def bounded():
            async with asyncio.timeout(None) as deadline:
                lookup = asyncio.ensure_future(resolve_tunnel_udid("53DA57AA-1234"))
                await self._started(devicectl)
                # From here: the stand-in's start-up is not the caller's
                # deadline (CodeRabbit on #739).
                deadline.reschedule(asyncio.get_running_loop().time() + 0.2)
                await lookup

        with pytest.raises(TimeoutError):
            await bounded()
        await self._run_ended()

        (pid, listing), = devicectl()
        assert self._gone(pid), "the run outlived its own bound"
        assert not listing.parent.exists()
        assert await resolve_tunnel_udid("53DA57AA-1234") is None
        assert len(devicectl()) == 1, "a devicectl that had just hung was run again"

    async def test_a_hung_devicectl_is_bounded_and_not_run_again_at_once(
            self, monkeypatch, devicectl):
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "hang")
        monkeypatch.setattr(tunneld_module, "DEVICECTL_LIST_S", 2.0)

        lookup = asyncio.create_task(resolve_tunnel_udid("53DA57AA-1234"))
        pid, _ = await self._started(devicectl)
        assert await asyncio.wait_for(lookup, 10) is None
        assert self._gone(pid)

        assert await asyncio.wait_for(resolve_tunnel_udid("53DA57AA-1234"), 5) is None
        assert len(devicectl()) == 1, "a devicectl that had just hung was run again"

    async def test_a_failed_devicectl_is_asked_again_after_a_while(
            self, monkeypatch, devicectl):
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "fail")
        assert await resolve_tunnel_udid("53DA57AA-1234") is None

        monkeypatch.setattr(tunneld_module, "DEVICECTL_RETRY_S", 0.0)
        monkeypatch.setenv("FAKE_DEVICECTL_MODE", "ok")
        assert await resolve_tunnel_udid("53DA57AA-1234") == "00008130-AAAA"
        assert len(devicectl()) == 2

    async def test_a_list_without_the_phone_is_not_remembered(self, monkeypatch, devicectl):
        """A phone plugged in a moment later has to resolve."""
        monkeypatch.setenv("FAKE_DEVICECTL_JSON", json.dumps({"result": {"devices": []}}))
        assert await resolve_tunnel_udid("53DA57AA-1234") is None

        monkeypatch.setenv("FAKE_DEVICECTL_JSON", json.dumps(LISTED))
        assert await resolve_tunnel_udid("53DA57AA-1234") == "00008130-AAAA"
        assert len(devicectl()) == 2

    @pytest.mark.parametrize("listing", OTHER_SHAPES)
    async def test_a_listing_of_another_shape_maps_nothing(self, monkeypatch, devicectl, listing):
        monkeypatch.setenv("FAKE_DEVICECTL_JSON", json.dumps(listing))
        assert await resolve_tunnel_udid("53DA57AA-1234") is None

    async def test_a_temp_directory_it_cannot_make_is_a_failure_to_ask(
            self, monkeypatch, devicectl):
        """Not an OSError out of every caller (review)."""
        def unwritable(*a, **k):
            raise PermissionError("temp directory not writable")

        monkeypatch.setattr(tunneld_module.tempfile, "TemporaryDirectory", unwritable)
        assert await resolve_tunnel_udid("53DA57AA-1234") is None
        assert tunneld_module._devicectl_failed_at is not None, "not remembered"

    async def test_with_no_xcode_it_is_not_run(self, monkeypatch, devicectl):
        """`devicectl` comes with Xcode; without developer tools `xcrun` may
        ask to install them, on every call."""
        monkeypatch.setattr(tunneld_module, "xcode_available", lambda: False)
        assert await resolve_tunnel_udid("53DA57AA-1234") is None
        assert devicectl() == []

# ---------------------------------------------------------------------------
# generate_plist
# ---------------------------------------------------------------------------


class TestGeneratePlist:
    def test_contains_required_keys(self):
        plist = generate_plist(Path("/usr/bin/pymobiledevice3"))
        assert TUNNELD_LABEL in plist
        assert "/usr/bin/pymobiledevice3" in plist
        assert "<string>remote</string>" in plist
        assert "<string>tunneld</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist
        assert str(LOG_PATH) in plist

    def test_log_path_is_system_location(self):
        # Regression: the log path must NOT reference the user's home directory.
        # A user-home path baked into the plist caused launchd to pre-create
        # /Volumes/<HomeVolume>/... at boot for users whose home lived on an
        # external volume, blocking the real volume from mounting at its name.
        plist = generate_plist(Path("/usr/bin/pymobiledevice3"))
        assert "/Users/" not in plist
        assert "/Volumes/" not in plist
        assert "/.quern/" not in plist
        assert str(LOG_PATH).startswith("/Library/Logs/")


# ---------------------------------------------------------------------------
# installed_plist_is_current / installed_plist_log_path
# ---------------------------------------------------------------------------


class TestInstalledPlistFreshness:
    def test_missing_plist_reports_outdated(self, tmp_path):
        with patch("server.device.ios.tunneld.PLIST_PATH", tmp_path / "absent.plist"):
            assert installed_plist_log_path() is None
            assert installed_plist_is_current() is False

    def test_current_plist_reports_current(self, tmp_path):
        binary = Path("/usr/bin/pymobiledevice3")
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(binary))
        with (
            patch("server.device.ios.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=binary,
            ),
        ):
            assert installed_plist_log_path() == LOG_PATH
            assert installed_plist_is_current() is True

    def test_legacy_plist_reports_outdated(self, tmp_path):
        # Simulate the pre-migration plist that pointed StandardOutPath at the
        # user home. After upgrade, installed_plist_is_current() should be
        # False so setup / status surface the reinstall prompt.
        plist_file = tmp_path / "com.quern.tunneld.plist"
        legacy = generate_plist(Path("/usr/bin/pymobiledevice3")).replace(
            str(LOG_PATH), "/Users/somebody/.quern/tunneld.log",
        )
        plist_file.write_text(legacy)
        with patch("server.device.ios.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() == Path(
                "/Users/somebody/.quern/tunneld.log",
            )
            assert installed_plist_is_current() is False

    def test_binary_path_drift_reports_outdated(self, tmp_path):
        # Regression: after `sudo pipx install --global pymobiledevice3` adds
        # a binary at /usr/local/bin/, the plist still bakes in the old
        # per-user pipx path. Detection must catch this so the existing
        # migration prompt fires.
        binary_in_plist = Path(
            "/Volumes/Home/jham/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
        )
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(binary_in_plist))
        with (
            patch("server.device.ios.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/local/bin/pymobiledevice3"),
            ),
        ):
            assert installed_plist_is_current() is False

    def test_missing_binary_with_no_alternative_reports_outdated(self, tmp_path):
        # If the plist program path doesn't exist AND nothing else is
        # discoverable, we should still flag — the daemon will crash-loop.
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(
            generate_plist(Path("/nonexistent/pymobiledevice3")),
        )
        with (
            patch("server.device.ios.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=None,
            ),
        ):
            assert installed_plist_is_current() is False

    def test_missing_binary_with_discoverable_one_uses_discovered(self, tmp_path):
        # When find_pymobiledevice3_binary returns a different path than the
        # plist's program, plist is outdated regardless of whether the plist's
        # program exists on disk.
        binary_in_plist = Path("/usr/bin/pymobiledevice3")
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(binary_in_plist))
        with (
            patch("server.device.ios.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/local/bin/pymobiledevice3"),
            ),
        ):
            assert installed_plist_is_current() is False

    def test_unparseable_plist_reports_outdated(self, tmp_path):
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text("not a real plist")
        with patch("server.device.ios.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() is None
            assert installed_plist_is_current() is False


# ---------------------------------------------------------------------------
# _run_sudo + install_daemon upgrade path
# ---------------------------------------------------------------------------


class TestRunSudo:
    def test_returns_true_on_zero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _run_sudo(["ls"], timeout=5) is True
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == ["sudo", "ls"]
            assert kwargs["timeout"] == 5

    def test_returns_false_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert _run_sudo(["ls"], timeout=5) is False

    def test_returns_false_on_timeout_without_raising(self, capsys):
        import subprocess as sp
        with patch(
            "subprocess.run",
            side_effect=sp.TimeoutExpired(cmd=["sudo", "ls"], timeout=5),
        ):
            # Regression: previously a hung launchctl would raise
            # TimeoutExpired all the way to main() and dump a traceback.
            assert _run_sudo(["ls"], timeout=5) is False
        assert "timed out" in capsys.readouterr().out

    def test_returns_false_on_oserror(self, capsys):
        with patch("subprocess.run", side_effect=OSError("no sudo for you")):
            assert _run_sudo(["ls"], timeout=5) is False
        assert "no sudo for you" in capsys.readouterr().out


class TestInstallDaemonUpgradePath:
    def test_unloads_existing_before_bootstrap(self, tmp_path):
        """Regression: bootstrap-over-loaded returns EIO 5 and the previous
        kickstart -k fallback hung launchctl. The upgrade path must call
        bootout *before* bootstrap and skip kickstart entirely."""
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        with (
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.ios.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.ios.tunneld.time.sleep"),  # speed up the test
        ):
            assert install_daemon() == 0

        operations = [a[0] for a in calls]
        assert operations == ["launchctl", "cp", "chown", "chmod", "launchctl"]
        # bootout must come first; bootstrap last; no kickstart anywhere.
        assert calls[0] == ["launchctl", "bootout", f"system/{TUNNELD_LABEL}"]
        assert calls[-1][:3] == ["launchctl", "bootstrap", "system"]
        for call in calls:
            assert "kickstart" not in call

    def test_chmods_plist_to_644(self, tmp_path):
        """LaunchDaemon plists should be mode 644 (Apple convention).
        NamedTemporaryFile produces 600 and `cp` preserves that."""
        calls: list[list[str]] = []

        with (
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.ios.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch(
                "server.device.ios.tunneld._run_sudo",
                side_effect=lambda args, timeout: (calls.append(args), True)[1],
            ),
            patch("server.device.ios.tunneld.time.sleep"),
        ):
            assert install_daemon() == 0

        assert ["chmod", "644", str(tmp_path / "tunneld.plist")] in calls

    def test_bootstrap_retry_recovers_after_eio_race(self, tmp_path):
        """Regression: macOS 26 (Tahoe) launchd briefly races bootout/bootstrap
        and returns EIO 5 on the first bootstrap. install_daemon must retry
        once after a settle delay before giving up."""
        bootstrap_attempts = [0]

        def fake_run_sudo(args, timeout, non_interactive=False):
            if args[:3] == ["launchctl", "bootstrap", "system"]:
                bootstrap_attempts[0] += 1
                # First bootstrap fails (the race); second succeeds.
                return bootstrap_attempts[0] > 1
            return True

        with (
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.ios.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.ios.tunneld.time.sleep"),
        ):
            assert install_daemon() == 0

        # Both bootstrap attempts must have actually happened.
        assert bootstrap_attempts[0] == 2

    def test_bootstrap_failure_after_retry_returns_nonzero(self, tmp_path):
        """If bootstrap still fails after the settle retry, surface a
        diagnostic message and return 1 — don't silently fall back to
        anything destructive."""
        def fake_run_sudo(args, timeout, non_interactive=False):
            # bootstrap fails every time; everything else succeeds.
            return "bootstrap" not in args

        with (
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.ios.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.ios.tunneld.time.sleep"),
        ):
            assert install_daemon() == 1


# ---------------------------------------------------------------------------
# cli_tunneld
# ---------------------------------------------------------------------------


class TestRestartDaemon:
    """#73: restart is the command reached for when tunneld already looks
    wrong, so it must neither hang nor lie about having fixed anything."""

    @staticmethod
    def _patches(tmp_path, calls, serving, loaded=True):
        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        plist = tmp_path / "tunneld.plist"
        plist.write_text("")
        return (
            patch("server.device.ios.tunneld.PLIST_PATH", plist),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.ios.tunneld.time.sleep"),
            patch("server.device.ios.tunneld._tunneld_devices", return_value=(serving, [])),
            patch(
                "server.device.ios.tunneld.launchd_job",
                return_value={"state": "running", "pid": "1"} if loaded else {},
            ),
        )

    def test_loaded_job_is_signalled_not_reloaded(self, tmp_path):
        """KeepAlive respawns a killed daemon, so a loaded job needs only a
        signal — a smaller privilege than loading and unloading daemons."""
        calls: list[list[str]] = []
        with contextlib.ExitStack() as stack:
            for ctx in self._patches(tmp_path, calls, serving=True):
                stack.enter_context(ctx)
            assert _restart_daemon() == 0

        assert calls == [[LAUNCHCTL, "kill", "SIGKILL", f"system/{TUNNELD_LABEL}"]]
        assert not any("bootout" in c for c in calls)

    def test_unloaded_job_falls_back_to_bootout_bootstrap(self, tmp_path):
        calls: list[list[str]] = []
        with contextlib.ExitStack() as stack:
            for ctx in self._patches(tmp_path, calls, serving=True, loaded=False):
                stack.enter_context(ctx)
            assert _restart_daemon() == 0

        operations = [c[1] for c in calls]
        assert operations == ["bootout", "bootstrap"]

    def test_never_kickstart(self, tmp_path):
        """kickstart -k sends SIGKILL *and* rebuilds the job, and hung
        launchctl on macOS 15. Neither path may reach for it."""
        for loaded in (True, False):
            calls: list[list[str]] = []
            with contextlib.ExitStack() as stack:
                for ctx in self._patches(tmp_path, calls, serving=True, loaded=loaded):
                    stack.enter_context(ctx)
                _restart_daemon()
            for call in calls:
                assert "kickstart" not in call

    def test_failure_when_reloaded_but_not_serving(self, tmp_path):
        """The wedge is a live daemon with no listener, which launchd calls
        healthy. Trusting launchctl's exit code would report success for
        exactly the state the restart was meant to clear."""
        calls: list[list[str]] = []
        with contextlib.ExitStack() as stack:
            for ctx in self._patches(tmp_path, calls, serving=False):
                stack.enter_context(ctx)
            assert _restart_daemon() == 1

    def test_signal_failure_falls_through_to_reload(self, tmp_path):
        """A signal that does not bring the daemon back is not the end of the
        road — the heavier reload path is still worth trying."""
        calls: list[list[str]] = []
        serving = iter([(False, [])] * 10 + [(True, [])] * 10)

        plist = tmp_path / "tunneld.plist"
        plist.write_text("")
        with (
            patch("server.device.ios.tunneld.PLIST_PATH", plist),
            patch(
                "server.device.ios.tunneld._run_sudo",
                side_effect=lambda args, timeout, non_interactive=False: calls.append(args) or True,
            ),
            patch("server.device.ios.tunneld.time.sleep"),
            patch("server.device.ios.tunneld._tunneld_devices", side_effect=lambda: next(serving)),
            patch("server.device.ios.tunneld.launchd_job", return_value={"state": "running"}),
        ):
            assert _restart_daemon() == 0

        operations = [c[1] for c in calls]
        assert operations == ["kill", "bootout", "bootstrap"]

    def test_not_installed_touches_nothing(self, tmp_path, capsys):
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        with (
            patch("server.device.ios.tunneld.PLIST_PATH", tmp_path / "absent.plist"),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
        ):
            assert _restart_daemon() == 1
        assert calls == []
        assert "not installed" in capsys.readouterr().out


class TestCanRecoverUnattended:
    """The probe must answer "runs without a password", not "is permitted"."""

    GRANTED = (
        "User jerimiah may run the following commands on host:\n"
        "    (ALL) ALL\n"
        "    (root) NOPASSWD: /bin/launchctl kill SIGKILL system/com.quern.tunneld\n"
    )
    BLANKET_ONLY = (
        "User jerimiah may run the following commands on host:\n"
        "    (ALL) ALL\n"
    )

    def _listing(self, stdout, returncode=0):
        return patch(
            "subprocess.run",
            return_value=MagicMock(returncode=returncode, stdout=stdout),
        )

    def test_true_when_the_rule_is_present(self):
        with self._listing(self.GRANTED):
            assert can_recover_unattended() is True

    def test_false_on_blanket_sudo_alone(self):
        """Regression: `sudo -l <command>` returns 0 for anything an admin
        could run *with* a password, so a machine with `(ALL) ALL` and no grant
        reported unattended recovery as available. A false all-clear here means
        the automatic path silently does nothing."""
        with self._listing(self.BLANKET_ONLY):
            assert can_recover_unattended() is False

    def test_false_for_a_different_signal(self):
        """The grant names SIGKILL. A rule for one signal must not read as a
        rule for another."""
        with self._listing(self.GRANTED):
            with patch.object(
                tunneld_module,
                "RECOVERY_ARGS",
                ["/bin/launchctl", "kill", "SIGSTOP", "system/com.quern.tunneld"],
            ):
                assert can_recover_unattended() is False

    def test_false_when_sudo_refuses(self):
        with self._listing("", returncode=1):
            assert can_recover_unattended() is False

    def test_false_when_sudo_missing(self):
        with patch("subprocess.run", side_effect=OSError("no sudo")):
            assert can_recover_unattended() is False


class TestRecoveryIsNonInteractiveWhenAutomatic:
    def test_passes_dash_n_so_it_cannot_prompt(self):
        """With no terminal, a password prompt is indistinguishable from a
        hang, so the automatic path must fail instead of asking."""
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=1)) as run,
            patch("server.device.ios.tunneld._wait_until_serving", return_value=False),
        ):
            assert recover_wedged_tunneld(non_interactive=True) is False
        assert run.call_args[0][0][:2] == ["sudo", "-n"]

    def test_interactive_by_default_for_the_cli(self):
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as run,
            patch("server.device.ios.tunneld._wait_until_serving", return_value=True),
        ):
            assert recover_wedged_tunneld() is True
        assert run.call_args[0][0][:2] == ["sudo", "/bin/launchctl"]


class TestHealthDoesNotBlockTheEventLoop:
    """`launchd_job` shells out with a 15s timeout. This coroutine is reached
    from the screenshot path now, not just diagnostics, so running it inline
    would stall every other in-flight request for as long as launchctl takes."""

    async def test_launchd_job_runs_on_a_worker_thread(self):
        with (
            patch("server.device.ios.tunneld.is_tunneld_running", return_value=True),
            # Patching `installed_plist_is_current` here became a no-op when
            # it was derived from drift, and the real drift check then read
            # this machine's actual /Library/LaunchDaemons plist -- host state
            # deciding a test about the event loop.
            patch("server.device.ios.tunneld.installed_plist_drift", return_value=None),
            patch(
                "server.device.ios.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/bin/pmd3"),
            ),
            patch.object(Path, "exists", return_value=True),
            patch("asyncio.to_thread", new_callable=AsyncMock, return_value={}) as to_thread,
        ):
            await tunneld_health()

        assert to_thread.await_count == 1, "launchd_job was not moved off the loop"
        assert to_thread.await_args[0][0] is tunneld_module.launchd_job


class TestGrantProbeAsksAboutTheRightUser:
    """Same class of bug as the grant line itself: under sudo the process is
    root, and root is not who the rule was written for."""

    LISTING = (
        "User jerimiah may run the following commands on host:\n"
        "    (root) NOPASSWD: /bin/launchctl kill SIGKILL system/com.quern.tunneld\n"
    )

    def test_asks_about_the_named_user_when_root(self):
        with (
            patch("os.geteuid", return_value=0),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=self.LISTING),
            ) as run,
        ):
            assert can_recover_unattended("jerimiah") is True
        assert run.call_args[0][0] == ["sudo", "-n", "-l", "-U", "jerimiah"]

    def test_asks_about_itself_when_not_root(self):
        """Unprivileged, we are already the user in question, and `-U` may not
        be available to us at all."""
        with (
            patch("os.geteuid", return_value=501),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=self.LISTING),
            ) as run,
        ):
            assert can_recover_unattended("jerimiah") is True
        assert run.call_args[0][0] == ["sudo", "-n", "-l"]


class TestRecoveryGrant:
    """The grant is what turns "quern noticed" into "quern fixed it", so it
    has to be tight enough to be worth granting and safe enough to install."""

    def test_rule_is_fully_specified(self):
        rule = recovery_grant_line()
        assert "NOPASSWD:" in rule
        assert f"{LAUNCHCTL} kill SIGKILL system/{TUNNELD_LABEL}" in rule
        # No wildcard may reach the command spec: ALL after NOPASSWD, or a
        # glob in the path, would authorise far more than one signal.
        command_spec = rule.split("NOPASSWD:", 1)[1]
        assert "*" not in command_spec
        assert "ALL" not in command_spec

    def test_names_the_human_not_root_under_sudo(self, monkeypatch):
        """Under sudo, getpass.getuser() is root. A rule granting root the
        right to do what root can already do reports success and leaves the
        real user still prompted."""
        monkeypatch.setenv("SUDO_USER", "jerimiah")
        monkeypatch.setattr("getpass.getuser", lambda: "root")
        assert recovery_grant_line().startswith("jerimiah ALL=")

    def test_falls_back_to_current_user_without_sudo(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setattr("getpass.getuser", lambda: "someone")
        assert recovery_grant_line().startswith("someone ALL=")

    def test_refuses_to_install_invalid_syntax(self, capsys):
        """A malformed drop-in breaks sudo for everything on the machine, so
        validation must gate the install rather than follow it."""
        installed: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            installed.append(args)
            return True

        with (
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stderr="parse error near line 1"),
            ),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
        ):
            assert install_recovery_grant() == 1

        assert installed == [], "must not touch /etc/sudoers.d after visudo rejects it"
        assert "not valid sudoers syntax" in capsys.readouterr().out

    def test_installs_readonly_root_owned(self, capsys):
        """sudoers silently ignores a drop-in that is group- or world-writable,
        so the mode is load-bearing rather than cosmetic."""
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
            patch("server.device.ios.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.ios.tunneld.can_recover_unattended", return_value=True),
        ):
            assert install_recovery_grant() == 0

        assert calls[0][0] == "install"
        assert "0440" in calls[0]
        assert "root" in calls[0]

    def test_verifies_against_the_granted_user(self, capsys):
        """Under sudo the installer is root; checking root's policy would miss
        the rule it just wrote and report failure after a successful install."""
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
            patch("server.device.ios.tunneld._run_sudo", return_value=True),
            patch("server.device.ios.tunneld._grant_user", return_value="jerimiah"),
            patch(
                "server.device.ios.tunneld.can_recover_unattended", return_value=True
            ) as probe,
        ):
            assert install_recovery_grant() == 0
        probe.assert_called_once_with("jerimiah")

    def test_reports_when_grant_does_not_take_effect(self, capsys):
        """Installing the file and sudo honouring it are different facts, and
        reporting the first as the second would be a false all-clear."""
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
            patch("server.device.ios.tunneld._run_sudo", return_value=True),
            patch("server.device.ios.tunneld.can_recover_unattended", return_value=False),
        ):
            assert install_recovery_grant() == 1
        assert "still asks for a password" in capsys.readouterr().out


class TestCliTunneld:
    def test_help(self, capsys):
        result = cli_tunneld(["--help"])
        assert result == 0
        captured = capsys.readouterr()
        assert "install" in captured.out
        assert "uninstall" in captured.out

    def test_no_args_shows_help(self, capsys):
        result = cli_tunneld([])
        assert result == 0
        captured = capsys.readouterr()
        assert "Usage:" in captured.out

    def test_unknown_command(self, capsys):
        result = cli_tunneld(["bogus"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Unknown command" in captured.out

    def test_status_command(self):
        with (
            patch("server.device.ios.tunneld.find_pymobiledevice3_binary", return_value=None),
            patch("server.device.ios.tunneld.PLIST_PATH") as mock_plist,
        ):
            mock_plist.exists.return_value = False
            # tunneld is asked through loopback_opener (#634): stubbing
            # urlopen asked the real tunneld on this Mac.
            refused = MagicMock()
            refused.open.side_effect = Exception("refused")
            with patch("server.remote.client.loopback_opener", return_value=refused):
                result = cli_tunneld(["status"])
                assert result == 0
