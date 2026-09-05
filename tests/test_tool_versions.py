"""Reporting which external tools quern is actually using.

The failure this exists for is quiet: two machines comparing `pymobiledevice3`
as a boolean agreed they both had it while being three majors apart, and the
disagreement surfaced instead as one of them reading code the other did not
have. A name is not one thing -- on a single machine it is a library and a
binary at different versions -- so these tests care mostly about not collapsing
things that differ.
"""

from __future__ import annotations

import json

import pytest

from server.device.tool_versions import (
    ToolSite,
    classify_source,
    is_volatile,
    parse_version,
    upgrade_note,
)
from server.lifecycle import setup as setup_mod


class TestParseVersion:
    """Every tool writes its version differently; all of these are real."""

    def test_bare_version(self):
        assert parse_version("9.15.1") == "9.15.1"

    def test_name_then_version(self):
        assert parse_version("idevice_id 1.4.0") == "1.4.0"

    def test_version_buried_in_prose(self):
        assert parse_version("Android Debug Bridge version 1.0.41") == "1.0.41"

    def test_v_prefix_is_dropped(self):
        assert parse_version("v22.22.2") == "22.22.2"

    def test_name_colon_version(self):
        assert parse_version("Mitmproxy: 12.2.3") == "12.2.3"

    def test_a_prerelease_suffix_survives(self):
        assert parse_version("quern 0.14.1-beta.2") == "0.14.1-beta"

    def test_output_with_no_version_reports_none(self):
        assert parse_version("usage: idb [-h] [--log {DEBUG,INFO}]") is None

    def test_the_first_version_line_wins(self):
        assert parse_version("Mitmproxy: 12.2.3\nPython: 3.12.1") == "12.2.3"


class TestClassifySource:
    def test_brew(self):
        assert classify_source("/opt/homebrew/bin/idevice_id") == "brew"

    def test_pipx(self):
        assert classify_source("/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3") == "pipx"

    def test_fnm(self):
        assert classify_source("/Users/x/.local/state/fnm_multishells/1794_17/bin/node") == "fnm"

    def test_android_sdk(self):
        assert classify_source("/Users/x/Library/Android/sdk/platform-tools/adb") == "android-sdk"

    def test_system(self):
        assert classify_source("/usr/local/bin/pymobiledevice3") == "system"

    def test_nothing_to_go_on(self):
        assert classify_source(None) == "unknown"


class TestVolatilePaths:
    """A path that changes between shells identifies where a tool came from,
    and is useless as somewhere to look again."""

    def test_fnm_hands_out_a_per_shell_path(self):
        assert is_volatile("/Users/x/.local/state/fnm_multishells/1794_1787032486225/bin/node")

    def test_a_stable_path_is_not_volatile(self):
        assert not is_volatile("/opt/homebrew/bin/idevice_id")

    def test_no_path_is_not_volatile(self):
        assert not is_volatile(None)


class TestUpgradeNote:
    def _brew(self, **kw):
        return ToolSite(name="libimobiledevice", role="cli", available=True,
                        source="brew", **kw)

    def test_a_dependency_says_an_upgrade_may_be_undone(self):
        note = upgrade_note(self._brew(requested=False))
        assert "arrived as a dependency" in note

    def test_unrecorded_provenance_is_not_reported_as_a_dependency(self):
        """Installs predating the flag carry neither, and treating that as
        "arrived as a dependency" would be inventing a fact."""
        note = upgrade_note(self._brew(requested=None))
        assert "did not record" in note
        assert "arrived as a dependency" not in note

    def test_dependents_are_named_so_the_blast_radius_is_visible(self):
        note = upgrade_note(self._brew(
            requested=True, required_by=["ios-webkit-debug-proxy", "ideviceinstaller"]))
        assert "ideviceinstaller, ios-webkit-debug-proxy" in note

    def test_a_requested_formula_nothing_depends_on_needs_no_note(self):
        assert upgrade_note(self._brew(requested=True)) is None

    def test_non_brew_tools_get_no_brew_advice(self):
        site = ToolSite(name="node", role="cli", available=True, source="fnm",
                        requested=False, required_by=["something"])
        assert upgrade_note(site) is None


