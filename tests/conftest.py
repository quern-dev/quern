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
#: This exists because the suite deleted the developer's own `quern` command on
#: every run for months, and it presented as the CLI working intermittently --
#: about the least diagnosable symptom available. The lesson is not "remember to
#: patch Path.home". A suite that *can* reach outside its sandbox eventually
#: will, quietly, so reaching outside has to be the thing that fails.
#:
#: `QUERN_STATE_DIR` now redirects the whole of `~/.quern` (see the note on
#: CONFIG_DIR in server/config.py). It previously redirected `state.json` and
#: `active-device.json` and nothing else, while this file claimed otherwise --
#: so the sandbox everyone believed in did not cover the api key, config.json,
#: the device pool or the install manifest.
#:
#: The list below is not a substitute for redirection; it is what catches the
#: next path nobody redirected. Two strengths, because a machine running quern
#: is not idle: a live daemon appends to its log, a live server rewrites its own
#: state, and an editor rewrites its config, all while the suite runs. Watching
#: those for *modification* reports the machine working rather than a test
#: misbehaving, and a guard that cries wolf gets deleted.
_WATCH_EXACTLY: dict[str, list[Path]] = {
    "quern's own install": [
        Path.home() / ".local" / "bin" / "quern",
        # The binary, not only the bundle: rewriting a file inside a directory
        # does not move the directory's own mtime, so watching `Quern.app`
        # alone was blind to its signed executable being replaced.
        Path.home() / "Applications" / "Quern.app",
        Path.home() / "Applications" / "Quern.app" / "Contents" / "MacOS" / "QuernMenuBar",
        Path.home() / ".quern" / "api-key",
        Path.home() / ".quern" / "config.json",
        Path.home() / ".quern" / "installed-by-setup.json",
    ],
    "another tool's configuration": [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "skills" / "quern-api",
        Path.home() / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json",
        Path.home() / ".cursor" / "mcp.json",
        Path.home() / ".codex" / "config.toml",
        Path.home() / ".config" / "opencode" / "opencode.json",
    ],
    "the developer's shell": [
        # `install_wrapper_script` appends `export PATH=...` to whichever of
        # these it finds, after a prompt that defaults to yes and reads
        # /dev/tty -- so running the wrong test from a terminal and pressing
        # Enter edits a real shell profile.
        Path.home() / ".zshrc",
        Path.home() / ".bashrc",
        Path.home() / ".bash_profile",
        Path.home() / ".config" / "fish" / "config.fish",
    ],
    "the machine's shared state": [
        Path("/Library/LaunchDaemons/com.quern.tunneld.plist"),
        Path("/etc/sudoers.d/quern-tunneld"),
        Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem",
        # The venv itself, not the pipx home: `pipx uninstall` removes the venv
        # and leaves the home in place, so watching the parent saw nothing.
        Path.home() / ".local" / "pipx" / "venvs" / "pymobiledevice3",
        Path.home() / "Library" / "Application Support" / "pipx" / "venvs"
        / "pymobiledevice3",
        Path("/opt/pipx/venvs/pymobiledevice3"),
    ],
}

#: Watched for existence only -- something live writes these constantly.
_WATCH_EXISTENCE: dict[str, list[Path]] = {
    "quern's own install": [
        Path.home() / ".quern",
        Path.home() / ".quern" / "state.json",
        Path.home() / ".quern" / "device-pool.json",
        Path.home() / ".quern" / "cert-state.json",
        Path.home() / ".quern" / "bin",
        Path.home() / ".local" / "share" / "quern",
    ],
    "another tool's configuration": [
        Path.home() / ".claude.json",
    ],
    "the machine's shared state": [
        Path("/Library/Logs/com.quern.tunneld.log"),
        Path.home() / ".android" / "avd",
        Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices",
    ],
}

#: Sentinel for a path that could not be read. Distinct from "does not exist":
#: `/etc/sudoers.d` is commonly 0750 root:wheel, and returning "absent" there
#: made the entry a permanent quiet pass -- a failed check reading as one that
#: passed, for the two paths least likely to be readable.
_UNREADABLE = ("unreadable",)


def _exact(path: Path) -> tuple:
    try:
        st = path.stat()
    except FileNotFoundError:
        return (False, None, None)
    except OSError:
        return _UNREADABLE
    return (True, st.st_mtime_ns, st.st_size)


def _exists(path: Path) -> tuple:
    try:
        return (path.stat() is not None,)
    except FileNotFoundError:
        return (False,)
    except OSError:
        # Propagating here failed every test in the suite over one unreadable
        # path, rather than the one at fault.
        return _UNREADABLE


def _describe(before: tuple, after: tuple) -> str:
    if _UNREADABLE in (before, after):
        return "changed the readability of"
    if before[0] and not after[0]:
        return "deleted"
    if not before[0] and after[0]:
        return "created"
    return "modified"


_WATCHED: list[tuple] = [
    (kind, path, _exact) for kind, paths in _WATCH_EXACTLY.items() for path in paths
] + [
    (kind, path, _exists) for kind, paths in _WATCH_EXISTENCE.items() for path in paths
]

# Keyed by path, so the same path in both dicts would have one reading silently
# overwrite the other -- and comparing a 1-tuple against a 3-tuple is never
# equal, turning the whole suite red with a message that misdirects.
_seen = [path for _, path, _ in _WATCHED]
assert len(_seen) == len(set(_seen)), (
    f"a path is watched twice: {sorted({p for p in _seen if _seen.count(p) > 1})}"
)


@pytest.fixture(autouse=True)
def _the_real_machine_is_not_a_fixture():
    """Fail the test that touched the machine it ran on, and name what it did.

    Per-test rather than per-session so the failure names the culprit rather
    than the run. A few dozen stat calls each side; no measurable cost against a
    91-second suite.

    What it does not catch, so nobody assumes more:

    * A test that creates a file and removes it again, or rewrites one with
      identical bytes and mtime, leaves nothing to compare.
    * Only the *first* test to change a given path is named. A later test with
      the identical defect sees the already-changed state and reads clean, so
      fixing only the named one can still leave others broken.
    * Damage that is not a file. `run_uninstall` signals the PID from state, and
      the integration tests kill a real server; nothing here sees a process.
    """
    before = {path: read(path) for _, path, read in _WATCHED}
    yield
    for kind, path, read in _WATCHED:
        after = read(path)
        if after == before[path]:
            continue
        pytest.fail(
            f"this test {_describe(before[path], after)} {path} — "
            f"{kind}, on the machine the suite is running on.\n"
            "Redirect it rather than patching Path.home at the call site: "
            "QUERN_STATE_DIR covers all of ~/.quern and setup.WRAPPER_PATH "
            "covers the wrapper; a path with no redirect wants one adding."
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
