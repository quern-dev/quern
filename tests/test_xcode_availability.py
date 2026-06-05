"""Tests for server.device._xcode.xcode_available preflight."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_xcode_cache(monkeypatch):
    """Clear the lru_cache between tests and bypass the autouse conftest
    fixture so we exercise the real implementation."""
    from server.device import _xcode
    _xcode.xcode_available.cache_clear()
    # Clear DEVELOPER_DIR so its presence doesn't shortcut the check.
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    yield
    _xcode.xcode_available.cache_clear()


def _xcode_select_result(returncode: int, stdout: str = ""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_returns_true_when_xcode_select_reports_a_developer_dir():
    from server.device._xcode import xcode_available
    with patch(
        "subprocess.run",
        return_value=_xcode_select_result(0, "/Applications/Xcode.app/Contents/Developer\n"),
    ) as run_mock:
        assert xcode_available() is True
    assert run_mock.call_count == 1
    args, _ = run_mock.call_args
    assert args[0] == ["xcode-select", "-p"]


def test_returns_false_when_xcode_select_exits_nonzero():
    """No developer dir configured — Mac will trigger the install dialog
    if anything invokes xcrun, so we must report unavailable."""
    from server.device._xcode import xcode_available
    with patch("subprocess.run", return_value=_xcode_select_result(2, "")):
        assert xcode_available() is False


def test_returns_false_when_xcode_select_returns_empty_stdout():
    """Defensive: rc==0 but no dev dir means xcrun would still fail."""
    from server.device._xcode import xcode_available
    with patch("subprocess.run", return_value=_xcode_select_result(0, "   \n")):
        assert xcode_available() is False


def test_returns_true_when_developer_dir_env_var_is_set(monkeypatch):
    """Quern's setup script sets DEVELOPER_DIR as a runtime fix-up — that
    counts even if xcode-select isn't pointed at anything useful."""
    from server.device._xcode import xcode_available
    monkeypatch.setenv("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    # xcode-select isn't called when DEVELOPER_DIR is set.
    with patch("subprocess.run") as run_mock:
        assert xcode_available() is True
    assert run_mock.call_count == 0


def test_returns_false_on_subprocess_exception():
    """Defensive: subprocess failure shouldn't propagate; we report
    unavailable so callers gate their xcrun calls."""
    from server.device._xcode import xcode_available
    with patch("subprocess.run", side_effect=OSError("xcode-select not found")):
        assert xcode_available() is False


def test_result_is_cached_across_calls():
    """The lru_cache means we don't shell out on every backend probe."""
    from server.device._xcode import xcode_available
    with patch(
        "subprocess.run",
        return_value=_xcode_select_result(0, "/Applications/Xcode.app/Contents/Developer\n"),
    ) as run_mock:
        xcode_available()
        xcode_available()
        xcode_available()
    assert run_mock.call_count == 1


def test_cache_clear_forces_reprobe():
    """cache_clear() should make the next call shell out again. Used by
    setup.py after it mutates DEVELOPER_DIR mid-process."""
    from server.device._xcode import xcode_available
    with patch(
        "subprocess.run",
        return_value=_xcode_select_result(0, "/dir\n"),
    ) as run_mock:
        xcode_available()
        xcode_available.cache_clear()
        xcode_available()
    assert run_mock.call_count == 2
