"""Replay a captured machine, so "works on my machine" becomes a test.

`scripts/capture-env.py` records the handful of facts Quern's environment checks
actually read: where each pymobiledevice3 lives, what it resolves to, the order
of PATH, and what the tunneld LaunchDaemon has baked in. This rebuilds that
world in-process and runs the real lookups against it.

The fixture here is a real machine whose home is an external volume, captured
while it was producing two wrong answers at once (2026-09-11):

  * `check_pymobiledevice3` reported the pipx CLI as "installed under external
    home", naming a path that was in fact quern's own venv console script.
  * `check_tunneld` reported a stale log path, printing the installed and
    expected paths *identically*, because it named the log path whichever
    condition had failed. The real drift was the binary.

Both came from one lookup returning the wrong file, and the repair both
messages advised -- `./quern tunneld install` -- would have baked an external
volume path into a boot-time daemon.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from server.device import tunneld

FIXTURES = Path(__file__).parent / "fixtures" / "envs"


class CapturedEnv:
    """A machine, as recorded by scripts/capture-env.py."""

    def __init__(self, data: dict):
        self.data = data
        self.home = Path(data["home"])
        self.project_root = Path(data["project_root"])
        self.installs = {i["path"]: i for i in data["pymobiledevice3_installs"]}

    @property
    def venv_script(self) -> Path:
        return self.project_root / ".venv" / "bin" / "pymobiledevice3"

    @property
    def path(self) -> list[str]:
        """The captured PATH directories, in their original order.

        The capture keeps only the entries that matter and records each one's
        index, so an omitted entry cannot silently change what resolves first.
        """
        return [entry["dir"] for entry in sorted(self.data["path"], key=lambda e: e["index"])]

    def path_as_setup_sees_it(self) -> list[str]:
        """PATH with the project venv prepended, which is what `run_setup` does
        before any check runs -- and the reason the console script wins."""
        return [str(self.project_root / ".venv" / "bin"), *self.path]

    def install(self, monkeypatch: pytest.MonkeyPatch, *, venv_on_path: bool) -> None:
        """Make the real lookups see this machine and nothing of the host's."""
        entries = self.path_as_setup_sees_it() if venv_on_path else self.path
        known = set(self.installs)

        def fake_which(name: str) -> str | None:
            if name != "pymobiledevice3":
                return None
            for directory in entries:
                candidate = f"{directory}/{name}"
                if candidate in known:
                    return candidate
            return None

        real_exists = Path.exists

        def fake_exists(self_: Path) -> bool:
            # Only the captured world exists. Anything else would let a host
            # install leak in and quietly make the replay pass for the wrong
            # reason -- which is the failure this file is about.
            if self_.name == "pymobiledevice3":
                return str(self_) in known
            return real_exists(self_)

        def fake_resolve(self_: Path, strict: bool = False) -> Path:
            entry = self.installs.get(str(self_))
            return Path(entry["resolves_to"]) if entry else self_

        monkeypatch.setattr(shutil, "which", fake_which)
        monkeypatch.setattr(Path, "exists", fake_exists)
        monkeypatch.setattr(Path, "resolve", fake_resolve)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.home))
        monkeypatch.setattr(tunneld, "_project_root", lambda: self.project_root)


@pytest.fixture
def home_on_external() -> CapturedEnv:
    return CapturedEnv(json.loads((FIXTURES / "home-on-external.json").read_text()))


def test_the_capture_still_describes_the_situation_it_was_taken_for(home_on_external):
    """If the fixture stops holding these, every test below proves nothing."""
    env = home_on_external
    assert env.data["home_is_external"], "the fixture is a home-on-external machine"
    kinds = {i["kind"] for i in env.data["pymobiledevice3_installs"]}
    assert "project-venv-console-script" in kinds, "the shadowing copy"
    assert "pipx-global" in kinds, "the real CLI"
    assert str(env.venv_script).startswith("/Volumes/"), "the shadowing copy is on the volume"
    # The failing condition, as data rather than as an assumption in this file.
    # A capture taken from an ordinary shell shows `which` resolving correctly,
    # because setup's venv prepend happens inside setup's own process -- so the
    # capture records what a check would see as well as what the shell sees.
    assert env.data["which_pymobiledevice3_as_setup_sees_it"] == str(env.venv_script), (
        "the capture must record the shadowing, or a user hitting this bug "
        "attaches a report showing everything resolving correctly"
    )
    assert env.data["which_pymobiledevice3"] != str(env.venv_script), (
        "and it must record the plain lookup too, which is what made it confusing"
    )


