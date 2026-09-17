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
from pathlib import Path

import pytest

from server.lifecycle import releases

LOCAL = "http://127.0.0.1:8899/repos/quern-dev/quern"
#: The site repo, checked out beside this one. Relative, not absolute: an
#: absolute path made these tests pass on one machine and skip everywhere else,
#: including CI.
INSTALL_SH = Path(__file__).resolve().parents[2] / "quern.dev" / "public" / "_install.sh"


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
