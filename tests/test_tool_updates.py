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

import pytest

from server.device.tool_updates import (
    CLI_FLOORS,
    format_offer,
    is_behind,
    plan_updates,
    version_tuple,
)
from server.device.tool_versions import ToolSite


def _site(name="pymobiledevice3", role="cli", source="pipx", version="9.15.1", **kw):
    # `package` defaults to the tool's own name, which is true for most sites.
    # The ones where it is not -- idb/fb-idb, adb/android-platform-tools -- are
    # set explicitly by the tests that care.
    return ToolSite(
        name=name, role=role, source=source, version=version,
        package=kw.pop("package", name), brew_cask=kw.pop("brew_cask", False),
        available=kw.pop("available", True), path=kw.pop("path", f"/opt/{source}/bin/{name}"),
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


def test_doctor_passes_the_fix_flag_through():
    """Pin the wiring: the flag reached `_report_python_deps` and not this
    section, which is how the inconsistency arose in the first place."""
    import inspect

    from server import main

    source = inspect.getsource(main._cmd_doctor)
    assert '_report_external_tools(getattr(args, "fix", False))' in source


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
