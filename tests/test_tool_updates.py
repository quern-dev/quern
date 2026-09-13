"""Tests for deciding which install sites are behind (#67, stage 2).

Stage 1 built the inventory and stopped there: it could say pymobiledevice3 was
a 11.3.1 library and a 9.15.1 binary at once, and had nothing to say about what
to do with that. This is the deciding half.

The behaviours worth pinning are the ones where a wrong answer is *quiet*:

- a lookup that fails must not read as "up to date" -- on a machine with no brew
  every formula would otherwise get a clean bill of health
- a tool that arrived as a dependency must not be offered, because upgrading it
  directly can be undone by whatever pulled it in
- ...unless it is below a floor, where leaving it is not an option either
- exclusion is decided by `source`, so the same tool from brew on another
  machine is still offered

No test here reaches the network or runs brew: both lookups are injected, the
same way `probe_container` takes `describe_point`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from server.device.tool_updates import (
    CLI_FLOORS,
    format_offer,
    is_behind,
    plan_updates,
    version_tuple,
)
from server.device.tool_versions import ToolSite

#: The home these tests describe, which is deliberately not the one they run on.
#:
#: Building the default path from the real `Path.home()` coupled five tests to
#: whoever ran them: they passed here and failed under any other HOME. Worse,
#: it made both sides of `_pipx_is_global`'s comparison agree by construction,
#: so `resolve()` was the identity and the suite could not tell the buggy
#: version from the fixed one in either direction.
FAKE_HOME = Path("/tmp/quern-tests-home")


@pytest.fixture(autouse=True)
def _home_is_not_this_machine(monkeypatch):
    """Point `Path.home()` at FAKE_HOME for every test in this module."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: FAKE_HOME))


def _site(name="pymobiledevice3", role="cli", source="pipx", version="9.15.1", **kw):
    # `package` defaults to the tool's own name, which is true for most sites.
    # The ones where it is not -- idb/fb-idb, adb/android-platform-tools -- are
    # set explicitly by the tests that care.
    return ToolSite(
        name=name, role=role, source=source, version=version,
        package=kw.pop("package", name), brew_cask=kw.pop("brew_cask", False),
        # Under the user's home by default. The old default, `/opt/<source>/…`,
        # is where pipx puts a *global* install, so every pipx test here was
        # unknowingly describing one -- and asserting the per-user upgrade
        # command for it. Tests that mean a global install now say so.
        available=kw.pop("available", True),
        path=kw.pop("path", f"{FAKE_HOME}/.local/{source}/bin/{name}"),
        **kw,
    )


async def _plan(sites, *, pypi=None, brew=None):
    async def no_pypi(_name):
        return None

    async def no_brew():
        return {}

    return await plan_updates(
        sites,
        pypi=pypi or no_pypi,
        brew=brew or no_brew,
    )


def _by_name(updates, name, role="cli"):
    return next(u for u in updates if u.name == name and u.role == role)


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("a", "b", "expected"), [
    ("9.15.1", "11.3.1", True),      # the real gap this was built for
    ("11.3.1", "9.15.1", False),
    ("1.4.0", "1.4.0", False),
    ("1.0.41", "1.0.41", False),
    ("22.22.2", "22.22.10", True),   # not string ordering
    ("2", "2.0.1", True),
])
def test_is_behind(a, b, expected):
    assert is_behind(a, b) is expected


@pytest.mark.parametrize("bad", [None, "", "unknown", "v-next", "1.2.x"])
def test_unparseable_versions_do_not_compare(bad):
    """Offering an upgrade off a version nobody could parse bumps a tool for no
    reason, so an unreadable version reads as 'not behind' in both directions."""
    assert version_tuple(bad) is None
    assert is_behind(bad, "9.9.9") is False
    assert is_behind("9.9.9", bad) is False


# --------------------------------------------------------------------------
# A failed lookup must never read as up to date
# --------------------------------------------------------------------------


async def test_unreachable_pypi_is_not_a_clean_bill_of_health():
    updates = await _plan([_site()])
    update = _by_name(updates, "pymobiledevice3")
    assert update.action == "unknown"
    assert not update.actionable
    assert "could not check" in update.reason
    assert update.command == []


async def test_absent_brew_is_not_a_clean_bill_of_health():
    """The bug this guards: an empty mapping means 'brew checked, nothing
    outdated'; None means 'brew could not be asked'. Collapsing them reports
    every formula current on a machine with no brew installed."""
    async def no_brew():
        return None

    updates = await _plan([_site(name="libimobiledevice", source="brew", version="1.4.0")],
                          brew=no_brew)
    update = _by_name(updates, "libimobiledevice")
    assert update.action == "unknown"
    assert "could not check" in update.reason


async def test_brew_answering_with_nothing_outdated_does_mean_current():
    """`brew outdated` is exhaustive, so absence from it is a real answer."""
    async def empty_brew():
        return {}

    updates = await _plan([_site(name="libimobiledevice", source="brew", version="1.4.0")],
                          brew=empty_brew)
    update = _by_name(updates, "libimobiledevice")
    assert update.action == "current"
    assert update.latest == "1.4.0"


# --------------------------------------------------------------------------
# The offer itself
# --------------------------------------------------------------------------


async def test_a_behind_pipx_tool_is_offered_with_its_command():
    async def pypi(_name):
        return "11.3.1"

    updates = await _plan([_site()], pypi=pypi)
    update = _by_name(updates, "pymobiledevice3")
    assert update.action == "upgrade_available"
    assert update.current == "9.15.1"
    assert update.latest == "11.3.1"
    assert update.command == ["pipx", "upgrade", "pymobiledevice3"]


async def test_a_current_tool_is_not_offered():
    async def pypi(_name):
        return "9.15.1"

    updates = await _plan([_site()], pypi=pypi)
    assert _by_name(updates, "pymobiledevice3").action == "current"
    assert format_offer(updates) == ""


# --------------------------------------------------------------------------
# Exclusion is decided by source, not by name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "fragment"), [
    ("fnm", "fnm manages node"),
    ("android-sdk", "Android Studio"),
    ("xcode", "update Xcode"),
    ("system", "OS image"),
])
async def test_tools_quern_does_not_manage_are_explained_not_offered(source, fragment):
    updates = await _plan([_site(name="node", source=source, version="22.0.0")])
    update = _by_name(updates, "node")
    assert update.action == "unmanaged"
    assert not update.actionable
    assert fragment in update.reason


