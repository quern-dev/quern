"""Unit tests for server/device/plist.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.plist import read_plist, remove_plist_key, set_plist_value
from server.models import DeviceError


def _mock_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    """Return an asyncio.Process-like mock."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestReadPlist:
    async def test_read_plist_parses_output(self, tmp_path):
        # Write a real plist file and read it back
        import plistlib
        plist_path = tmp_path / "test.plist"
        with open(plist_path, "wb") as f:
            plistlib.dump({"foo": "bar", "count": 42}, f)
        result = await read_plist(plist_path)
        assert result == {"foo": "bar", "count": 42}

    async def test_read_plist_raises_on_nonzero(self, tmp_path):
        with pytest.raises(DeviceError, match="plistlib read failed"):
            await read_plist(tmp_path / "missing.plist")


class TestSetPlistValue:
    async def test_set_bool_uses_bool_flag(self, tmp_path):
        proc = _mock_proc(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await set_plist_value(tmp_path / "test.plist", "myKey", True)
        call_args = mock_exec.call_args[0]
        assert "-bool" in call_args
        assert "true" in call_args

    async def test_set_false_bool(self, tmp_path):
        proc = _mock_proc(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await set_plist_value(tmp_path / "test.plist", "myKey", False)
        call_args = mock_exec.call_args[0]
        assert "-bool" in call_args
        assert "false" in call_args

    async def test_set_int_uses_integer_flag(self, tmp_path):
        proc = _mock_proc(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await set_plist_value(tmp_path / "test.plist", "count", 42)
        call_args = mock_exec.call_args[0]
        assert "-integer" in call_args
        assert "42" in call_args

    async def test_set_float_uses_float_flag(self, tmp_path):
        proc = _mock_proc(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await set_plist_value(tmp_path / "test.plist", "ratio", 3.14)
        call_args = mock_exec.call_args[0]
        assert "-float" in call_args

    async def test_set_string_uses_string_flag(self, tmp_path):
        proc = _mock_proc(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await set_plist_value(tmp_path / "test.plist", "name", "hello")
        call_args = mock_exec.call_args[0]
        assert "-string" in call_args
        assert "hello" in call_args

    async def test_set_raises_on_nonzero(self, tmp_path):
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(1, stderr=b"key not found"),
        ):
            with pytest.raises(DeviceError, match="plutil set failed"):
                await set_plist_value(tmp_path / "test.plist", "badKey", "value")


class TestRemovePlistKey:
    async def test_remove_key_calls_plutil_remove(self, tmp_path):
        proc = _mock_proc(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await remove_plist_key(tmp_path / "test.plist", "obsoleteKey")
        call_args = mock_exec.call_args[0]
        assert "plutil" in call_args
        assert "-remove" in call_args
        assert "obsoleteKey" in call_args

    async def test_remove_raises_on_nonzero(self, tmp_path):
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(1, stderr=b"no such key"),
        ):
            with pytest.raises(DeviceError, match="plutil remove failed"):
                await remove_plist_key(tmp_path / "test.plist", "missingKey")
