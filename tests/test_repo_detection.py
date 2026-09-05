"""Which repository the review and merge scripts act on.

Both hardcoded `jerimiah797/quern`. That kept working after the repo moved to
the `quern-dev` org only because GitHub serves a permanent 301 from the old
owner path -- so the scripts were reaching the right repo by redirect rather
than by knowing where it was. A repo later created at `jerimiah797/quern` would
supersede that redirect, and `pr-review-status.py` would report on somebody
else's PRs while `merge-pr.sh` tried to merge into their repository.

Both now derive it from `git remote get-url origin`, and share one detection:
if the gate checked one repo and the merge ran against another, the check would
be meaningless in the most dangerous way available.

Returning None for anything unparseable is the load-bearing part -- a guess here
sends a merge somewhere nobody asked for.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pr-review-status.py"


def _load():
    spec = importlib.util.spec_from_file_location("pr_review_status", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load()


def test_importing_the_script_does_not_shell_out_to_git(script):
    """Detection runs only under __main__, so importing it on a machine with no
    origin remote must not exit."""
    assert script.REPO == "unknown/unknown"
    assert script.HOST == "unknown"


@pytest.mark.parametrize(("url", "expected"), [
    ("git@github.com:quern-dev/quern.git", ("github.com", "quern-dev/quern")),
    ("git@github.com:quern-dev/quern", ("github.com", "quern-dev/quern")),
    ("https://github.com/quern-dev/quern.git", ("github.com", "quern-dev/quern")),
    ("https://github.com/quern-dev/quern", ("github.com", "quern-dev/quern")),
    ("ssh://git@github.com/quern-dev/quern.git", ("github.com", "quern-dev/quern")),
    ("git://github.com/quern-dev/quern.git", ("github.com", "quern-dev/quern")),
    # A fork must resolve to the fork, not to upstream -- that is the case the
    # hardcoded value could never serve.
    ("git@github.com:someone-else/quern.git", ("github.com", "someone-else/quern")),
    ("  https://github.com/quern-dev/quern.git  ", ("github.com", "quern-dev/quern")),
    # Credentials in the userinfo must not end up in the host.
    ("https://user:token@github.com/quern-dev/quern.git",
     ("github.com", "quern-dev/quern")),
])
def test_remote_urls_resolve_to_host_owner_and_name(script, url, expected):
    assert script.parse_remote(url) == expected


@pytest.mark.parametrize(("url", "host"), [
    ("git@gitea.example.com:team/quern.git", "gitea.example.com"),
    ("https://ghe.corp.internal/team/quern.git", "ghe.corp.internal"),
    ("ssh://git@codeberg.org/team/quern.git", "codeberg.org"),
])
def test_the_host_is_preserved_not_discarded(script, url, host):
    """`gh --repo owner/name` resolves against the default host, so dropping the
    host makes a self-hosted remote select a same-named repository on
    github.com -- the exact bug this change exists to remove, one layer down.

    An earlier version of this test asserted `team/quern` for the gitea URL and
    so encoded the bug as correct."""
    parsed = script.parse_remote(url)
    assert parsed is not None
    assert parsed[0] == host
    assert parsed[1] == "team/quern"


@pytest.mark.parametrize("url", [
    "", "   ", "not-a-url", "https://github.com/", "quern", "git@github.com:",
    "https://github.com/onlyone",
])
def test_unparseable_remotes_return_none_rather_than_guessing(script, url):
    assert script.parse_remote(url) is None


@pytest.mark.parametrize(("url", "must_not_contain"), [
    ("https://user:s3cr3t@github.com/quern-dev/quern.git", "s3cr3t"),
    ("https://token@github.com/quern-dev/quern.git", "token"),
])
def test_the_failure_message_does_not_echo_credentials(script, url, must_not_contain):
    """A remote can carry credentials in its userinfo, and this runs in CI where
    stderr is captured and kept (CWE-532)."""
    assert must_not_contain not in script.redact(url)
    assert "github.com" in script.redact(url), "the host is still useful to see"


def test_detection_matches_this_clone(script):
    """The end-to-end check: whatever this clone pushes to is what the scripts
    act on.

    Asserted against `parse_remote`, not against a literal `quern-dev/quern` --
    hardcoding upstream here would fail in exactly the fork this change exists
    to support, which is the mistake the whole PR is about.
    """
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=10,
        cwd=_SCRIPT.parents[1],
    ).stdout.strip()
    parsed = script.parse_remote(url)
    assert parsed is not None
    host, slug = parsed
    emitted = subprocess.run(
        ["python3", str(_SCRIPT), "--repo-slug"],
        capture_output=True, text=True, timeout=30, cwd=_SCRIPT.parents[1],
    ).stdout.strip()
    assert emitted == f"{host}/{slug}"


def test_the_scripts_share_one_detection():
    """merge-pr.sh must not grow its own copy: a gate checking one repository
    while the merge runs against another is worse than no gate."""
    merge = (_SCRIPT.parent / "merge-pr.sh").read_text()
    assert "--repo-slug" in merge, "merge-pr.sh should ask the gate for the slug"
    assert 'gh pr merge "$PR" --repo "$REPO"' in merge


def test_no_hardcoded_owner_remains():
    for path in (_SCRIPT, _SCRIPT.parent / "merge-pr.sh"):
        for line in path.read_text().splitlines():
            if "jerimiah797" in line:
                # The docstring may explain the history; code may not.
                assert line.strip().startswith("#") or "Hardcoded as" in line, line


# --------------------------------------------------------------------------
# The host has to reach gh, not just be parsed
# --------------------------------------------------------------------------
#
# Preserving the host in parse_remote is useless if the calls still go to the
# default one. A mutation removing the --hostname injection passed every test
# above, which is the same shape of gap as parsing the host and discarding it.


def _capture_gh(script, monkeypatch):
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    return calls


def test_gh_api_calls_carry_the_hostname(script, monkeypatch):
    calls = _capture_gh(script, monkeypatch)
    script.gh("api", "repos/owner/name/pulls/1/reviews")
    assert calls, "gh was never invoked"
    cmd = calls[0]
    assert "--hostname" in cmd, "gh api would resolve against the default host"
    assert cmd[cmd.index("--hostname") + 1] == script.HOST
    # The REST path itself stays owner/name — the host travels as a flag.
    assert "repos/owner/name/pulls/1/reviews" in cmd


def test_an_explicit_hostname_is_not_overridden(script, monkeypatch):
    calls = _capture_gh(script, monkeypatch)
    script.gh("api", "--hostname", "example.com", "repos/owner/name")
    assert calls[0].count("--hostname") == 1
    assert "example.com" in calls[0]


def test_non_api_calls_are_left_alone(script, monkeypatch):
    """`gh pr` takes the host inside --repo, so injecting --hostname there would
    be a second, conflicting way to say the same thing."""
    calls = _capture_gh(script, monkeypatch)
    script.gh("pr", "view", "1", "--repo", "github.com/owner/name")
    assert "--hostname" not in calls[0]


def test_repo_arg_is_host_qualified(script):
    """`gh` accepts HOST/OWNER/REPO for github.com too, so there is one form
    rather than a conditional that could pick the wrong branch."""
    assert script.repo_arg() == f"{script.HOST}/{script.REPO}"