async def test_the_same_tool_from_brew_is_offered():
    """The reason adb and node are left alone is where they came from, not what
    they are called. An earlier sketch keyed the exclusions off the name and
    would have refused to update a brew-installed copy on another machine."""
    async def brew():
        return {"adb": "2.0.0"}

    excluded = await _plan([_site(name="adb", source="android-sdk", version="1.0.41")])
    assert _by_name(excluded, "adb").action == "unmanaged"

    offered = await _plan([_site(name="adb", source="brew", version="1.0.41")], brew=brew)
    update = _by_name(offered, "adb")
    assert update.action == "upgrade_available"
    assert update.command == ["brew", "upgrade", "adb"]


async def test_venv_tools_defer_to_quern_update():
    """`quern update` already reinstalls the venv eagerly. A second route to the
    same packages would be two mechanisms racing over one directory."""
    updates = await _plan([_site(name="mitmproxy", source="venv", version="12.2.3")])
    update = _by_name(updates, "mitmproxy")
    assert update.action == "current"
    assert "quern update" in update.reason
    assert update.command == []


# --------------------------------------------------------------------------
# Arriving as a dependency
# --------------------------------------------------------------------------


async def test_a_dependency_install_is_reported_but_not_offered():
    async def brew():
        return {"libimobiledevice": "1.5.0"}

    site = _site(name="libimobiledevice", source="brew", version="1.4.0")
    site.requested = False
    site.required_by = ["ideviceinstaller"]

    update = _by_name(await _plan([site], brew=brew), "libimobiledevice")
    assert update.action == "current"
    assert not update.actionable
    assert "arrived as a dependency" in update.reason
    assert "1.5.0 is available" in update.reason


async def test_a_requested_install_is_offered_even_with_dependents():
    """`required_by` is context for the reader, not a veto. libimobiledevice on
    this machine is requested=True with two dependents and should still update."""
    async def brew():
        return {"libimobiledevice": "1.5.0"}

    site = _site(name="libimobiledevice", source="brew", version="1.4.0")
    site.requested = True
    site.required_by = ["ideviceinstaller", "ios-webkit-debug-proxy"]

    update = _by_name(await _plan([site], brew=brew), "libimobiledevice")
    assert update.action == "upgrade_available"
    assert "also required by" in update.note


async def test_an_unrecorded_install_is_still_offered():
    """brew predates `installed_on_request` for old installs; 'not recorded'
    must not be read as 'arrived as a dependency'."""
    async def brew():
        return {"libimobiledevice": "1.5.0"}

    site = _site(name="libimobiledevice", source="brew", version="1.4.0")
    site.requested = None

    update = _by_name(await _plan([site], brew=brew), "libimobiledevice")
    assert update.action == "upgrade_available"
    assert "did not record" in update.note


# --------------------------------------------------------------------------
# Floors escalate, and override the dependency exemption
# --------------------------------------------------------------------------


async def test_no_cli_floors_are_declared_unverified():
    """Guards the finding, not the code. Every floor quern declared was a `>=`
    that nothing had tested; adding an unverified CLI floor here would repeat
    exactly that. A new entry must arrive with a comment saying what breaks
    below it and where that was measured."""
    assert CLI_FLOORS == {}, (
        "adding a CLI floor is a decision -- document what breaks below it "
        "and at which versions that was verified"
    )


async def test_below_floor_escalates_to_required(monkeypatch):
    async def pypi(_name):
        return "11.3.1"

    monkeypatch.setitem(CLI_FLOORS, ("pymobiledevice3", "cli"), "10.0")
    update = _by_name(await _plan([_site()], pypi=pypi), "pymobiledevice3")
    assert update.action == "upgrade_required"
    assert "below the 10.0" in update.reason


async def test_a_floor_overrides_the_dependency_exemption(monkeypatch):
    """Something else having installed a tool does not make a broken version
    acceptable -- the exemption is for tools that merely aren't newest."""
    async def brew():
        return {"libimobiledevice": "2.0.0"}

    monkeypatch.setitem(CLI_FLOORS, ("libimobiledevice", "cli"), "1.9")
    site = _site(name="libimobiledevice", source="brew", version="1.4.0")
    site.requested = False
    site.required_by = ["ideviceinstaller"]

    update = _by_name(await _plan([site], brew=brew), "libimobiledevice")
    assert update.action == "upgrade_required"
    assert update.note and "arrived as a dependency" in update.note


async def test_a_tool_above_its_floor_but_behind_latest_is_still_offered(monkeypatch):
    """The floor is not an excuse to sit on an old version -- it only decides
    how urgent the offer is."""
    async def pypi(_name):
        return "11.3.1"

    monkeypatch.setitem(CLI_FLOORS, ("pymobiledevice3", "cli"), "9.0")
    update = _by_name(await _plan([_site()], pypi=pypi), "pymobiledevice3")
    assert update.action == "upgrade_available"


# --------------------------------------------------------------------------
# Missing tools, and the rendered block
# --------------------------------------------------------------------------


async def test_a_missing_tool_points_at_setup():
    update = _by_name(await _plan([_site(available=False, version=None)]), "pymobiledevice3")
    assert update.action == "unknown"
    assert "quern setup" in update.reason


async def test_the_offer_carries_every_command_and_flags_required(monkeypatch):
    async def pypi(_name):
        return "11.3.1"

    async def brew():
        return {"libimobiledevice": "1.5.0"}

    monkeypatch.setitem(CLI_FLOORS, ("libimobiledevice", "cli"), "1.5")
    lib = _site(name="libimobiledevice", source="brew", version="1.4.0")
    lib.requested = True

    text = format_offer(await _plan([_site(), lib], pypi=pypi, brew=brew))
    assert "pipx upgrade pymobiledevice3" in text
    assert "brew upgrade libimobiledevice" in text
    assert "9.15.1 → 11.3.1" in text
    # Required sorts above merely-available so the urgent one is read first.
    assert text.index("libimobiledevice") < text.index("pymobiledevice3")
    assert "! libimobiledevice" in text


async def test_nothing_to_do_renders_nothing():
    """An update run with no tool work must print no tool section at all."""
    updates = await _plan([_site(name="mitmproxy", source="venv", version="12.2.3")])
    assert format_offer(updates) == ""