class TestTheLookupFindsTheCLI:
    def test_the_project_venv_console_script_is_not_the_cli(
        self, home_on_external, monkeypatch
    ):
        """quern depends on pymobiledevice3 as a library, so its venv holds a
        console script of the same name. Setup puts that venv first on PATH."""
        env = home_on_external
        env.install(monkeypatch, venv_on_path=True)

        found = tunneld.find_pymobiledevice3_binary()

        assert found != env.venv_script, "the library console script is not the CLI"
        assert str(found) == "/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3"

    def test_the_answer_does_not_depend_on_whether_setup_touched_PATH(
        self, home_on_external, monkeypatch
    ):
        """A lookup that changes answer depending on who called it is how one
        machine produced two different diagnoses of itself."""
        env = home_on_external
        env.install(monkeypatch, venv_on_path=False)
        without = tunneld.find_pymobiledevice3_binary()
        env.install(monkeypatch, venv_on_path=True)
        with_venv = tunneld.find_pymobiledevice3_binary()
        assert without == with_venv


class TestUpgradingWhatTheCapturedMachineHasInstalled:
    """The captured machine is the one that hit this, so it is the one to plan
    against.

    `quern update --tools` on it failed with "Package is not installed.
    Expected to find ~/.local/pipx/venvs/pymobiledevice3, but it does not
    exist" -- for a tool sitting in /opt/pipx and working fine. Hand-built
    sites would have proved the same thing, but this proves it about a real
    configuration rather than one I invented to match the code.
    """

    async def test_the_upgrade_command_matches_where_it_is_installed(
        self, home_on_external
    ):
        from server.device.tool_updates import plan_updates
        from server.device.tool_versions import ToolSite

        install = next(
            i for i in home_on_external.data["pymobiledevice3_installs"]
            if i["kind"] == "pipx-global"
        )
        site = ToolSite(
            name="pymobiledevice3", role="cli", available=True,
            version="9.15.1", path=install["path"], source="pipx",
            package="pymobiledevice3",
        )

        async def pypi(_name):
            return "11.12.4"

        async def brew():
            return {}

        updates = await plan_updates([site], pypi=pypi, brew=brew)
        update = next(u for u in updates if u.name == "pymobiledevice3")

        assert update.command == [
            "sudo", "pipx", "upgrade", "--global", "pymobiledevice3"
        ], f"`{' '.join(update.command)}` is the command that failed on this machine"
        assert update.needs_root is True

    def test_the_capture_still_records_a_global_install(self, home_on_external):
        """If the fixture is ever re-captured on a machine without one, the test
        above silently stops testing anything."""
        kinds = {i["kind"] for i in home_on_external.data["pymobiledevice3_installs"]}
        assert "pipx-global" in kinds


