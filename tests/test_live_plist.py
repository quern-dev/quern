"""Unit tests for server/device/live_plist.py -- the cfprefsd channel."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device import live_plist
from server.models import DeviceError


def _proc(returncode=0, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestWhatCountsAsAPreferenceFile:
    def test_a_plist_directly_in_library_preferences(self, tmp_path):
        prefs = tmp_path / "Library" / "Preferences"
        prefs.mkdir(parents=True)
        full = (prefs / "com.example.App.plist").resolve()
        assert live_plist._preferences_domain(tmp_path, full) == str(full.with_suffix(""))

    @pytest.mark.parametrize("relative", [
        "Library/config.plist",
        "Library/Preferences/nested/x.plist",
        "Library/Preferences/notes.txt",
        "Documents/Library/Preferences/x.plist",
    ])
    def test_anything_else_is_a_file(self, tmp_path, relative):
        full = (tmp_path / relative).resolve()
        assert live_plist._preferences_domain(tmp_path, full) is None

    async def test_a_shut_down_simulator_has_no_cfprefsd_to_ask(self, tmp_path):
        full = (tmp_path / "Library" / "Preferences" / "a.plist").resolve()
        with patch.object(live_plist, "get_device_state", AsyncMock(return_value="Shutdown")):
            assert await live_plist._through_cfprefsd("U", tmp_path, full) is None

    async def test_an_unknown_state_still_asks_cfprefsd(self, tmp_path):
        """Better a reported failure than a silently stale file."""
        full = (tmp_path / "Library" / "Preferences" / "a.plist").resolve()
        with patch.object(live_plist, "get_device_state", AsyncMock(return_value="unknown")):
            assert await live_plist._through_cfprefsd("U", tmp_path, full) is not None


class TestDefaultsRunner:
    async def test_it_runs_defaults_inside_the_simulator(self):
        with patch("asyncio.create_subprocess_exec", return_value=_proc(0, b"out")) as spawn:
            assert await live_plist._defaults("U", "export", "/d", "-") == b"out"
        assert spawn.call_args[0] == (
            "xcrun", "simctl", "spawn", "U", "defaults", "export", "/d", "-",
        )

    async def test_a_nonzero_exit_is_a_device_error_with_stderr(self):
        failing = _proc(1, stderr=b"Domain not found")
        with patch("asyncio.create_subprocess_exec", return_value=failing):
            with pytest.raises(DeviceError, match="Domain not found"):
                await live_plist._defaults("U", "delete", "/d", "k")

    async def test_a_missing_xcrun_is_a_device_error(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("xcrun")):
            with pytest.raises(DeviceError, match="defaults export failed"):
                await live_plist._defaults("U", "export", "/d", "-")

    async def test_a_hung_call_is_killed(self, monkeypatch):
        monkeypatch.setattr(live_plist, "_DEFAULTS_TIMEOUT", 0.05)
        proc = MagicMock()
        proc.returncode = None

        async def hang():
            await asyncio.sleep(10)

        def kill():
            proc.returncode = -9

        proc.communicate = hang
        proc.kill = MagicMock(side_effect=kill)
        proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="timed out"):
                await live_plist._defaults("U", "export", "/d", "-")
        proc.kill.assert_called_once()

    async def test_an_unreadable_export_is_a_device_error(self):
        with patch.object(live_plist, "_defaults", AsyncMock(return_value=b"not a plist")):
            with pytest.raises(DeviceError, match="unreadable"):
                await live_plist._export("U", "/d")


def test_values_map_to_defaults_types():
    assert live_plist._defaults_type(True) == ("-bool", "true")
    assert live_plist._defaults_type(False) == ("-bool", "false")
    assert live_plist._defaults_type(7) == ("-int", "7")
    assert live_plist._defaults_type(2.5) == ("-float", "2.5")
    assert live_plist._defaults_type("-x") == ("-string", "-x")


def test_read_back_compares_types_not_just_values():
    assert live_plist._same(True, True)
    assert not live_plist._same(1, True)
    assert not live_plist._same(1.0, 1)