# --------------------------------------------------------------------------
# Wiring into `quern update`
# --------------------------------------------------------------------------
#
# The planner being correct is worth nothing if the updater never calls it, or
# calls it in a mode nobody asked for. Both failures are silent, so both are
# pinned here rather than left to the module tests above.


@pytest.fixture
def stale_tool(monkeypatch):
    """One actionable upgrade, with no network, no brew and no real sites."""
    from server.device import tool_updates

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [
            tool_updates.ToolUpdate(
                name="pymobiledevice3", role="cli", action="upgrade_available",
                current="9.15.1", latest="11.3.1",
                command=["pipx", "upgrade", "pymobiledevice3"],
                reason="newer release available (11.3.1)",
            ),
        ]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)

    ran: list[list[str]] = []
    monkeypatch.setattr(
        "server.lifecycle.updater.subprocess.run",
        lambda cmd, **kw: ran.append(cmd) or __import__("types").SimpleNamespace(returncode=0),
    )
    return ran


def test_reporting_never_runs_the_upgrade(stale_tool, capsys):
    """The default must not touch pipx or brew. These commands change state for
    every other consumer on the machine, so running them as a side effect of
    updating quern is not a decision to make on the caller's behalf."""
    from server.lifecycle.updater import _report_tool_updates

    _report_tool_updates(apply=False)
    out = capsys.readouterr().out
    assert "pipx upgrade pymobiledevice3" in out
    assert "quern update --tools" in out
    assert stale_tool == [], "reporting must not execute anything"


def test_applying_runs_each_command(stale_tool, capsys):
    from server.lifecycle.updater import _report_tool_updates

    _report_tool_updates(apply=True)
    assert stale_tool == [["pipx", "upgrade", "pymobiledevice3"]]


def test_a_broken_version_check_does_not_fail_the_update(monkeypatch, capsys):
    """A version lookup is advisory. Letting it abort `quern update` would make
    an offline machine unable to update quern itself."""
    async def boom():
        raise RuntimeError("no network")

    monkeypatch.setattr("server.device.tool_versions.collect_sites", boom)

    from server.lifecycle.updater import _report_tool_updates

    _report_tool_updates(apply=True)
    assert "could not check external tool versions" in capsys.readouterr().out


def test_already_up_to_date_still_checks_tools(monkeypatch):
    """External tools age independently of quern. Gating the check on quern
    having an update means learning about a two-major-old binary only when
    something unrelated happens to ship."""
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_find_project_root", lambda: __import__("pathlib").Path("/tmp"))
    monkeypatch.setattr(updater, "_is_git_install", lambda _root: True)
    monkeypatch.setattr(updater, "_update_via_git", lambda _root: 2)  # already current

    called: list[bool] = []

    def record(apply=False):
        called.append(apply)
        return True

    monkeypatch.setattr(updater, "_report_tool_updates", record)

    assert updater.run_update() == 0
    assert called == [False], "the up-to-date path skipped the tool check"


def test_the_tools_flag_reaches_the_updater(monkeypatch):
    """Pin the argv wiring: a correct planner behind a flag nobody parses is
    the same as no planner."""
    import inspect

    from server import __main__ as entry

    source = inspect.getsource(entry)
    assert 'run_update(apply_tools="--tools" in sys.argv[2:])' in source


# --------------------------------------------------------------------------
# brew_outdated itself
# --------------------------------------------------------------------------
#
# The planner tests above inject a fake brew, so they pin how a None is
# *handled* and say nothing about whether the real function ever produces one.
# A mutation making brew_outdated return {} on failure passed all 37 of them --
# which is precisely the false all-clear the None exists to prevent, sitting in
# production code with a green suite over it.


@pytest.fixture
def fake_brew_run(monkeypatch):
    def install(code: int, stdout: str):
        async def _run(_args, timeout):  # noqa: ARG001
            return code, stdout

        monkeypatch.setattr("server.device.tool_updates._run", _run)

    return install


async def test_brew_outdated_returns_none_when_brew_is_missing(fake_brew_run):
    """`_run` reports a non-zero code for a binary that does not exist, which is
    the no-homebrew machine. That must not read as 'nothing is outdated'."""
    from server.device.tool_updates import brew_outdated

    fake_brew_run(1, "")
    assert await brew_outdated() is None


async def test_brew_outdated_returns_none_on_unparseable_output(fake_brew_run):
    from server.device.tool_updates import brew_outdated

    fake_brew_run(0, "not json at all")
    assert await brew_outdated() is None


async def test_brew_outdated_distinguishes_nothing_outdated_from_failure(fake_brew_run):
    """A successful call with an empty formulae list is a real answer."""
    from server.device.tool_updates import brew_outdated

    fake_brew_run(0, '{"formulae": [], "casks": []}')
    assert await brew_outdated() == {}


async def test_brew_outdated_maps_name_to_current_version(fake_brew_run):
    from server.device.tool_updates import brew_outdated

    fake_brew_run(0, '{"formulae": [{"name": "libimobiledevice", '
                     '"installed_versions": ["1.4.0"], "current_version": "1.5.0"}]}')
    assert await brew_outdated() == {"libimobiledevice": "1.5.0"}


async def test_brew_outdated_skips_entries_missing_a_version(fake_brew_run):
    """Guards against a partial entry becoming a None latest, which would read
    downstream as 'up to date at None'."""
    from server.device.tool_updates import brew_outdated

    fake_brew_run(0, '{"formulae": [{"name": "x"}, {"current_version": "2.0"}]}')
    assert await brew_outdated() == {}


# --------------------------------------------------------------------------
# The doctor report
# --------------------------------------------------------------------------
#
# `format_offer` and `format_report` answer different questions and must not be
# collapsed. The offer is "what should I run", so it hides everything healthy.
# The report is "why does this machine differ from that one", so hiding the
# healthy entries is precisely the failure -- two machines comparing only their
# problems agree they have none while running three-major-apart copies.