class TestTheExclusionCoversBothShadowingRoots:
    """The `sys.prefix` half had no coverage at all: deleting it left 230 tests
    green. It is the half that matters most -- setup identifies the shadowing
    directory by the running interpreter, not by where the checkout is."""

    def test_the_running_interpreters_venv_is_excluded(self, monkeypatch):
        import sys as real_sys

        fake_venv = Path("/Volumes/elsewhere/venvs/quern")
        monkeypatch.setattr(real_sys, "prefix", str(fake_venv))
        monkeypatch.setattr(real_sys, "base_prefix", "/usr")
        monkeypatch.setattr(tunneld, "_project_root", lambda: None)

        roots = tunneld.shadowing_roots()

        assert fake_venv in roots, (
            "a venv outside the checkout still shadows, and is identified by "
            "sys.prefix -- which is how run_setup identifies it"
        )

    def test_the_checkout_is_excluded_too(self, monkeypatch, tmp_path):
        """A real symlinked directory, not an imaginary path.

        The first version used a path that does not exist, where non-strict
        `.resolve()` is the identity -- so the assertion held with or without
        it. The same tautology the symlink test above was rewritten to avoid.
        """
        import sys as real_sys

        real = tmp_path / "real-checkout"
        real.mkdir()
        link = tmp_path / "link-checkout"
        link.symlink_to(real)

        monkeypatch.setattr(real_sys, "prefix", "/usr")
        monkeypatch.setattr(real_sys, "base_prefix", "/usr")
        monkeypatch.setattr(tunneld, "_project_root", lambda: link)

        assert tunneld.shadowing_roots() == [real], (
            "the root is compared against a resolved candidate, so it has to "
            "be resolved too"
        )

    def test_a_venv_reached_through_a_symlink_is_still_excluded(self, monkeypatch, tmp_path):
        """The root is compared against a resolved candidate, so it has to be
        resolved too. /tmp/v resolves to /private/tmp/v on macOS, and an
        unresolved root never matched -- handing back the shadowing script.

        Goes through `shadowing_roots()` rather than passing `excluded_roots`,
        because the resolve being tested happens inside it. The first version
        passed the root in already resolved and proved nothing: leaving the
        `.resolve()` off still passed.
        """
        import sys as real_sys

        real = tmp_path / "real-venv"
        (real / "bin").mkdir(parents=True)
        script = real / "bin" / "pymobiledevice3"
        script.write_text("")
        link = tmp_path / "link-venv"
        link.symlink_to(real)

        monkeypatch.setattr(real_sys, "prefix", str(link))
        monkeypatch.setattr(real_sys, "base_prefix", "/usr")
        monkeypatch.setattr(tunneld, "_project_root", lambda: None)
        monkeypatch.setattr(tunneld, "pipx_candidates", list)

        found = tunneld.find_pymobiledevice3_binary(
            which=lambda _n: str(link / "bin" / "pymobiledevice3"),
        )

        assert found is None, "the shadowing script came back through a symlinked root"

    def test_the_pipx_fallback_applies_the_exclusion_too(self, monkeypatch, tmp_path):
        """The fallback skipped it entirely, and one of its entries is a shim
        whose symlink target is unconstrained — so it was a way back in."""
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        shim = venv / "bin" / "pymobiledevice3"
        shim.write_text("")

        monkeypatch.setattr(tunneld, "pipx_candidates", lambda: [shim])
        found = tunneld.find_pymobiledevice3_binary(
            which=lambda _n: None, excluded_roots=[venv]
        )

        assert found is None, "the fallback returned a binary inside an excluded root"


class TestTheDriftIsReportedHonestly:
    def test_a_drifted_binary_is_not_described_as_a_stale_log_path(
        self, home_on_external, monkeypatch
    ):
        """The message printed the installed and expected log paths identically,
        because both were correct. The binary was what differed."""
        env = home_on_external
        env.install(monkeypatch, venv_on_path=True)
        plist = env.data["tunneld_plist"]
        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {
            "ProgramArguments": plist["program_arguments"],
            "StandardOutPath": plist["standard_out_path"],
        })

        drift = tunneld.installed_plist_drift()

        assert drift is None, f"nothing has drifted on this machine, but got: {drift}"

    def test_a_genuinely_drifted_binary_says_so(self, home_on_external, monkeypatch):
        env = home_on_external
        env.install(monkeypatch, venv_on_path=True)
        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {
            "ProgramArguments": ["/somewhere/else/pymobiledevice3", "remote", "tunneld"],
            "StandardOutPath": env.data["tunneld_plist"]["standard_out_path"],
        })

        drift = tunneld.installed_plist_drift()

        assert drift is not None
        assert "binary" in drift
        assert "log path" not in drift, "the log path is correct; saying otherwise misleads"


class TestSetupReportsWhatDrifted:
    """The branch's second fix had no coverage at all: replacing `check_tunneld`
    with its old hardcoded-log-path body left the whole suite green. The tests
    below it exercised `installed_plist_drift`, which was already correct."""

    def _plist(self, monkeypatch, tmp_path, program: str, log: str):
        monkeypatch.setattr(tunneld, "_read_installed_plist", lambda: {
            "ProgramArguments": [program, "remote", "tunneld"],
            "StandardOutPath": log,
        })
        # A real file rather than a blanket `Path.exists` patch, which leaked
        # into an autouse teardown fixture and made it unlink a file that was
        # never there.
        installed = tmp_path / "com.quern.tunneld.plist"
        installed.write_text("")
        monkeypatch.setattr(tunneld, "PLIST_PATH", installed)
        # check_tunneld probes the daemon over HTTP before it reports drift.
        # Left alone that is a real request to this machine's live tunneld, so
        # the test would depend on whether the developer happens to have one.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no daemon in a test")),
        )

    def test_a_drifted_binary_is_named_in_the_message(
        self, home_on_external, monkeypatch, tmp_path
    ):
        from server.lifecycle.setup import check_tunneld

        env = home_on_external
        env.install(monkeypatch, venv_on_path=True)
        self._plist(monkeypatch, tmp_path, "/somewhere/else/pymobiledevice3",
                    env.data["tunneld_plist"]["standard_out_path"])

        result = check_tunneld()

        assert "binary" in result.message, (
            f"the binary drifted; the message says: {result.message}"
        )
        assert "log path" not in result.message, (
            "this printed the installed and expected log paths identically, "
            "because both were correct"
        )

    def test_a_drifted_log_path_is_still_named_correctly(
        self, home_on_external, monkeypatch, tmp_path
    ):
        from server.lifecycle.setup import check_tunneld

        env = home_on_external
        env.install(monkeypatch, venv_on_path=True)
        self._plist(monkeypatch, tmp_path,
                    "/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
                    "/Volumes/Home/someone/.quern/tunneld.log")

        result = check_tunneld()

        assert "log path" in result.message


