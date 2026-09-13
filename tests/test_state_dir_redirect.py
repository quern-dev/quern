"""`QUERN_STATE_DIR` must redirect all of `~/.quern`, not some of it.

It used to redirect two files. `server/lifecycle/state.py` read the variable
itself and applied it to `state.json` and `active-device.json`; every other path
was built from `Path.home()` at import -- the api key, config.json, the device
pool, crash reports, the install manifest, the tool snapshot, the bin directory.
Meanwhile `tests/conftest.py` and `CONTRIBUTING.md` both told contributors that
`~/.quern` was redirected, so the sandbox everyone relied on covered a fraction
of what they thought.

A test calling `regenerate_api_key()` would therefore have rewritten the real
key and left every MCP client on the machine authenticating with a stale one.

This runs in a subprocess because the paths are module-level constants: they are
computed once at import, so the variable has to be set before Python loads them.
That is also why the failure mode was so easy to miss -- patching the
environment inside a test changes nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Every module-level path that should live under the state directory, with the
#: module it comes from. Grepped for `Path.home() / ".quern"` originally; a new
#: one that forgets to use CONFIG_DIR is exactly what this catches.
PATHS = [
    ("server.config", "CONFIG_DIR"),
    ("server.config", "API_KEY_FILE"),
    ("server.config", "USER_CONFIG_FILE"),
    ("server.lifecycle.state", "STATE_FILE"),
    ("server.lifecycle.state", "ACTIVE_DEVICE_FILE"),
    ("server.lifecycle.setup", "INSTALL_MANIFEST"),
    ("server.lifecycle.setup", "TOOL_SNAPSHOT"),
    ("server.device.pool", "POOL_FILE"),
    ("server.device.preview", "QUERN_BIN_DIR"),
    ("server.device.sim_bridge", "QUERN_BIN_DIR"),
    ("server.device.u2_client", "_QUERN_DRIVER_APK"),
    ("server.sources.crash", "CRASH_DIR"),
    ("server.proxy.extension", "INSTALL_DIR"),
]


def _resolved_under(state_dir: str) -> dict[str, str]:
    """Import each module with QUERN_STATE_DIR set and report where it points."""
    lines = "\n".join(
        f"import {module}; print({name!r}, {module}.{attr})"
        for module, attr in PATHS
        for name in [f"{module}.{attr}"]
    )
    script = f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{lines}\n"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "QUERN_STATE_DIR": state_dir},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split(" ", 1) for line in result.stdout.strip().splitlines())


def test_every_quern_path_follows_the_state_dir(tmp_path):
    state_dir = str(tmp_path / "redirected")
    resolved = _resolved_under(state_dir)

    escaped = {
        name: path for name, path in resolved.items() if not path.startswith(state_dir)
    }
    assert not escaped, (
        "these point outside the redirect, so a test writing through them "
        f"reaches the real machine: {escaped}"
    )


def test_the_real_home_is_where_things_go_without_the_variable():
    """The redirect is an override, not the normal path. A bug here would send
    a real install somewhere nobody expects."""
    lines = "\n".join(
        f"import {module}; print({module}.{attr})" for module, attr in PATHS
    )
    script = f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{lines}\n"
    env = {k: v for k, v in os.environ.items() if k != "QUERN_STATE_DIR"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    expected = str(Path.home() / ".quern")
    for path in result.stdout.strip().splitlines():
        assert path.startswith(expected), f"{path} is not under {expected}"