async def test_the_report_shows_brew_dependents_for_a_tool_needing_no_action():
    """The reason this exists. libimobiledevice being current is not the
    interesting part; that two other formulae depend on it is, because that is
    what turns a later upgrade into a decision rather than a command."""
    from server.device.tool_updates import format_report

    async def brew():
        return {}

    site = _site(name="libimobiledevice", source="brew", version="1.4.0")
    site.requested = True
    site.required_by = ["ideviceinstaller", "ios-webkit-debug-proxy"]

    updates = await _plan([site], brew=brew)
    assert _by_name(updates, "libimobiledevice").action == "current"

    text = format_report(updates)
    assert "ideviceinstaller" in text
    assert "ios-webkit-debug-proxy" in text
    # The offer, by contrast, has nothing to say about it.
    assert format_offer(updates) == ""


async def test_the_report_lists_every_site_including_unmanaged():
    from server.device.tool_updates import format_report

    async def pypi(_name):
        return "11.3.1"

    text = format_report(await _plan([
        _site(),
        _site(name="node", source="fnm", version="22.22.2"),
        _site(name="adb", source="android-sdk", version="1.0.41"),
        _site(name="mitmproxy", source="venv", version="12.2.3"),
    ], pypi=pypi))

    for name in ("pymobiledevice3", "node", "adb", "mitmproxy"):
        assert name in text
    assert "fnm manages node" in text
    assert "Android Studio" in text


async def test_the_report_sorts_actionable_first():
    from server.device.tool_updates import format_report

    async def pypi(_name):
        return "11.3.1"

    text = format_report(await _plan([
        _site(name="node", source="fnm", version="22.22.2"),
        _site(),
    ], pypi=pypi))
    assert text.index("pymobiledevice3") < text.index("node")


async def test_the_report_carries_the_source_that_decided_the_action():
    """Without it a reader sees 'not managed' and goes looking for a quern
    setting to change, rather than for Android Studio."""
    from server.device.tool_updates import format_report

    text = format_report(await _plan([_site(name="adb", source="android-sdk", version="1.0.41")]))
    assert "(android-sdk)" in text


def test_the_report_survives_having_nothing_to_report():
    from server.device.tool_updates import format_report

    assert "none detected" in format_report([])


def test_doctor_reports_without_running_anything(monkeypatch, capsys):
    """Doctor is documented as read-only diagnostics. It prints the upgrade
    commands; `quern update --tools` is the only thing that runs them."""
    import subprocess as sp

    from server.device import tool_updates

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [tool_updates.ToolUpdate(
            name="pymobiledevice3", role="cli", action="upgrade_available",
            current="9.15.1", latest="11.3.1", source="pipx",
            command=["pipx", "upgrade", "pymobiledevice3"],
            reason="newer release available (11.3.1)",
        )]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)

    ran = []
    monkeypatch.setattr(sp, "run", lambda *a, **k: ran.append(a))

    from server.main import _report_external_tools

    _report_external_tools()
    out = capsys.readouterr().out
    assert "pipx upgrade pymobiledevice3" in out
    assert ran == [], "doctor must not execute upgrade commands"


def test_a_broken_check_does_not_break_doctor(monkeypatch, capsys):
    """A machine where this raises is exactly one someone is running doctor on."""
    async def boom():
        raise RuntimeError("brew exploded")

    monkeypatch.setattr("server.device.tool_versions.collect_sites", boom)

    from server.main import _report_external_tools

    _report_external_tools()
    assert "could not be checked" in capsys.readouterr().out


# --------------------------------------------------------------------------
# brew provenance is fetched for every brew site
# --------------------------------------------------------------------------


async def test_provenance_is_attached_to_every_brew_site_not_just_one():
    """It used to be fetched for libimobiledevice alone, because that was the
    only brew install on the machine it was written on. mitmproxy, adb and
    pymobiledevice3 are all brew-installable, and on a machine that installed
    them that way the field that decides whether an upgrade is safe to offer was
    simply absent."""
    from server.device import tool_versions

    asked: list[str] = []

    async def fake_provenance(formula):
        asked.append(formula)
        return True, [f"{formula}-consumer"]

    import server.device.tool_versions as tv

    original = tv.brew_provenance
    tv.brew_provenance = fake_provenance
    try:
        sites = [
            _site(name="libimobiledevice", source="brew"),
            _site(name="mitmproxy", source="brew"),
            _site(name="node", source="fnm"),
            _site(name="gone", source="brew", available=False),
        ]
        await tool_versions._attach_brew_provenance(sites)
    finally:
        tv.brew_provenance = original

    assert sorted(asked) == ["libimobiledevice", "mitmproxy"]
    assert sites[1].required_by == ["mitmproxy-consumer"]
    assert sites[2].requested is None, "non-brew sites must be left alone"


async def test_one_unreadable_formula_does_not_lose_the_others():
    """Best effort per formula: `brew info` failing on one must not cost the
    provenance of every other, which a bare gather would do."""
    from server.device import tool_versions

    async def flaky(formula):
        if formula == "broken":
            raise RuntimeError("brew info exploded")
        return True, ["consumer"]

    import server.device.tool_versions as tv

    original = tv.brew_provenance
    tv.brew_provenance = flaky
    try:
        sites = [_site(name="broken", source="brew"), _site(name="fine", source="brew")]
        await tool_versions._attach_brew_provenance(sites)
    finally:
        tv.brew_provenance = original

    assert sites[0].requested is None
    assert sites[1].required_by == ["consumer"]


# --------------------------------------------------------------------------
# `doctor --fix` and the boundary it does not cross
# --------------------------------------------------------------------------
#
# `--fix` is scoped to "exactly what server startup runs" -- the venv, nothing
# else. A pipx or brew upgrade changes state for every consumer on the machine,
# further out of scope than `quern setup`, which `--fix` already refuses to run.
#
# The risk is not that it does too much; it is that it stays quiet. "--fix:
# nothing to do" printed above a tool marked as behind reads as "and nothing to
# do about that either", which is the one way this section can mislead.


@pytest.fixture
def doctor_with_stale_tool(monkeypatch):
    from server.device import tool_updates

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [tool_updates.ToolUpdate(
            name="pymobiledevice3", role="cli", action="upgrade_available",
            current="9.15.1", latest="11.3.1", source="pipx",
            command=["pipx", "upgrade", "pymobiledevice3"],
            reason="newer release available (11.3.1)",
        )]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)

    import subprocess as sp

    ran: list = []
    monkeypatch.setattr(sp, "run", lambda *a, **k: ran.append(a))
    return ran