class TestStatusDoesNotInventProblems:
    """`_print_status` had no test, and the fix that gave it an honest drift
    reason left the "Reinstall to migrate" advice one level out -- so every
    healthy install was told to reinstall, with no reason above it."""

    def _status(self, monkeypatch, capsys, tmp_path, drift):
        monkeypatch.setattr(tunneld, "installed_plist_drift", lambda: drift)
        monkeypatch.setattr(tunneld, "_tunneld_devices", lambda: (True, {}))
        monkeypatch.setattr(tunneld, "find_pymobiledevice3_binary", lambda *a, **k: Path("/x"))
        # A real file, not a blanket `Path.exists` patch -- that leaked into an
        # autouse teardown fixture and made it unlink something never created.
        # Second time today.
        installed = tmp_path / "com.quern.tunneld.plist"
        installed.write_text("")
        monkeypatch.setattr(tunneld, "PLIST_PATH", installed)
        tunneld._print_status()
        return capsys.readouterr().out

    def test_a_healthy_plist_is_not_told_to_reinstall(self, monkeypatch, capsys, tmp_path):
        out = self._status(monkeypatch, capsys, tmp_path, None)
        assert "Reinstall" not in out, (
            "a passing check must not read as a failed one -- and the advice is "
            "the command the install guard refuses on a home-on-external machine"
        )
        assert "outdated" not in out

    def test_a_drifted_plist_names_the_reason_and_the_remedy(
        self, monkeypatch, capsys, tmp_path
    ):
        out = self._status(monkeypatch, capsys, tmp_path, "binary is X, but quern resolves Y")
        assert "binary is X" in out
        assert "Reinstall" in out


class TestTheInstallRefusesToBreakItself:
    def test_a_binary_on_an_external_volume_is_never_written_into_the_daemon(
        self, home_on_external, monkeypatch, capsys, tmp_path
    ):
        """The daemon starts at boot, before /Volumes is guaranteed mounted.
        Recording one there produces a daemon that works until the next reboot
        and then does not, having reported success."""
        env = home_on_external
        # The guard's own error message tells the user to set this. Inheriting
        # it from the developer's shell turned the test green-for-the-wrong-
        # reason into red-for-the-wrong-reason.
        monkeypatch.delenv(tunneld.BOOT_OVERRIDE, raising=False)
        monkeypatch.setattr(
            tunneld, "find_pymobiledevice3_binary", lambda *a, **k: env.venv_script
        )
        wrote: list = []
        monkeypatch.setattr(tunneld, "generate_plist", lambda b: wrote.append(b) or "")
        # Sudo and the real plist path are stubbed because the mutation this
        # test exists for -- deleting the guard -- otherwise boots out the
        # developer's own tunneld and overwrites /Library/LaunchDaemons, from
        # `pytest -q`. A guard whose mutation test is unsafe to run is a guard
        # nobody will verify.
        sudo_calls: list = []
        # Returns True, the way a successful _run_sudo does. Returning 0 made
        # install_daemon read every call as a failure, so `rc != 0` passed
        # whether the guard existed or not -- and anyone "correcting" it later
        # would have reached the unstubbed launchctl bootstrap from pytest.
        monkeypatch.setattr(tunneld, "_run_sudo", lambda *a, **k: sudo_calls.append(a) or True)
        monkeypatch.setattr(tunneld, "_bootstrap_with_retry", lambda *a, **k: 0)
        monkeypatch.setattr(tunneld, "PLIST_PATH", tmp_path / "com.quern.tunneld.plist")

        rc = tunneld.install_daemon()

        assert rc != 0, "installing an unreachable daemon must not report success"
        assert not wrote, "no plist should have been generated"
        assert not sudo_calls, "nothing should have been done as root"
        assert "external volume" in capsys.readouterr().out
