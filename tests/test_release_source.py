"""Where releases are fetched from, and what may be followed (#219).

A release candidate cannot be tested before it exists, which is how 0.18.3
shipped an update that crashed for everyone (#212): nothing pointed anywhere
but api.github.com, so "update from the previous release" could only be tried
after publishing. `QUERN_RELEASES_URL` moves that base for a rehearsal.

The trust rule travels with it: by default only github.com may be followed,
and when an operator has redirected the base, only that host.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from server.lifecycle import releases

LOCAL = "http://127.0.0.1:8899/repos/quern-dev/quern"
def _install_sh() -> Path:
    """The site repo's installer, checked out beside the *main* checkout.

    Not `parents[2]`: inside a git worktree that is the worktree's container
    (`.claude/worktrees`), so these assertions skipped in every review worktree
    as well as in CI -- three tests reading green because the file they check
    was not there. `--git-common-dir` points at the main checkout's `.git`
    whichever tree this runs in.
    """
    root = Path(__file__).resolve().parents[1]
    try:
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        if common:
            root = Path(common).parent
    except (OSError, subprocess.SubprocessError):
        pass
    return root.parent / "quern.dev" / "public" / "_install.sh"


#: Still skipped in CI, which checks out this repo alone. That is a real gap
#: -- the installer is the one fetcher no test here can reach -- and it is
#: named in the skip rather than hidden by a path that could never resolve.
INSTALL_SH = _install_sh()


class TestTheBase:
    def test_github_by_default(self):
        assert releases.api_base({}) == releases.DEFAULT_API
        assert not releases.is_overridden({})

    def test_the_variable_moves_it(self):
        assert releases.api_base({releases.ENV_VAR: LOCAL}) == LOCAL
        assert releases.is_overridden({releases.ENV_VAR: LOCAL})

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_an_empty_value_is_not_an_override(self, value):
        env = {} if value is None else {releases.ENV_VAR: value}
        assert releases.api_base(env) == releases.DEFAULT_API

    def test_a_trailing_slash_does_not_double_up(self):
        assert releases.api_base({releases.ENV_VAR: LOCAL + "/"}) == LOCAL


class TestWhatMayBeFollowed:
    """The URL comes out of a release response, and what is downloaded gets
    extracted over the install."""

    @pytest.mark.parametrize("url, ok", [
        # An uploaded asset.
        ("https://github.com/quern-dev/quern/releases/download/v1/quern-1.tar.gz", True),
        # GitHub's generated source tarball: what a release with no asset
        # resolves to. v0.14.1 is exactly this, and refusing it would refuse
        # those users' updates outright -- a review caught this branch doing
        # precisely that, with a test asserting it as intended.
        ("https://api.github.com/repos/quern-dev/quern/tarball/v0.14.1", True),
        ("https://github.com/quern-dev/quern/archive/refs/tags/v1.tar.gz", True),
        # Another repository on the same host: a free account is enough to
        # host a payload there, and the payload is extracted over the install.
        ("https://github.com/attacker/evil/releases/download/v1/quern-1.tar.gz", False),
        ("https://github.com/@evil.example/x.tar.gz", False),
        ("https://github.com.evil.example/quern.tar.gz", False),
        ("https://github.com@evil.example/quern.tar.gz", False),
        # Case is not significant in a host, and is in a path.
        ("https://GitHub.COM/quern-dev/quern/releases/download/v1/q.tar.gz", True),
        ("https://github.com/Quern-Dev/quern/releases/download/v1/q.tar.gz", False),
        # Plaintext, and other endpoints on the API host.
        ("http://github.com/quern-dev/quern/releases/download/v1/q.tar.gz", False),
        ("https://api.github.com/repos/quern-dev/quern/releases/assets/1", False),
        ("https://example.com/quern.tar.gz", False),
        ("file:///etc/passwd", False),
        ("x.tgz", False),
        # A prefix compare reads left to right, and `..` is how a path that
        # starts inside the allowlist ends up outside it. api.github.com
        # resolves these server-side and urllib sends the path unchanged, so
        # this one passed the check and fetched octocat/Hello-World's tree --
        # the substitution the repo pin exists to prevent, through the pin.
        ("https://api.github.com/repos/quern-dev/quern/tarball/../../../"
         "octocat/Hello-World/tarball/master", False),
        ("https://api.github.com/repos/quern-dev/quern/tarball/%2e%2e/%2e%2e/evil", False),
        ("https://github.com/quern-dev/quern/releases/download/../../evil/x.tar.gz", False),
        ("https://github.com/quern-dev/quern/releases/download/./v1/q.tar.gz", False),
        # An encoded separator is one character to the server and a boundary
        # to a reader. The two readings must not disagree about the repo.
        ("https://github.com/quern-dev%2Fquern%2Freleases%2Fdownload%2Fv1/q.tar.gz", False),
        # A default port written out is the same origin, and a false refusal
        # here is how the last version of this check would have blocked every
        # v0.14.1 user's update.
        ("https://github.com:443/quern-dev/quern/releases/download/v1/q.tar.gz", True),
        ("https://github.com:8443/quern-dev/quern/releases/download/v1/q.tar.gz", False),
        ("https://github.com:notaport/quern-dev/quern/releases/download/v1/q.tar.gz", False),
        # Userinfo: the host a person reads is not the host urllib connects to.
        ("https://github.com:x@evil.example/quern-dev/quern/releases/download/v1/q", False),
        # And the other way round, which is the case the host check alone does
        # not cover: this really does resolve to github.com, on a real path,
        # so only the explicit refusal stops urllib sending those credentials.
        # A release response naming credentials is not one to follow.
        ("https://evil.example@github.com/quern-dev/quern/releases/download/v1/q.tar.gz", False),
        # A version string is not a path segment worth splitting on.
        ("https://github.com/quern-dev/quern/releases/download/v1.2.3/q.tar.gz", True),
    ])
    def test_by_default_only_this_repository(self, url, ok):
        assert releases.asset_url_is_trusted(url, {}) is ok

    @pytest.mark.parametrize("url, ok", [
        (LOCAL + "/releases/download/v1/quern-1.tar.gz", True),
        (LOCAL + "/tarball/v1", True),
        ("http://127.0.0.1:8899/repos/quern-dev/quern/anything", True),
        ("http://127.0.0.1:8899/elsewhere", False),      # outside the named base
        ("http://127.0.0.1:9999/quern.tar.gz", False),   # another port is another server
        # A scheme downgrade is a different server too. A review mutation that
        # compared only the host survived the suite until this case existed.
        ("https://127.0.0.1:8899/repos/quern-dev/quern/x.tar.gz", False),
        ("https://github.com/quern-dev/quern/releases/download/v1/q.tar.gz", False),
        ("https://example.com/quern.tar.gz", False),
    ])
    def test_an_override_trusts_only_itself(self, url, ok):
        """Not "anything goes": a redirected base is one host the operator
        named, and a response from it naming somewhere else is still wrong."""
        assert releases.asset_url_is_trusted(url, {releases.ENV_VAR: LOCAL}) is ok


class TestTheBuiltUrl:
    def test_github_by_default(self):
        url = releases.download_url("0.18.5", {})
        assert url == ("https://github.com/quern-dev/quern/releases/download/"
                       "v0.18.5/quern-0.18.5.tar.gz")

    def test_the_override_is_used_and_trusted(self):
        url = releases.download_url("0.18.5", {releases.ENV_VAR: LOCAL})
        assert url.startswith(LOCAL)
        assert releases.asset_url_is_trusted(url, {releases.ENV_VAR: LOCAL})


class TestEveryFetcherUsesIt:
    """One base, not one per caller: two sources of truth is how a rehearsal
    ends up verifying something other than what runs."""

    def test_the_updater_resolves_through_the_base(self, monkeypatch):
        from server.lifecycle import updater

        seen = []
        monkeypatch.setenv(releases.ENV_VAR, LOCAL)

        class Resp:
            def __init__(self, url):
                seen.append(url)
                self._body = ('{"tag_name": "v1.0.0", "tarball_url": "'
                              + LOCAL + '/t.tgz"}').encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: Resp(req.full_url))
        assert updater._fetch_latest_release("stable") == ("1.0.0", LOCAL + "/t.tgz")
        assert seen == [LOCAL + "/releases/latest"]

    def test_setups_app_fetch_resolves_through_the_base(self, monkeypatch, tmp_path):
        from server.lifecycle import setup as setup_mod

        seen = []
        monkeypatch.setenv(releases.ENV_VAR, LOCAL)
        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", tmp_path / "Applications")
        monkeypatch.setattr("server.get_version", lambda: "1.0.0")

        def urlopen(url, timeout=0):
            seen.append(getattr(url, "full_url", url))
            raise OSError("stop here; the base is what this checks")

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        setup_mod.fetch_menubar_app(tmp_path)
        assert seen == [LOCAL + "/releases/tags/v1.0.0"]

    @pytest.mark.skipif(
        sys.platform != "darwin",
        # The suite's first platform skip, and deliberately narrow. `menubar`
        # guards every entry point on platform.system() != "Darwin" and returns
        # before fetching anything, so off a Mac this asserts on a URL list that
        # nothing could have filled -- a failure that says the test cannot run
        # here, not that the base resolution is wrong. Skipping is honest; the
        # Linux job is a backstop for shared code, and the menu-bar app is not
        # shared code. Do not reach for this marker to quiet a test that is
        # merely inconvenient on Linux: what it covers has to be macOS-only.
        reason="menubar is macOS-only and returns before fetching anywhere else",
    )
    def test_menubar_install_resolves_through_the_base(self, monkeypatch, tmp_path):
        """The caller the module docstring names, and the one that was missed:
        it built the github.com URL itself, so a rehearsal fetched and verified
        the *published* app while claiming to check the candidate."""
        from server.lifecycle import menubar

        monkeypatch.setenv(releases.ENV_VAR, LOCAL)
        state = menubar.AppState(
            path=tmp_path / "Applications" / "Quern.app",
            installed=False, version=None, running=False, quern_version="1.0.0",
        )
        monkeypatch.setattr(menubar, "state", lambda: state)
        monkeypatch.setattr(menubar.setup, "WRAPPER_PATH", tmp_path / "quern")
        (tmp_path / "quern").write_text("#!/bin/sh\n")
        (tmp_path / "quern").chmod(0o755)
        (tmp_path / "Applications").mkdir()

        seen = []

        def download(url, version, dest):
            seen.append(url)
            raise OSError("stop here; the base is what this checks")

        monkeypatch.setattr(menubar.setup, "download_release_app", download)
        menubar.cmd_install(force=True)
        assert seen == [LOCAL + "/releases/download/v1.0.0/quern-1.0.0.tar.gz"]

    def test_the_updater_refuses_a_download_off_the_release_host(
        self, monkeypatch, tmp_path, capsys,
    ):
        """The path that replaces the whole source tree had no such check at
        all until #219; setup's app fetch has had one since it was written."""
        from server.lifecycle import updater

        monkeypatch.setattr(updater, "_read_local_version", lambda _r: "0.1.0")
        monkeypatch.setattr(updater, "_fetch_latest_release",
                            lambda _ch: ("9.9.9", "https://example.com/quern.tar.gz"))
        downloaded = []
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: downloaded.append(a) or (_ for _ in ()).throw(
                                AssertionError("downloaded from an untrusted host")))

        assert updater._update_via_tarball(tmp_path) == 1
        assert downloaded == []
        assert "not on the release host" in capsys.readouterr().out


class TestTheInstallScript:
    """The one fetcher that is not Python, and the only route a first install
    takes."""

    @pytest.fixture
    def script(self):
        if not INSTALL_SH.exists():
            pytest.skip(f"{INSTALL_SH} is not checked out beside this repo")
        return INSTALL_SH.read_text()

    def test_it_reads_the_same_variable(self, script):
        assert re.search(r'RELEASES_API="\$\{QUERN_RELEASES_URL:-', script)

    def test_nothing_still_hardcodes_the_api(self, script):
        hardcoded = [line for line in script.splitlines()
                     if "api.github.com" in line and "QUERN_RELEASES_URL" not in line]
        assert not hardcoded, f"these bypass the override: {hardcoded}"

    def test_the_source_fallback_still_uses_githubs_download_host(self, script):
        """`api.github.com` is not where a source tarball lives, so the
        default cannot be derived from the API base."""
        assert 'TARBALL_URL="https://github.com/${GITHUB_REPO}/archive' in script