def test_fix_does_not_upgrade_external_tools(doctor_with_stale_tool, capsys):
    from server.main import _report_external_tools

    _report_external_tools(fix=True)
    capsys.readouterr()
    assert doctor_with_stale_tool == [], (
        "--fix must not run pipx or brew: those change state for every consumer "
        "on the machine, not just quern"
    )


def test_fix_says_it_cannot_help_rather_than_staying_quiet(doctor_with_stale_tool, capsys):
    from server.main import _report_external_tools

    _report_external_tools(fix=True)
    out = capsys.readouterr().out
    assert "--fix does not upgrade external tools" in out
    assert "quern update --tools" in out


def test_without_fix_there_is_no_disclaimer(doctor_with_stale_tool, capsys):
    """The note answers a question only `--fix` raises. Printing it always would
    be noise on the read-only path."""
    from server.main import _report_external_tools

    _report_external_tools(fix=False)
    assert "--fix does not upgrade" not in capsys.readouterr().out


def test_fix_is_silent_when_every_tool_is_current(monkeypatch, capsys):
    """No disclaimer when there is nothing it could have fixed anyway."""
    from server.device import tool_updates

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [tool_updates.ToolUpdate(
            name="mitmproxy", role="cli", action="current", current="12.2.3",
            source="venv", reason="up to date at 12.2.3",
        )]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)

    from server.main import _report_external_tools

    _report_external_tools(fix=True)
    assert "--fix does not upgrade" not in capsys.readouterr().out


def test_doctor_passes_the_fix_flag_through(monkeypatch):
    """Pin the wiring: the flag reached `_report_python_deps` and not this
    section, which is how the inconsistency arose in the first place.

    Asserted by calling doctor rather than by reading its source. The earlier
    version matched a literal call expression, which made every refactor of
    `_cmd_doctor` look like a regression while a genuinely dropped flag inside
    an unchanged-looking line would have passed.
    """
    import argparse

    import pytest

    from server import main

    seen: dict[str, bool] = {}
    monkeypatch.setattr(main, "_report_python_deps", lambda fix: seen.update(deps=fix))
    monkeypatch.setattr(main, "_report_external_tools", lambda fix: seen.update(external=fix))
    monkeypatch.setattr(main, "_report_service_health", lambda fix: seen.update(health=fix))
    monkeypatch.setattr(main, "read_state", lambda: None)

    with pytest.raises(SystemExit):
        main._cmd_doctor(argparse.Namespace(fix=True))

    assert seen == {"deps": True, "external": True, "health": True}


# --------------------------------------------------------------------------
# Package identity: what the manager calls a tool is not what quern calls it
# --------------------------------------------------------------------------
#
# `collect_sites` records `idb` from the `fb-idb` distribution and `adb` from
# the `android-platform-tools` cask. Planning off `site.name` therefore queried
# the wrong PyPI project and emitted an upgrade command for a package that does
# not exist -- while looking entirely plausible in the output.


async def test_the_pypi_lookup_uses_the_distribution_not_the_tool_name():
    asked: list[str] = []

    async def pypi(name):
        asked.append(name)
        return "1.9.0"

    site = _site(name="idb", package="fb-idb", source="pipx", version="1.5.2")
    update = _by_name(await _plan([site], pypi=pypi), "idb")

    assert asked == ["fb-idb"], "queried PyPI for the wrong project"
    assert update.command == ["pipx", "upgrade", "fb-idb"]


async def test_a_cask_is_upgraded_with_the_cask_flag():
    """There is no `adb` formula; brew ships the binary in a cask, so
    `brew upgrade adb` fails outright."""
    async def brew():
        return {"android-platform-tools": "36.0.0"}

    site = _site(name="adb", package="android-platform-tools", brew_cask=True,
                 source="brew", version="1.0.41")
    update = _by_name(await _plan([site], brew=brew), "adb")

    assert update.action == "upgrade_available"
    assert update.command == ["brew", "upgrade", "--cask", "android-platform-tools"]


async def test_a_formula_is_upgraded_without_the_cask_flag():
    async def brew():
        return {"libimobiledevice": "1.5.0"}

    site = _site(name="libimobiledevice", source="brew", version="1.4.0")
    update = _by_name(await _plan([site], brew=brew), "libimobiledevice")
    assert update.command == ["brew", "upgrade", "libimobiledevice"]


async def test_a_site_without_an_identity_is_unknown_not_current():
    """Planning it as `current` would report a tool as up to date on the
    strength of a lookup that never happened."""
    site = _site(package=None)
    update = _by_name(await _plan([site]), "pymobiledevice3")
    assert update.action == "unknown"
    assert not update.actionable
    assert "no package identity" in update.reason
    assert update.command == []


async def test_brew_outdated_reads_casks_as_well_as_formulae(fake_brew_run):
    """Reading only `formulae` reported an outdated cask as up to date."""
    from server.device.tool_updates import brew_outdated

    payload = json.dumps({
        "formulae": [{"name": "libimobiledevice", "current_version": "1.5.0"}],
        # Casks report `name` as a list of tokens, not a string.
        "casks": [{"name": ["android-platform-tools"], "current_version": "36.0.0"}],
    })
    fake_brew_run(0, payload)
    assert await brew_outdated() == {
        "libimobiledevice": "1.5.0",
        "android-platform-tools": "36.0.0",
    }


async def test_provenance_asks_brew_about_the_formula_name():
    """`brew info adb` is not a thing. Asking under the tool's nickname returned
    no provenance, which reads downstream as 'not recorded'."""
    import server.device.tool_versions as tv
    from server.device import tool_versions

    asked: list[str] = []

    async def fake_provenance(formula):
        asked.append(formula)
        return True, []

    original = tv.brew_provenance
    tv.brew_provenance = fake_provenance
    try:
        await tool_versions._attach_brew_provenance(
            [_site(name="adb", package="android-platform-tools", source="brew")])
    finally:
        tv.brew_provenance = original

    assert asked == ["android-platform-tools"]


# --------------------------------------------------------------------------
# `--tools` is an instruction, so its failures have to be visible
# --------------------------------------------------------------------------


