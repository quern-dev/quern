"""Global test fixtures — runs before any test module is imported."""

import functools
import os
import tempfile
from pathlib import Path

import pytest

# Redirect state.json to a temp directory so tests don't clobber
# a running server's state file.
_test_state_dir = tempfile.mkdtemp(prefix="quern-test-")
os.environ["QUERN_STATE_DIR"] = _test_state_dir


@pytest.fixture(autouse=True)
def _reset_active_device_sidecar():
    """Clear active-device.json between tests. Without this, any test that
    mutates DeviceController._active_udid leaves a real file on disk that
    the next test's DeviceController() constructor reads back, producing
    cross-test state leaks (the persistence is now a sidecar file, not
    state.json — so the existing QUERN_STATE_DIR redirect alone isn't
    enough)."""
    sidecar = Path(_test_state_dir) / "active-device.json"
    if sidecar.exists():
        sidecar.unlink()
    yield
    if sidecar.exists():
        sidecar.unlink()


@pytest.fixture(autouse=True)
def _default_xcode_available(monkeypatch):
    """Default xcode_available() to True so existing iOS-backend tests work
    regardless of whether the CI runner has Xcode installed. Each consumer
    holds its own local reference (`from server.device._xcode import
    xcode_available`), so we patch every import site. Tests verifying the
    no-Xcode gate can monkeypatch the same names to ``lambda: False``.

    The replacement is lru_cached so callers that invoke ``.cache_clear()``
    (e.g. ``_fix_developer_dir_for_setup`` after it mutates DEVELOPER_DIR)
    don't blow up on a bare ``lambda``.
    """
    def _make_stub():
        return functools.lru_cache(maxsize=1)(lambda: True)
    for path in (
        "server.device.simctl.xcode_available",
        "server.device.devicectl.xcode_available",
        "server.lifecycle.setup.xcode_available",
    ):
        monkeypatch.setattr(path, _make_stub())