class TestDescribe:
    def test_a_missing_tool_says_so(self):
        assert "not found" in ToolSite(name="idb", role="cli").describe()

    def test_the_role_distinguishes_two_installs_of_one_name(self):
        library = ToolSite(name="pymobiledevice3", role="library", available=True,
                           version="11.3.1", source="venv")
        cli = ToolSite(name="pymobiledevice3", role="cli", available=True,
                       version="9.15.1", path="/opt/pipx/bin/pymobiledevice3")
        assert library.describe() != cli.describe()
        assert "library" in library.describe() and "cli" in cli.describe()


# ── the recorded snapshot, and drift against it ──────────────────────────


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    path = tmp_path / "tool-sites.json"
    monkeypatch.setattr(setup_mod, "TOOL_SNAPSHOT", path)
    return path


def _site(name="pymobiledevice3", role="cli", version="9.15.1",
          path="/opt/pipx/bin/pymobiledevice3", **kw):
    return {"name": name, "role": role, "version": version, "path": path,
            "source": "pipx", "available": True, "volatile_path": False,
            "upgrade_note": None, **kw}


def _record(snapshot, sites):
    snapshot.write_text(json.dumps({"recorded_at": "2026-01-01T00:00:00", "sites": sites}))


def test_a_version_change_is_reported(snapshot):
    _record(snapshot, [_site(version="7.7.1")])
    drift = setup_mod._report_drift([_site(version="9.15.1")])
    assert drift and "7.7.1 → 9.15.1" in drift[0].detail


def test_a_move_is_reported(snapshot):
    _record(snapshot, [_site(path="/usr/local/bin/pymobiledevice3")])
    drift = setup_mod._report_drift([_site(path="/opt/pipx/bin/pymobiledevice3")])
    assert drift and "moved" in drift[0].detail


def test_nothing_changed_reports_nothing(snapshot):
    _record(snapshot, [_site()])
    assert setup_mod._report_drift([_site()]) == []


def test_with_no_record_there_is_nothing_to_compare(snapshot):
    assert setup_mod._report_drift([_site()]) == []


def test_a_tool_absent_from_the_record_is_not_drift(snapshot):
    """It was installed since. That is news, but it is not a change to
    something that was working, and calling it drift would cry wolf."""
    _record(snapshot, [_site()])
    drift = setup_mod._report_drift([_site(), _site(name="adb", role="cli")])
    assert drift == []


def test_a_per_shell_path_cannot_report_a_move(snapshot):
    """fnm names node's directory for the pid that asked, so a recorded path
    would differ on every shell and drift would fire constantly."""
    _record(snapshot, [_site(name="node", version="22.22.2", path=None,
                             volatile_path=True)])
    drift = setup_mod._report_drift([
        _site(name="node", version="22.22.2", volatile_path=True,
              path="/x/.local/state/fnm_multishells/999_1/bin/node"),
    ])
    assert drift == []


def test_a_per_shell_tool_still_reports_a_version_change(snapshot):
    """Dropping the path must not drop the tool from the comparison."""
    _record(snapshot, [_site(name="node", version="20.1.0", path=None,
                             volatile_path=True)])
    drift = setup_mod._report_drift([
        _site(name="node", version="22.22.2", volatile_path=True, path=None),
    ])
    assert drift and "20.1.0 → 22.22.2" in drift[0].detail


def test_recording_drops_volatile_paths(snapshot, monkeypatch):
    monkeypatch.setattr(
        setup_mod, "_collect_sites_sync",
        lambda: [_site(name="node", volatile_path=True,
                       path="/x/fnm_multishells/1_2/bin/node")],
    )
    setup_mod.record_tool_sites()
    stored = json.loads(snapshot.read_text())["sites"][0]
    assert stored["path"] is None
    assert stored["version"] == "9.15.1"


def test_the_two_pymobiledevice3_installs_are_tracked_apart(snapshot):
    """The whole point: one name, two installs, and a change to one of them
    must not be hidden by the other agreeing."""
    _record(snapshot, [
        _site(role="library", version="11.3.1", path=None),
        _site(role="cli", version="9.15.1"),
    ])
    drift = setup_mod._report_drift([
        _site(role="library", version="11.3.1", path=None),
        _site(role="cli", version="7.7.1"),
    ])
    assert drift and "9.15.1 → 7.7.1" in drift[0].detail
    assert "library" not in drift[0].detail