def test_a_failed_upgrade_makes_update_exit_nonzero(monkeypatch, capsys):
    """Printing "failed" while the process exits 0 tells a script the opposite
    of what happened."""
    from server.device import tool_updates
    from server.lifecycle import updater

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [tool_updates.ToolUpdate(
            name="pymobiledevice3", role="cli", action="upgrade_available",
            current="9.15.1", latest="11.3.1", source="pipx",
            command=["pipx", "upgrade", "pymobiledevice3"], reason="newer",
        )]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)
    monkeypatch.setattr(
        updater.subprocess, "run",
        lambda *a, **k: __import__("types").SimpleNamespace(returncode=1))

    assert updater._report_tool_updates(apply=True) is False
    assert "1 tool upgrade(s) failed" in capsys.readouterr().out


def test_a_raised_upgrade_failure_also_counts(monkeypatch, capsys):
    from server.device import tool_updates
    from server.lifecycle import updater

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [tool_updates.ToolUpdate(
            name="pymobiledevice3", role="cli", action="upgrade_available",
            current="9.15.1", latest="11.3.1", source="pipx",
            command=["pipx", "upgrade", "pymobiledevice3"], reason="newer",
        )]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)

    def boom(*_a, **_k):
        raise PermissionError("pipx not executable")

    monkeypatch.setattr(updater.subprocess, "run", boom)
    assert updater._report_tool_updates(apply=True) is False


def test_reporting_alone_is_never_a_failure(monkeypatch):
    """A stale tool is information. Only `--tools` turns it into an instruction
    that can fail."""
    from server.device import tool_updates
    from server.lifecycle import updater

    async def fake_sites():
        return [_site()]

    async def fake_plan(_sites, **_kw):
        return [tool_updates.ToolUpdate(
            name="pymobiledevice3", role="cli", action="upgrade_available",
            current="9.15.1", latest="11.3.1", source="pipx",
            command=["pipx", "upgrade", "pymobiledevice3"], reason="newer",
        )]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)
    assert updater._report_tool_updates(apply=False) is True


def test_doctor_reports_external_tools_when_no_device_tools_are_found(monkeypatch):
    """The branch where it matters most: a missing device controller often *is*
    a missing or stale external tool. Skipping the report there also made the
    README's description of `quern doctor` false."""
    import inspect

    from server import main

    source = inspect.getsource(main._cmd_doctor)
    empty_branch = source.split("if not tools:")[1].split("sys.exit(0)")[0]
    assert "_report_external_tools" in empty_branch


# --------------------------------------------------------------------------
# Every rebuild step has to reach the exit code
# --------------------------------------------------------------------------
#
# `_rebuild_and_restart` used to return only the dependency result. A failed MCP
# build was a warning, `run_setup`'s exit code was discarded outright, and the
# restart's was never read -- so `quern update` could exit 0 having left the MCP
# tools stale or the server down.


@pytest.fixture
def rebuild(monkeypatch, tmp_path):
    """Drive each step of _rebuild_and_restart independently."""
    from server.lifecycle import updater

    state = {"deps": True, "mcp": True, "setup": 0, "restart": 0, "running": True}

    monkeypatch.setattr("server.__main__._ensure_python_deps",
                        lambda **_kw: state["deps"])
    monkeypatch.setattr("server.__main__._ensure_mcp_built", lambda **_kw: state["mcp"])
    monkeypatch.setattr("server.lifecycle.setup.run_setup", lambda: state["setup"])
    monkeypatch.setattr("server.lifecycle.state.read_state",
                        lambda: {"server_port": 9100} if state["running"] else None)
    monkeypatch.setattr("server.lifecycle.state.is_server_healthy", lambda _p: state["running"])

    def fake_run(_cmd, **_kw):
        if isinstance(state["restart"], BaseException):
            raise state["restart"]
        return SimpleNamespace(returncode=state["restart"])

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    state["_run"] = lambda: updater._rebuild_and_restart(tmp_path)
    return state


def test_everything_working_reports_no_failures(rebuild):
    assert rebuild["_run"]() == []


def test_a_failed_mcp_build_is_no_longer_only_a_warning(rebuild):
    rebuild["mcp"] = False
    assert rebuild["_run"]() == ["MCP build"]


def test_a_nonzero_setup_is_reported(rebuild):
    rebuild["setup"] = 1
    assert rebuild["_run"]() == ["setup"]


def test_a_failed_restart_is_reported(rebuild):
    """Leaving the server down and exiting 0 is the worst of these: the update
    looks clean and nothing is listening."""
    rebuild["restart"] = 1
    assert rebuild["_run"]() == ["restart"]


def test_a_restart_that_raises_does_not_crash_the_update(rebuild):
    """The restart carries a 30s timeout whose exception was uncaught, so an
    update that hung on restart crashed instead of reporting."""
    rebuild["restart"] = subprocess.TimeoutExpired(cmd="restart", timeout=30)
    assert rebuild["_run"]() == ["restart"]


def test_later_steps_still_run_after_an_early_failure(rebuild):
    """An update that cannot build the MCP wrapper should still reconcile the
    venv, so failures accumulate rather than short-circuiting."""
    rebuild["deps"] = False
    rebuild["mcp"] = False
    rebuild["setup"] = 1
    rebuild["restart"] = 1
    assert rebuild["_run"]() == ["dependencies", "MCP build", "setup", "restart"]


def test_no_restart_is_attempted_when_the_server_is_stopped(rebuild):
    rebuild["running"] = False
    rebuild["restart"] = 1          # would fail if it were attempted
    assert rebuild["_run"]() == []


def test_run_update_names_the_step_that_failed(monkeypatch, capsys):
    """"dependencies could not be installed" after a failed restart sends the
    reader to the wrong place."""
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_find_project_root", lambda: __import__("pathlib").Path("/tmp"))
    monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
    monkeypatch.setattr(updater, "_update_via_git", lambda _r: 0)
    monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _p: ["restart"])
    # run_update now reports external tools before checking rebuild failures,
    # so without this the test would query PyPI and shell out to brew.
    monkeypatch.setattr(updater, "_report_tool_updates", lambda apply=False: True)

    assert updater.run_update() == 1
    out = capsys.readouterr().out
    assert "restart failed" in out
    assert "dependencies could not be installed" not in out


