"""Tests for IdbBackend — mock asyncio.create_subprocess_exec."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.device.idb import IdbBackend
from server.models import DeviceError

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Create a mock async subprocess."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value="/usr/local/bin/idb"):
            assert await backend.is_available() is True

    async def test_not_available(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value=None):
            assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# _resolve_binary
# ---------------------------------------------------------------------------


class TestResolveBinary:
    def test_found(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value="/usr/local/bin/idb"):
            path = backend._resolve_binary()
        assert path == "/usr/local/bin/idb"

    def test_cached(self):
        backend = IdbBackend()
        backend._binary = "/cached/idb"
        path = backend._resolve_binary()
        assert path == "/cached/idb"

    def test_not_found_raises(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value=None):
            with pytest.raises(DeviceError, match="idb not found"):
                backend._resolve_binary()


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


class TestRun:
    async def test_success(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"ok\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            stdout, stderr = await backend._run("ui", "describe-all", "--udid", "X")
            assert stdout == "ok\n"
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "describe-all", "--udid", "X",
                stdout=-1, stderr=-1,
            )

    async def test_nonzero_exit_raises(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stderr=b"connection refused", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="connection refused"):
                await backend._run("ui", "describe-all")

    async def test_error_tool_is_idb(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stderr=b"fail", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError) as exc_info:
                await backend._run("ui", "tap", "100", "200")
            assert exc_info.value.tool == "idb"


# ---------------------------------------------------------------------------
# describe_all
# ---------------------------------------------------------------------------


class TestDescribeAll:
    async def test_parse_fixture(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        fixture_data = (FIXTURES / "idb_describe_all_output.json").read_bytes()
        proc = _mock_proc(stdout=fixture_data)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_all("AAAA-1111")

        assert isinstance(result, list)
        assert len(result) == 14
        assert result[0]["type"] == "Application"
        assert result[1]["AXLabel"] == "Maps"

    async def test_invalid_json_raises(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"not json")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="Failed to parse"):
                await backend.describe_all("AAAA-1111")

    async def test_non_array_raises(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b'{"not": "an array"}')
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="Expected JSON array"):
                await backend.describe_all("AAAA-1111")

    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"[]")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.describe_all("UDID-123")
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "describe-all", "--udid", "UDID-123", "--nested",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# tap
# ---------------------------------------------------------------------------


class TestTap:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.tap("AAAA-1111", 100.5, 200.3)
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "tap", "100", "200",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# swipe
# ---------------------------------------------------------------------------


class TestSwipe:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.swipe("AAAA-1111", 100.5, 200.7, 300.2, 400.9, duration=1.0)
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "swipe",
                "100", "201", "300", "401",
                "--udid", "AAAA-1111",
                "--duration", "1.0",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# type_text
# ---------------------------------------------------------------------------


class TestTypeText:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.type_text("AAAA-1111", "Hello World")
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "text", "Hello World",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# press_button
# ---------------------------------------------------------------------------


class TestPressButton:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.press_button("AAAA-1111", "HOME")
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "button", "HOME",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )
