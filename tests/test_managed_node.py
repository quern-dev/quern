"""Quern's own Node (#760): choosing, verifying, installing and using it.

The downloads are served from a real tarball built here, so the checksum,
unpacking and `current` link are exercised for real. Only `codesign` is faked:
a test cannot produce the Node.js Foundation's signature.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from server.lifecycle import managed_node as mn
from server.lifecycle import node_env

GOOD_SIGNATURE = (
    "Authority=Developer ID Application: Node.js Foundation\n"
    f"TeamIdentifier={mn.TEAM_ID}\n"
)


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Redirect ~/.quern for the module; conftest already blocks real downloads."""
    monkeypatch.setattr(mn, "CONFIG_DIR", tmp_path / "quern")
    return tmp_path / "quern"


def _tarball(version: str, arch: str = "arm64", *, reports: str | None = None,
             extra: list[tarfile.TarInfo] | None = None) -> bytes:
    """A minimal Node release: an executable `bin/node` that prints a version,
    npm as a relative link, and the `include/` that is meant to be skipped."""
    top = f"node-{version}-darwin-{arch}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, data=b"", mode=0o644):
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        add("bin/node", f"#!/bin/sh\necho {reports or version}\n".encode(), 0o755)
        add("lib/node_modules/npm/bin/npm-cli.js", b"// npm\n", 0o755)
        link = tarfile.TarInfo(f"{top}/bin/npm")
        link.type, link.linkname = tarfile.SYMTYPE, "../lib/node_modules/npm/bin/npm-cli.js"
        tar.addfile(link)
        add("include/node/node.h", b"/* headers */\n")
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


class Dist:
    """A fake nodejs.org: serves the index, tarballs and their SHASUMS."""

    def __init__(self, releases: dict[str, bytes], index: list[dict], *, shasum_override=None):
        self.releases, self.index, self.shasum_override = releases, index, shasum_override
        self.fetched: list[str] = []

    def __call__(self, url: str, dest: Path) -> None:
        self.fetched.append(url)
        if url.endswith("/index.json"):
            import json
            dest.write_text(json.dumps(self.index))
            return
        version = url.split("/")[-2]
        name = url.split("/")[-1]
        if name == "SHASUMS256.txt":
            lines = []
            for fname, data in self.releases.items():
                if f"-{version}-" in fname:
                    digest = self.shasum_override or hashlib.sha256(data).hexdigest()
                    lines.append(f"{digest}  {fname}")
            dest.write_text("\n".join(lines) + "\n")
            return
        if name not in self.releases:
            raise OSError(f"404 {url}")
        dest.write_bytes(self.releases[name])


def runner(signature: str = GOOD_SIGNATURE, verify_rc: int = 0):
    """codesign faked; everything else (the fake node) really runs."""
    def run(argv, **kwargs):
        if argv[0] == "/usr/bin/codesign":
            if "--verify" in argv:
                err = "" if verify_rc == 0 else "invalid"
                return subprocess.CompletedProcess(argv, verify_rc, "", err)
            return subprocess.CompletedProcess(argv, 0, "", signature)
        return subprocess.run(argv, capture_output=True, text=True, timeout=10)
    return run


def _entry(version, *, lts="Lithium", files=("osx-arm64-tar", "osx-x64-tar")):
    return {"version": version, "lts": lts, "files": list(files)}


def _dist(*versions, arch="arm64", index=None, **kw):
    releases = {f"node-{v}-darwin-{arch}.tar.gz": _tarball(v, arch) for v in versions}
    return Dist(releases, index or [_entry(v) for v in versions], **kw)


# ── choosing a release ────────────────────────────────────────────────────

class TestChooseRelease:
    def test_newest_lts_with_a_build_for_the_arch(self):
        index = [
            _entry("v27.0.0", lts=False, files=["osx-arm64-tar"]),
            _entry("v26.13.1"),
            _entry("v24.22.3", lts="Krypton"),
        ]
        assert mn.choose_release(index, "arm64").version == "v26.13.1"

    def test_intel_gets_only_what_has_an_x64_build(self):
        """Measured: 27's prerelease has no osx-x64; 26 does (#760)."""
        index = [
            _entry("v28.1.0", lts="Next", files=["osx-arm64-tar"]),
            _entry("v26.13.1"),
        ]
        assert mn.choose_release(index, "x64").version == "v26.13.1"

    def test_skips_releases_below_the_floor_or_without_the_security_fix(self):
        index = [_entry("v26.12.0"), _entry("v22.18.0", lts="Jod"),
                 _entry("v24.22.1", lts="Krypton")]
        # 26.12.0 lacks the Brotli fix (floor 26.12.1); 22.18 is below MIN_NODE.
        assert node_env.security_gap("v26.12.0")
        assert mn.choose_release(index, "arm64").version == "v24.22.1"

    def test_none_when_nothing_qualifies(self):
        assert mn.choose_release([_entry("v26.13.1", files=["linux-x64"])], "arm64") is None

    @pytest.mark.parametrize("machine,arch", [("arm64", "arm64"), ("x86_64", "x64"), ("ppc", None)])
    def test_machine_arch(self, machine, arch):
        assert mn.machine_arch(machine) == arch


# ── installing ────────────────────────────────────────────────────────────

class TestInstall:
    def test_installs_verifies_and_points_current_at_it(self, state):
        dist = _dist("v26.13.1")
        binary = mn.install(mn.Release("v26.13.1", "arm64"), fetch=dist, run=runner())
        assert binary == state / "node" / "current" / "bin" / "node"
        assert os.readlink(state / "node" / "current") == "v26.13.1"
        assert mn.installed_version(run=runner()) == "v26.13.1"
        assert (state / "node" / "v26.13.1" / "bin" / "npm").is_symlink()
        assert not (state / "node" / "v26.13.1" / "include").exists(), "headers are not shipped"

    def test_an_update_replaces_the_old_version(self, state):
        dist = _dist("v26.13.1", "v26.14.0")
        mn.install(mn.Release("v26.13.1", "arm64"), fetch=dist, run=runner())
        mn.install(mn.Release("v26.14.0", "arm64"), fetch=dist, run=runner())
        assert os.readlink(state / "node" / "current") == "v26.14.0"
        assert not (state / "node" / "v26.13.1").exists()

    @pytest.mark.parametrize("break_it,message", [
        (lambda: {"shasum_override": "0" * 64}, "does not match its published SHA-256"),
    ])
    def test_a_bad_checksum_leaves_the_working_node(self, state, break_it, message):
        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())
        bad = _dist("v26.14.0", **break_it())
        with pytest.raises(mn.ManagedNodeError, match=message):
            mn.install(mn.Release("v26.14.0", "arm64"), fetch=bad, run=runner())
        assert os.readlink(state / "node" / "current") == "v26.13.1"
        assert mn.installed_version(run=runner()) == "v26.13.1"

    def test_another_signer_is_refused(self, state):
        with pytest.raises(mn.ManagedNodeError, match="not signed by the Node.js Foundation"):
            mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"),
                       run=runner(signature="TeamIdentifier=SOMEONEELSE\n"))
        assert not (state / "node" / "current").exists()

    def test_a_broken_signature_is_refused(self, state):
        with pytest.raises(mn.ManagedNodeError, match="signature does not verify"):
            mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"),
                       run=runner(verify_rc=1))

    def test_a_node_reporting_another_version_is_refused(self, state):
        dist = Dist({"node-v26.13.1-darwin-arm64.tar.gz": _tarball("v26.13.1", reports="v20.0.0")},
                    [_entry("v26.13.1")])
        with pytest.raises(mn.ManagedNodeError, match="reports v20.0.0"):
            mn.install(mn.Release("v26.13.1", "arm64"), fetch=dist, run=runner())
        assert not (state / "node" / "current").exists()

    def test_a_tarball_not_in_shasums_is_refused(self, state):
        dist = _dist("v26.13.1")

        def no_listing(url, dest):
            if url.endswith("SHASUMS256.txt"):
                dest.write_text("abc  some-other-file.tar.gz\n")
            else:
                dist(url, dest)
        with pytest.raises(mn.ManagedNodeError, match="not listed in SHASUMS256.txt"):
            mn.install(mn.Release("v26.13.1", "arm64"), fetch=no_listing, run=runner())

    @pytest.mark.parametrize("name,link", [
        ("node-v26.13.1-darwin-arm64/../../escape", None),
        ("node-v26.13.1-darwin-arm64/bin/evil", "/etc/passwd"),
        ("node-v26.13.1-darwin-arm64/bin/evil", "../../../outside"),
    ])
    def test_paths_and_links_outside_the_release_are_refused(self, state, name, link):
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
        data = _tarball("v26.13.1", extra=[info])
        dist = Dist({"node-v26.13.1-darwin-arm64.tar.gz": data}, [_entry("v26.13.1")])
        with pytest.raises(mn.ManagedNodeError, match="unexpected path|escapes"):
            mn.install(mn.Release("v26.13.1", "arm64"), fetch=dist, run=runner())
        assert not (state / "node" / "current").exists()

    def test_a_failed_download_is_an_error(self, state):
        with pytest.raises(mn.ManagedNodeError, match="could not download"):
            mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist(), run=runner())

    def test_remove(self, state):
        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())
        assert mn.remove() is True
        assert not (state / "node").exists()
        assert mn.remove() is False


# ── ensure: install, update within the line, or leave alone ──────────────

class TestEnsure:
    def test_installs_when_missing(self, state):
        out = mn.ensure(fetch=_dist("v26.13.1"), run=runner(), arch="arm64")
        assert (out.action, out.version) == ("installed", "v26.13.1")

    def test_updates_within_its_line_only(self, state):
        mn.install(mn.Release("v24.22.1", "arm64"), fetch=_dist("v24.22.1"), run=runner())
        dist = _dist("v24.22.3", "v26.13.1",
                     index=[_entry("v26.13.1"), _entry("v24.22.3", lts="Krypton")])
        out = mn.ensure(fetch=dist, run=runner(), arch="arm64")
        assert (out.action, out.version) == ("updated", "v24.22.3")
        assert out.detail == "from v24.22.1"

    def test_current_when_newest_in_line(self, state):
        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())
        dist = _dist("v26.13.1")
        out = mn.ensure(fetch=dist, run=runner(), arch="arm64")
        assert out.action == "current"
        assert not [u for u in dist.fetched if u.endswith(".tar.gz")], "nothing downloaded"

    def test_an_unreachable_index_keeps_the_working_node(self, state):
        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())

        def offline(url, dest):
            raise OSError("no network")
        out = mn.ensure(fetch=offline, run=runner(), arch="arm64")
        assert (out.action, out.version) == ("failed", "v26.13.1")
        assert "release index" in out.detail

    def test_no_build_for_this_machine(self, state, monkeypatch):
        monkeypatch.setattr(mn, "machine_arch", lambda machine=None: None)
        assert mn.ensure(fetch=_dist(), run=runner()).action == "unsupported"

    def test_not_installing_when_not_allowed(self, state):
        out = mn.ensure(allow_install=False, fetch=_dist("v26.13.1"), run=runner(), arch="arm64")
        assert (out.action, out.version) == ("failed", None)


# ── consumers ─────────────────────────────────────────────────────────────

class TestClientsPreferIt:
    """Through `_real_node_for_clients`: conftest stubs the public name."""

    def _install(self, state):
        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())

    def test_registration_uses_quern_s_node(self, state):
        self._install(state)
        chosen = node_env._real_node_for_clients(sites=[], run=lambda argv, **k: subprocess.run(
            argv, capture_output=True, text=True, timeout=10))
        assert chosen.path == str(mn.current_binary())
        assert (chosen.version, chosen.found_in) == ("v26.13.1", "quern's own Node")

    def test_without_it_the_user_s_node_is_considered(self, state):
        assert node_env._real_node_for_clients(sites=[]) is None

    def test_managed_false_skips_it(self, state):
        self._install(state)
        assert node_env._real_node_for_clients(sites=[], managed=False) is None


class TestSetupStep:
    """`_setup_node`: quern's Node first, the user's as fallback, never brew."""

    @pytest.fixture
    def setup(self, monkeypatch):
        from server.lifecycle import setup

        monkeypatch.setattr(setup, "_brew_install",
                            lambda *a, **k: pytest.fail("Homebrew was offered for Node"))
        return setup

    def test_quern_s_node_is_installed_and_used(self, setup, monkeypatch):
        monkeypatch.setattr(mn, "machine_arch", lambda: "arm64")
        monkeypatch.setattr(mn, "installed_version", lambda: None)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(mn, "ensure", lambda **k: mn.Outcome("v26.13.1", "installed"))
        monkeypatch.setattr(setup, "_probe_node_sites",
                            lambda: pytest.fail("probed the user's node"))
        sites, result = setup._setup_node(after_update=False)
        assert sites is None
        assert result.status == setup.CheckStatus.OK and "quern's own" in result.message

    def test_a_working_one_is_kept_when_the_update_check_fails(self, setup, monkeypatch):
        monkeypatch.setattr(mn, "machine_arch", lambda: "arm64")
        monkeypatch.setattr(mn, "installed_version", lambda: "v26.13.1")
        monkeypatch.setattr(mn, "ensure",
                            lambda **k: mn.Outcome("v26.13.1", "failed", "no network"))
        _, result = setup._setup_node(after_update=True)
        assert result.status == setup.CheckStatus.OK and "no network" in result.detail

    def test_declined_with_a_usable_user_node_is_a_warning(self, setup, monkeypatch):
        monkeypatch.setattr(mn, "machine_arch", lambda: "arm64")
        monkeypatch.setattr(mn, "installed_version", lambda: None)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: False)
        monkeypatch.setattr(setup, "_probe_node_sites", lambda: ["sites"])
        monkeypatch.setattr(setup, "check_node", lambda sites: setup.CheckResult(
            name="Node.js", status=setup.CheckStatus.OK, message="v22.23.4"))
        sites, result = setup._setup_node(after_update=False)
        assert sites == ["sites"]
        assert result.status == setup.CheckStatus.WARNING
        assert "yours" in result.message and "declined" in result.message

    def test_no_node_at_all_is_an_error(self, setup, monkeypatch):
        monkeypatch.setattr(mn, "machine_arch", lambda: "x64")
        monkeypatch.setattr(mn, "installed_version", lambda: None)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(mn, "ensure", lambda **k: mn.Outcome(None, "failed", "no network"))
        monkeypatch.setattr(setup, "_probe_node_sites", lambda: [])
        monkeypatch.setattr(setup, "check_node", lambda sites: setup.CheckResult(
            name="Node.js", status=setup.CheckStatus.MISSING, message="Not installed",
            fixable=True))
        _, result = setup._setup_node(after_update=False)
        assert result.status == setup.CheckStatus.ERROR
        assert "no network" in result.detail and "setup again" in result.detail


class TestBuildUsesIt:
    def test_the_mcp_build_runs_quern_s_npm(self, state, tmp_path, monkeypatch):
        from server import __main__ as entry

        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())
        project = tmp_path / "proj"
        (project / "mcp" / "src").mkdir(parents=True)
        (project / "mcp" / "src" / "index.ts").write_text("// src\n")
        (project / "mcp" / "package.json").write_text("{}\n")
        monkeypatch.setattr(entry, "_find_project_root", lambda: project)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs.get("env")))
            return subprocess.CompletedProcess(argv, 1)
        with patch("subprocess.run", side_effect=fake_run):
            entry._ensure_mcp_built(quiet=True)
        argv, env = calls[0]
        assert argv[0] == str(mn.current_bin_dir() / "npm")
        assert env["PATH"].split(os.pathsep)[0] == str(mn.current_bin_dir())


class TestReported:
    def test_tool_list_classifies_it_as_quern_s(self, state):
        from server.tooling.tool_versions import classify_source

        mn.install(mn.Release("v26.13.1", "arm64"), fetch=_dist("v26.13.1"), run=runner())
        assert classify_source(str(mn.current_binary())) == "quern"