def test_external_tools_are_reported_even_when_the_rebuild_failed(monkeypatch, capsys):
    """A failed rebuild used to return before the tool report, so `--tools`
    was silently dropped even though the caller asked for it explicitly.

    External tools live outside the project and do not depend on the rebuild
    having worked, so the two are independent.
    """
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_find_project_root", lambda: __import__("pathlib").Path("/tmp"))
    monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
    monkeypatch.setattr(updater, "_update_via_git", lambda _r: 0)
    monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _p: ["dependencies"])

    asked: list[bool] = []

    def record(apply=False):
        asked.append(apply)
        return True

    monkeypatch.setattr(updater, "_report_tool_updates", record)

    assert updater.run_update(apply_tools=True) == 1
    assert asked == [True], "--tools was dropped when the rebuild failed"


def test_a_raised_mcp_build_does_not_crash_the_update(rebuild):
    """_ensure_mcp_built shells out to npm twice with timeouts and catches
    neither, so a machine without npm raised straight through the rebuild."""
    import server.__main__ as entry

    def boom(**_kw):
        raise FileNotFoundError("npm not found")

    original = entry._ensure_mcp_built
    entry._ensure_mcp_built = boom
    try:
        assert rebuild["_run"]() == ["MCP build"]
    finally:
        entry._ensure_mcp_built = original


def test_an_mcp_build_timeout_is_recorded(rebuild):
    import server.__main__ as entry

    def boom(**_kw):
        raise subprocess.TimeoutExpired(cmd="npm run build", timeout=60)

    original = entry._ensure_mcp_built
    entry._ensure_mcp_built = boom
    try:
        assert rebuild["_run"]() == ["MCP build"]
    finally:
        entry._ensure_mcp_built = original


# --------------------------------------------------------------------------
# A global pipx install is upgraded with a different command
# --------------------------------------------------------------------------
#
# `pipx upgrade <name>` only ever looks in the per-user PIPX_HOME. Run against
# a globally-installed tool it fails with "Package is not installed. Expected
# to find ~/.local/pipx/venvs/<name>, but it does not exist" -- naming a path
# the user never chose, for a tool that is plainly installed and working.
#
# Not an edge case here: setup steers machines whose home is an external volume
# towards `sudo pipx install --global`, because the tunneld LaunchDaemon starts
# at boot and cannot reach a volume that mounts at login.


async def test_a_global_pipx_install_is_upgraded_with_sudo():
    async def pypi(_name):
        return "11.12.4"

    site = _site(source="pipx", version="9.15.1",
                 path="/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3")
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.command == ["sudo", "pipx", "upgrade", "--global", "pymobiledevice3"]
    assert update.needs_root is True


async def test_a_per_user_pipx_install_needs_no_password():
    async def pypi(_name):
        return "11.12.4"

    home = str(Path.home())
    site = _site(source="pipx", version="9.15.1",
                 path=f"{home}/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3")
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.command == ["pipx", "upgrade", "pymobiledevice3"]
    assert update.needs_root is False


async def test_the_newer_per_user_pipx_layout_is_also_per_user():
    """pipx 1.5 moved PIPX_HOME on macOS to ~/Library/Application Support/pipx.
    Deciding by location rather than by a list of known directories is what
    makes that a non-event."""
    async def pypi(_name):
        return "11.12.4"

    home = str(Path.home())
    site = _site(
        source="pipx", version="9.15.1",
        path=f"{home}/Library/Application Support/pipx/venvs/pymobiledevice3"
             "/bin/pymobiledevice3",
    )
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is False


async def test_a_site_with_no_path_guesses_the_command_that_needs_no_password():
    """Cannot tell means do not ask for credentials. The per-user command fails
    loudly; the sudo one prompts for a password on the strength of something we
    could not read."""
    async def pypi(_name):
        return "11.12.4"

    site = _site(source="pipx", version="9.15.1", path=None)
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is False


@pytest.mark.parametrize("raised", [OSError(5, "I/O error"), RuntimeError("loop")])
@pytest.mark.asyncio
async def test_an_unreadable_path_guesses_the_command_that_needs_no_password(raised):
    """The branch for a path `resolve()` cannot read, which had no coverage.

    The `path=None` test above describes this case in its docstring and does
    not reach it: the `if not site.path` guard short-circuits before the `try`.

    Both exception types, injected rather than provoked. Which one a real
    failure produces is a Python-version detail -- a symlink loop raises
    `RuntimeError` on 3.12 and resolves without complaint on 3.11 and 3.13 --
    and an earlier version of this test pinned 3.12's answer and failed CI on
    the other two. What the code actually promises is version-independent: when
    it cannot tell where an install lives, it must not ask for a password.
    """
    async def pypi(_name):
        return "11.12.4"

    def cannot_read(_self, *args, **kwargs):
        raise raised

    site = _site(source="pipx", version="9.15.1")
    with patch.object(Path, "resolve", cannot_read):
        update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is False
    assert "sudo" not in update.command


@pytest.mark.asyncio
async def test_a_symlink_loop_does_not_crash_the_plan(tmp_path):
    """However this Python reports a symlink loop, the plan survives it.

    Deliberately asserts nothing about the exception type, or that there is
    one. On 3.12 this exercises the handler; on 3.11 and 3.13 `resolve()`
    returns the path unchanged and it exercises the ordinary route. Either way
    the thing that must not happen -- an exception escaping into the
    tool-update plan, which is what it did before the handler was widened --
    does not.
    """
    async def pypi(_name):
        return "11.12.4"

    loop = tmp_path / "loop"
    loop.symlink_to(tmp_path / "loop2")
    (tmp_path / "loop2").symlink_to(loop)

    site = _site(source="pipx", version="9.15.1", path=str(loop / "bin" / "pmd3"))
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update is not None


@pytest.fixture
def globally_installed_tool(monkeypatch):
    """A tool installed with `pipx install --global`, needing sudo to upgrade."""
    from server.device import tool_updates

    async def fake_sites():
        return []

    async def fake_plan(sites, **kw):
        return [
            tool_updates.ToolUpdate(
                name="pymobiledevice3", role="cli", action="upgrade_available",
                current="9.15.1", latest="11.12.4",
                command=["sudo", "pipx", "upgrade", "--global", "pymobiledevice3"],
                needs_root=True,
                reason="newer release available (11.12.4)",
            ),
        ]

    monkeypatch.setattr("server.device.tool_versions.collect_sites", fake_sites)
    monkeypatch.setattr("server.device.tool_updates.plan_updates", fake_plan)

    ran: list[list[str]] = []
    monkeypatch.setattr(
        "server.lifecycle.updater.subprocess.run",
        lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0),
    )
    return ran


