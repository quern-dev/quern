"""`quern start`'s banner names the files the server really uses (#487).

It printed a fixed ~/.quern/api-key and ~/.quern/crashes, so under
QUERN_STATE_DIR -- how every scratch and test server is kept off the real one
-- it sent a reader to the owner's own key, not this server's.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _banner_paths(env: dict) -> list[str]:
    """The two lines as the banner builds them, in a process with `env`:
    CONFIG_DIR is fixed at import, so a fresh interpreter is the honest test."""
    code = ("import server.config as c, server.main as m;"
            "print(m._home_relative(c.API_KEY_FILE));"
            "print(m._crash_dir_shown(None))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.splitlines()


def test_under_quern_state_dir_the_banner_names_that_directory(tmp_path):
    env = {**os.environ, "QUERN_STATE_DIR": str(tmp_path), "HOME": str(tmp_path / "home")}
    key, crashes = _banner_paths(env)
    assert key == str(tmp_path / "api-key")
    assert crashes == str(tmp_path / "crashes")


def test_by_default_it_reads_as_it_always_did(tmp_path):
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if k != "QUERN_STATE_DIR"}
    env["HOME"] = str(home)
    assert _banner_paths(env) == ["~/.quern/api-key", "~/.quern/crashes"]


def test_a_given_crash_dir_is_shown_as_given():
    from server.main import _crash_dir_shown, _home_relative

    assert _crash_dir_shown("/var/tmp/crashes") == "/var/tmp/crashes"
    assert _home_relative(Path.home() / "x" / "y") == "~/x/y"


def test_the_banner_prints_through_them():
    """Not literals: a fixed path in either print was the bug."""
    source = (REPO / "server" / "main.py").read_text()
    assert '"  API key file: ~/.quern/api-key"' not in source
    assert "_home_relative(_config_mod.API_KEY_FILE)" in source
    assert "_crash_dir_shown(args.crash_dir)" in source
