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


class TestTheInstallRefusesToBreakItself:
    def test_a_binary_on_an_external_volume_is_never_written_into_the_daemon(
        self, home_on_external, monkeypatch, capsys
    ):
        """The daemon starts at boot, before /Volumes is guaranteed mounted.
        Recording one there produces a daemon that works until the next reboot
        and then does not, having reported success."""
        env = home_on_external
        monkeypatch.setattr(
            tunneld, "find_pymobiledevice3_binary", lambda *a, **k: env.venv_script
        )
        wrote: list = []
        monkeypatch.setattr(tunneld, "generate_plist", lambda b: wrote.append(b) or "")

        rc = tunneld.install_daemon()

        assert rc != 0, "installing an unreachable daemon must not report success"
        assert not wrote, "no plist should have been generated"
        assert "external volume" in capsys.readouterr().out