def test_a_sudo_upgrade_is_not_attempted_with_nowhere_to_ask(
    globally_installed_tool, monkeypatch, capsys
):
    """`quern update` is reachable from the menu bar, which has no controlling
    terminal. sudo there either hangs or fails with a message about a tty,
    neither of which tells the reader what to do."""
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_can_ask_for_a_password", lambda: False)

    ok = updater._report_tool_updates(apply=True)

    assert not globally_installed_tool, "sudo must not run with no terminal to prompt on"
    assert ok is False, "a skipped upgrade is a failure and must reach the exit code"
    out = capsys.readouterr().out
    assert "sudo pipx upgrade --global pymobiledevice3" in out, (
        "the command must be printed so the user can run it themselves"
    )


def test_the_menu_bar_is_told_where_to_run_it(
    globally_installed_tool, monkeypatch, capsys
):
    """Identity changes the wording only. "Cannot be asked for here" is the
    diagnosis; someone who clicked a menu item needs the next step."""
    from server.lifecycle import updater
    from server.lifecycle.invocation import INVOKED_BY, MENUBAR

    monkeypatch.setenv(INVOKED_BY, MENUBAR)
    monkeypatch.setattr(updater, "_can_ask_for_a_password", lambda: False)

    updater._report_tool_updates(apply=True)

    out = capsys.readouterr().out
    assert "Open a terminal and run" in out
    assert "sudo pipx upgrade --global pymobiledevice3" in out
    assert not globally_installed_tool, "wording must not change what is run"


def test_a_sudo_upgrade_runs_when_there_is_a_terminal(
    globally_installed_tool, monkeypatch, capsys
):
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_can_ask_for_a_password", lambda: True)

    ok = updater._report_tool_updates(apply=True)

    assert globally_installed_tool == [
        ["sudo", "pipx", "upgrade", "--global", "pymobiledevice3"]
    ]
    assert ok is True
    assert "may be asked for your password" in capsys.readouterr().out


def test_a_per_user_install_is_not_called_global_when_home_is_a_symlink(
    tmp_path, monkeypatch
):
    """The case the other tests structurally cannot see.

    Every other test builds its paths from `Path.home()` itself, so both sides
    of the comparison agree by construction and `resolve()` is the identity.
    That is the recorded shape of a test passing against the bug it claims to
    cover, so this one reaches home through a symlink -- which is how a machine
    with its home on an external volume is actually set up, i.e. the exact
    population the global-pipx feature was written for.
    """
    from server.device.tool_updates import _pipx_is_global

    real = tmp_path / "real_home"
    (real / ".local" / "pipx" / "venvs" / "fb-idb" / "bin").mkdir(parents=True)
    link = tmp_path / "home"
    link.symlink_to(real)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: link))

    site = ToolSite(
        name="idb",
        role="primary",
        source="pipx",
        path=str(link / ".local/pipx/venvs/fb-idb/bin/idb"),
        version="1.0.0",
    )
    assert link.resolve() != link, "the symlink is the point of this test"
    assert _pipx_is_global(site) is False


@pytest.mark.asyncio
async def test_a_global_install_says_why_it_needs_a_password():
    """The note was set and never asserted, so it could be dropped silently.

    It is the only thing that explains an otherwise surprising password prompt,
    which makes it the part a reader actually needs.
    """
    async def pypi(_name):
        return "11.12.4"

    site = _site(source="pipx", version="9.15.1",
                 path="/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3")
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is True
    assert update.note, "a sudo command with no explanation"
    assert "sudo" in update.note


@pytest.mark.asyncio
async def test_a_relocated_global_pipx_home_is_still_global():
    """`PIPX_GLOBAL_HOME` moves it, and the docstring claims tolerance for that.

    Every other test used `/opt/pipx` or a path under home, so the claim rested
    on the *absence* of a hardcoded `/opt/pipx` rather than on anything
    exercised. Decided by location -- not under the user's home -- so a global
    home anywhere outside it classifies correctly.
    """
    async def pypi(_name):
        return "11.12.4"

    site = _site(source="pipx", version="9.15.1",
                 path="/usr/local/share/pipx/venvs/pymobiledevice3/bin/pymobiledevice3")
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is True
    assert "--global" in update.command


@pytest.mark.asyncio
async def test_a_relocated_pipx_home_is_still_per_user(tmp_path, monkeypatch):
    """PIPX_HOME outside the home directory must not read as global.

    The location test on its own -- "outside $HOME means global" -- gets this
    exactly backwards. quern would offer `sudo pipx upgrade --global`, which
    targets PIPX_GLOBAL_HOME rather than the environment the tool is installed
    in, so it asks for a password and then fails "Package is not installed".
    That is the failure this whole branch exists to prevent, reached from the
    other direction.
    """
    async def pypi(_name):
        return "11.12.4"

    pipx_home = tmp_path / "elsewhere" / "pipx"
    (pipx_home / "venvs" / "pymobiledevice3" / "bin").mkdir(parents=True)
    monkeypatch.setenv("PIPX_HOME", str(pipx_home))

    site = _site(
        source="pipx", version="9.15.1",
        path=str(pipx_home / "venvs/pymobiledevice3/bin/pymobiledevice3"),
    )
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is False
    assert "--global" not in update.command


@pytest.mark.asyncio
async def test_a_relocated_global_home_is_still_global(tmp_path, monkeypatch):
    """And the same in reverse: PIPX_GLOBAL_HOME moved, under the user's home.

    Nothing says a relocated global home cannot sit inside $HOME, and the
    location test would then call a genuinely global install per-user and offer
    an upgrade with no sudo, which fails on permissions.
    """
    async def pypi(_name):
        return "11.12.4"

    global_home = tmp_path / "shared-pipx"
    (global_home / "venvs" / "pymobiledevice3" / "bin").mkdir(parents=True)
    monkeypatch.setenv("PIPX_GLOBAL_HOME", str(global_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    site = _site(
        source="pipx", version="9.15.1",
        path=str(global_home / "venvs/pymobiledevice3/bin/pymobiledevice3"),
    )
    update = _by_name(await _plan([site], pypi=pypi), "pymobiledevice3")

    assert update.needs_root is True
    assert "--global" in update.command
