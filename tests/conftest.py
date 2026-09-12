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


#: Everything on the real machine that quern installs, writes or removes.
#:
#: Enumerated from the source rather than from memory -- every `Path.home() /
#: ...` and absolute path under `server/` -- because the point of a guard is to
#: cover the ones nobody thought of.
#:
#: This exists because the suite deleted the developer's own `quern` command on
#: every run for months. `QUERN_STATE_DIR` redirects `~/.quern`, but nothing
#: redirected the wrapper: `run_uninstall` built `Path.home() /
#: ".local/bin/quern"` inline, and the uninstall tests patched six other things
#: and not `Path.home`. It presented as the CLI working intermittently, which is
#: about the least diagnosable symptom available.
#:
#: The lesson is not "remember to patch Path.home". A suite that *can* reach
#: outside its sandbox eventually will, quietly. Reaching outside has to be the
#: thing that fails.
#:
#: Two strengths, because a machine running quern is not idle. A live daemon
#: appends to its log, a live server rewrites its own state, and the editor this
#: is being written in rewrites its config -- all while the suite runs. Watching
#: those for *modification* reports the machine working, not a test misbehaving,
#: and a guard that cries wolf gets deleted. So they are watched for appearing
#: and disappearing only, which is the shape the real bug had. Everything
#: nothing else writes is watched exactly.
_WATCH_EXACTLY: dict[str, list[Path]] = {
    "quern's own install": [
        Path.home() / ".local" / "bin" / "quern",
        Path.home() / "Applications" / "Quern.app",
    ],
    "another tool's configuration": [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".cursor" / "mcp.json",
        Path.home() / ".codex" / "config.toml",
        Path.home() / ".config" / "opencode" / "opencode.json",
    ],
    "the machine's shared state": [
        Path("/Library/LaunchDaemons/com.quern.tunneld.plist"),
        Path("/etc/sudoers.d/quern-tunneld"),
        Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem",
    ],
}

#: Watched for existence only -- a running quern, daemon or editor writes these
#: legitimately and constantly.
_WATCH_EXISTENCE: dict[str, list[Path]] = {
    "quern's own install": [
        Path.home() / ".quern",
        Path.home() / ".quern" / "api-key",
        Path.home() / ".quern" / "state.json",
        Path.home() / ".quern" / "config.json",
        Path.home() / ".quern" / "device-pool.json",
        Path.home() / ".quern" / "cert-state.json",
        Path.home() / ".quern" / "installed-by-setup.json",
        Path.home() / ".local" / "share" / "quern",
    ],
    "another tool's configuration": [
        Path.home() / ".claude.json",
    ],
    "the machine's shared state": [
        Path("/Library/Logs/com.quern.tunneld.log"),
        Path.home() / ".android" / "avd",
        Path.home() / ".local" / "pipx",
        Path("/opt/pipx"),
    ],
}


def _exact(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (False, None, None)
    return (True, st.st_mtime_ns, st.st_size)


def _exists(path: Path) -> tuple:
    return (path.exists(),)


def _describe(before: tuple, after: tuple) -> str:
    if before[0] and not after[0]:
        return "deleted"
    if not before[0] and after[0]:
        return "created"
    return "modified"


@pytest.fixture(autouse=True)
def _the_real_machine_is_not_a_fixture():
    """Fail the test that touched the machine it ran on, and name what it did.

    Per-test rather than per-session so the failure names the culprit rather
    than the run. A handful of stat calls each side; measured at no detectable
    cost across the whole suite.

    What it does not catch, stated so nobody assumes more: a test that creates
    a file and removes it again, or rewrites one with identical bytes at an
    identical mtime, leaves nothing to compare. Catching that needs an audit
    hook on every `open` in the suite, which is a real cost for a case that is
    hard to reach by accident -- the bugs this is for leave a trace, because
    they are about installing and uninstalling rather than round-tripping.
    """
    watched = [
        (kind, path, _exact) for kind, paths in _WATCH_EXACTLY.items() for path in paths
    ] + [
        (kind, path, _exists) for kind, paths in _WATCH_EXISTENCE.items() for path in paths
    ]
    before = {path: read(path) for _, path, read in watched}
    yield
    for kind, path, read in watched:
        after = read(path)
        if after == before[path]:
            continue
        pytest.fail(
            f"this test {_describe(before[path], after)} {path} — "
            f"{kind}, on the machine the suite is running on.\n"
            "Redirect it rather than patching Path.home at the call site: "
            "setup.WRAPPER_PATH and QUERN_STATE_DIR both exist for this, and a "
            "path with no redirect wants one adding."
        )


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
