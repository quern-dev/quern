"""Global test fixtures — runs before any test module is imported."""

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
