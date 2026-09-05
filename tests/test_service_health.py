"""Health checks for tunneld and the mitmproxy local-capture extension.

Both services fail in the same shape, which is why they are tested together:
the thing quern checks stays green while the thing that matters stops working.

- `check_tools()` reports tunneld from an HTTP probe. A wedged daemon and one
  that was never started both answer nothing, so the boolean cannot separate
  "restart this" from "install this" (#73).
- Nothing looked at the mitmproxy system extension at all. It is approved once
  by a human and then upgraded underneath that approval by any dependency
  update -- `mitmproxy-rs` moved to 0.12.11 during the upgrade that prompted
  this -- after which local capture reports itself enabled while capturing
  nothing.

Neither check may prompt for a password: `launchctl print` on a system job and
`systemextensionsctl list` are both readable unprivileged, and that is load
bearing for a read-only `doctor`.
"""

from __future__ import annotations

import plistlib
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.proxy import extension as ext_mod

# --------------------------------------------------------------------------
# launchctl parsing
# --------------------------------------------------------------------------

LAUNCHCTL_RUNNING = """\
system/com.quern.tunneld = {
\tactive count = 1
\tpath = /Library/LaunchDaemons/com.quern.tunneld.plist
\tstate = running
\tprogram = /opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3
\truns = 1
\tpid = 825
\tlast exit code = (never exited)
\tendpoints = {
\t\t"com.quern.tunneld" = {
\t\t\tstate = active
\t\t}
\t}
}
"""


@pytest.fixture
def launchctl(monkeypatch):
    """Make `launchctl print` return canned output."""
    def install(stdout: str, returncode: int = 0):
        def fake_run(cmd, **_kw):
            assert cmd[:2] == ["launchctl", "print"], cmd
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        monkeypatch.setattr("server.device.tunneld.subprocess.run", fake_run)

    return install


def test_launchd_job_reads_the_scalar_fields(launchctl):
    from server.device.tunneld import launchd_job

    launchctl(LAUNCHCTL_RUNNING)
    job = launchd_job()
    assert job["state"] == "running"
    assert job["pid"] == "825"
    assert job["program"].endswith("pymobiledevice3")


def test_nested_endpoint_state_does_not_overwrite_the_job_state(launchctl):
    """`launchctl print` repeats `state = active` for every endpoint. Taking the
    last match would report the job as active whatever it is really doing, and
    the wedge signature depends entirely on this field."""
    from server.device.tunneld import launchd_job

    launchctl(LAUNCHCTL_RUNNING)
    assert launchd_job()["state"] == "running"


def test_launchd_job_is_empty_when_the_job_is_unknown(launchctl):
    from server.device.tunneld import launchd_job

    launchctl("Could not find service", returncode=113)
    assert launchd_job() == {}


def test_launchd_job_survives_launchctl_being_absent(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no launchctl")

    monkeypatch.setattr("server.device.tunneld.subprocess.run", boom)
    from server.device.tunneld import launchd_job

    assert launchd_job() == {}


# --------------------------------------------------------------------------
# tunneld health
# --------------------------------------------------------------------------


@pytest.fixture
def tunneld_env(monkeypatch, tmp_path):
    """Control every input tunneld_health consults."""
    plist = tmp_path / "com.quern.tunneld.plist"
    plist.write_text("<plist/>")
    binary = tmp_path / "pymobiledevice3"
    binary.write_text("#!/bin/sh\n")

    state = {"serving": True, "job": {}, "plist_current": True,
             "plist": plist, "binary": binary}

    async def fake_running():
        return state["serving"]

    monkeypatch.setattr("server.device.tunneld.is_tunneld_running", fake_running)
    monkeypatch.setattr("server.device.tunneld.launchd_job", lambda: state["job"])
    monkeypatch.setattr(
        "server.device.tunneld.installed_plist_is_current", lambda: state["plist_current"])
    monkeypatch.setattr(
        "server.device.tunneld.installed_plist_log_path", lambda: Path("/old/log"))
    monkeypatch.setattr(
        "server.device.tunneld.find_pymobiledevice3_binary", lambda: state["binary"])
    monkeypatch.setattr("server.device.tunneld.PLIST_PATH", plist)
    return state


async def test_wedged_is_http_down_while_launchd_says_running(tunneld_env):
    """The #73 signature. A daemon that holds no listener and never exits is
    invisible to KeepAlive, so launchd reports it healthy forever."""
    from server.device.tunneld import tunneld_health

    tunneld_env["serving"] = False
    tunneld_env["job"] = {"state": "running", "pid": "825"}

    health = await tunneld_health()
    assert health.status == "wedged"
    assert health.pid == 825
    assert not health.ok


async def test_stopped_is_http_down_with_launchd_not_running(tunneld_env):
    """Same HTTP symptom as wedged, opposite remedy — which is the entire
    reason the launchd state is consulted at all."""
    from server.device.tunneld import tunneld_health

    tunneld_env["serving"] = False
    tunneld_env["job"] = {}

    health = await tunneld_health()
    assert health.status == "stopped"


async def test_the_wedged_remedy_avoids_kickstart(tunneld_env):
    """`kickstart -k` sends SIGKILL and hung launchctl on macOS 15; the codebase
    already says so in install_daemon. It must never be the advice."""
    from server.device.tunneld import tunneld_health

    tunneld_env["serving"] = False
    tunneld_env["job"] = {"state": "running", "pid": "1"}

    health = await tunneld_health()
    assert "bootout" in health.remedy
    assert "bootstrap" in health.remedy
    assert "kickstart" not in health.remedy.split("(")[0]


async def test_missing_binary_is_reported_before_anything_else(tunneld_env):
    from server.device.tunneld import tunneld_health

    tunneld_env["binary"] = None
    health = await tunneld_health()
    assert health.status == "no_binary"
    assert "pipx install" in health.remedy


async def test_missing_plist_reads_as_not_installed(tunneld_env, monkeypatch, tmp_path):
    from server.device.tunneld import tunneld_health

    monkeypatch.setattr("server.device.tunneld.PLIST_PATH", tmp_path / "absent.plist")
    health = await tunneld_health()
    assert health.status == "not_installed"


async def test_a_serving_daemon_on_a_stale_plist_is_flagged(tunneld_env):
    from server.device.tunneld import tunneld_health

    tunneld_env["plist_current"] = False
    tunneld_env["job"] = {"state": "running", "pid": "5"}
    health = await tunneld_health()
    assert health.status == "stale_plist"


async def test_binary_drift_is_caught_while_serving(tunneld_env):
    """The daemon runs whatever the plist froze in. A second pipx install
    shadowing it drifts silently -- the old binary keeps serving perfectly, so
    the HTTP probe stays green."""
    from server.device.tunneld import tunneld_health

    tunneld_env["job"] = {"state": "running", "pid": "5", "program": "/other/pymobiledevice3"}
    health = await tunneld_health()
    assert health.status == "binary_drift"
    assert "/other/pymobiledevice3" in health.detail


async def test_healthy_when_everything_lines_up(tunneld_env):
    from server.device.tunneld import tunneld_health

    tunneld_env["job"] = {
        "state": "running", "pid": "825", "program": str(tunneld_env["binary"]),
    }
    health = await tunneld_health()
    assert health.status == "healthy"
    assert health.ok


# --------------------------------------------------------------------------
# The mitmproxy system extension
# --------------------------------------------------------------------------

# Real `systemextensionsctl list` output. Built by joining rather than written
# as one literal so the tab-separated row stays under the line limit without
# being reflowed into something the parser would never actually see.
_SYSEXT_ROW = "\t".join([
    "*", "*", "S8XHQB96PW",
    f"{ext_mod.BUNDLE_ID} (2.0/1)", "network-extension", "[activated enabled]",
])
SYSEXT_LIST = "\n".join([
    "2 extension(s)",
    "--- com.apple.system_extension.network_extension",
    "enabled\tactive\tteamID\tbundleID (version)\tname\t[state]",
    _SYSEXT_ROW,
]) + "\n"


@pytest.fixture
def sysext(monkeypatch):
    def install(stdout: str, returncode: int = 0):
        def fake_run(cmd, **_kw):
            assert cmd[0] == "systemextensionsctl", cmd
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(ext_mod.subprocess, "run", fake_run)

    monkeypatch.setattr(ext_mod.sys, "platform", "darwin")
    return install


@pytest.fixture
def shipped_tar(monkeypatch, tmp_path):
    """A real tar with a real Info.plist, so the extraction path is exercised."""
    def install(short: str, build: str):
        plist_dir = tmp_path / "build" / Path(ext_mod._EXTENSION_PLIST).parent
        plist_dir.mkdir(parents=True, exist_ok=True)
        (plist_dir / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": ext_mod.BUNDLE_ID,
            "CFBundleShortVersionString": short,
            "CFBundleVersion": build,
        }))
        tar_path = tmp_path / ext_mod.APP_TAR_NAME
        with tarfile.open(tar_path, "w") as archive:
            archive.add(tmp_path / "build" / "Mitmproxy Redirector.app",
                        arcname="Mitmproxy Redirector.app")
        monkeypatch.setattr(ext_mod, "_tar_path", lambda: tar_path)
        return tar_path

    return install


def test_shipped_version_is_read_out_of_the_tar(shipped_tar):
    shipped_tar("2.0", "1")
    assert ext_mod.shipped_version() == "2.0/1"


def test_reading_the_shipped_version_does_not_unpack_anything(shipped_tar, tmp_path):
    """A health check must not have side effects; unpacking is what activation
    does."""
    shipped_tar("2.0", "1")
    before = set(tmp_path.rglob("*"))
    ext_mod.shipped_version()
    assert set(tmp_path.rglob("*")) == before


def test_activated_extension_is_parsed_without_privileges(sysext):
    sysext(SYSEXT_LIST)
    assert ext_mod.activated_extension() == ("2.0/1", "activated enabled")


def test_no_matching_row_reads_as_not_activated(sysext, shipped_tar):
    sysext("0 extension(s)\n")
    shipped_tar("2.0", "1")
    health = ext_mod.extension_health()
    assert health.status == "not_activated"
    assert "System Settings" in health.remedy


def test_a_newer_shipped_version_is_stale(sysext, shipped_tar):
    """The failure this module exists for: approved once, then upgraded
    underneath the approval."""
    sysext(SYSEXT_LIST)
    shipped_tar("2.1", "3")

    health = ext_mod.extension_health()
    assert health.status == "stale"
    assert health.activated == "2.0/1"
    assert health.shipped == "2.1/3"
    assert "capturing nothing" in health.detail
    assert health.fixable


def test_matching_versions_are_healthy(sysext, shipped_tar):
    sysext(SYSEXT_LIST)
    shipped_tar("2.0", "1")
    health = ext_mod.extension_health()
    assert health.status == "healthy"
    assert not health.fixable


def test_registered_but_disabled_is_not_healthy(sysext, shipped_tar):
    sysext(SYSEXT_LIST.replace("[activated enabled]", "[activated waiting for user]"))
    shipped_tar("2.0", "1")
    health = ext_mod.extension_health()
    assert health.status == "not_activated"


def test_a_missing_wheel_is_reported_not_guessed(monkeypatch, sysext):
    sysext(SYSEXT_LIST)
    monkeypatch.setattr(ext_mod, "_tar_path", lambda: None)
    health = ext_mod.extension_health()
    assert health.status == "not_shipped"


def test_non_macos_is_unsupported_rather_than_broken(monkeypatch):
    monkeypatch.setattr(ext_mod.sys, "platform", "linux")
    health = ext_mod.extension_health()
    assert health.status == "unsupported"
    assert not health.fixable


def test_systemextensionsctl_failing_is_not_read_as_absent(sysext, shipped_tar):
    """A non-zero exit means the question could not be asked.

    The output is deliberately *not* empty: a failing command that still prints
    a row is the case that distinguishes checking the exit code from merely
    finding nothing to parse. An earlier version of this test passed an empty
    string, so it went on passing with the exit-code check deleted.
    """
    sysext(SYSEXT_LIST, returncode=1)
    shipped_tar("2.0", "1")
    assert ext_mod.activated_extension() is None


# --------------------------------------------------------------------------
# Reinstall
# --------------------------------------------------------------------------


def test_reinstall_unpacks_and_launches_the_app(shipped_tar, monkeypatch, tmp_path):
    shipped_tar("2.1", "3")
    monkeypatch.setattr(ext_mod, "INSTALL_DIR", tmp_path / "installed")

    opened = []

    def fake_run(cmd, **_kw):
        opened.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ext_mod.subprocess, "run", fake_run)

    ok, message = ext_mod.reinstall()
    assert ok
    assert opened[0][0] == "open"
    assert (tmp_path / "installed" / "Mitmproxy Redirector.app").is_dir()
    assert "System Settings" in message


def test_reinstall_does_not_claim_the_extension_is_active(shipped_tar, monkeypatch, tmp_path):
    """Approval is a human step in System Settings. Reporting success would be
    claiming a repair that has not happened."""
    shipped_tar("2.1", "3")
    monkeypatch.setattr(ext_mod, "INSTALL_DIR", tmp_path / "installed")
    monkeypatch.setattr(
        ext_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))

    _ok, message = ext_mod.reinstall()
    assert "System Settings" in message
    assert "approve" in message.lower()


def test_reinstall_reports_a_failed_launch(shipped_tar, monkeypatch, tmp_path):
    shipped_tar("2.1", "3")
    monkeypatch.setattr(ext_mod, "INSTALL_DIR", tmp_path / "installed")
    monkeypatch.setattr(
        ext_mod.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="nope"))

    ok, message = ext_mod.reinstall()
    assert not ok
    assert "nope" in message


def test_reinstall_without_the_wheel_fails_cleanly(monkeypatch):
    monkeypatch.setattr(ext_mod, "_tar_path", lambda: None)
    ok, message = ext_mod.reinstall()
    assert not ok
    assert "not installed" in message


# --------------------------------------------------------------------------
# What doctor does with all this
# --------------------------------------------------------------------------


@pytest.fixture
def wedged_tunneld(monkeypatch):
    """A wedged daemon and a stale extension, both reported through doctor."""
    from server.device.tunneld import TunneldHealth

    async def health():
        return TunneldHealth(
            status="wedged", serving=False, launchd_state="running", pid=825,
            detail="alive (pid 825) but not serving",
            remedy="sudo launchctl bootout system/com.quern.tunneld && ...",
        )

    monkeypatch.setattr("server.device.tunneld.tunneld_health", health)
    monkeypatch.setattr(ext_mod, "extension_health", lambda: ext_mod.ExtensionHealth(
        status="stale", shipped="2.1/3", activated="2.0/1",
        detail="macOS is running 2.0/1 but the wheel ships 2.1/3",
        remedy="quern doctor --fix re-runs the shipped app",
    ))

    reinstalled: list[bool] = []
    monkeypatch.setattr(
        ext_mod, "reinstall",
        lambda: (reinstalled.append(True), (True, "launched it; approve in System Settings"))[1])
    return reinstalled


def test_doctor_never_restarts_a_wedged_tunneld(wedged_tunneld, monkeypatch, capsys):
    """#73 keeps a live wedged instance deliberately, so detection schemes can
    be tested against a real one. A --fix that silently restarted it would
    destroy the only real reproduction anyone has."""
    ran: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))

    from server.main import _report_service_health

    _report_service_health(fix=True)
    out = capsys.readouterr().out
    assert "wedged" in out or "not serving" in out
    assert "issue #73" in out
    assert ran == [], "doctor must not run launchctl against tunneld"


def test_doctor_shows_the_wedge_remedy_without_running_it(wedged_tunneld, capsys):
    from server.main import _report_service_health

    _report_service_health(fix=False)
    out = capsys.readouterr().out
    assert "bootout" in out
    assert "not repaired automatically" in out


def test_fix_reinstalls_a_stale_extension(wedged_tunneld, capsys):
    from server.main import _report_service_health

    _report_service_health(fix=True)
    assert wedged_tunneld == [True], "--fix should have re-run the shipped app"
    assert "approve in System Settings" in capsys.readouterr().out


def test_without_fix_the_extension_is_only_reported(wedged_tunneld, capsys):
    from server.main import _report_service_health

    _report_service_health(fix=False)
    assert wedged_tunneld == [], "reporting must not launch anything"
    assert "re-runs the shipped app" in capsys.readouterr().out


def test_a_healthy_extension_is_never_reinstalled(monkeypatch, capsys):
    from server.device.tunneld import TunneldHealth

    async def health():
        return TunneldHealth(status="healthy", serving=True, detail="serving")

    monkeypatch.setattr("server.device.tunneld.tunneld_health", health)
    monkeypatch.setattr(ext_mod, "extension_health", lambda: ext_mod.ExtensionHealth(
        status="healthy", shipped="2.0/1", activated="2.0/1", detail="2.0/1 activated"))

    called: list = []
    monkeypatch.setattr(ext_mod, "reinstall", lambda: called.append(True) or (True, ""))

    from server.main import _report_service_health

    _report_service_health(fix=True)
    assert called == []


def test_a_broken_check_does_not_break_doctor(monkeypatch, capsys):
    async def boom():
        raise RuntimeError("launchctl exploded")

    monkeypatch.setattr("server.device.tunneld.tunneld_health", boom)
    monkeypatch.setattr(ext_mod, "extension_health", lambda: (_ for _ in ()).throw(
        RuntimeError("systemextensionsctl exploded")))

    from server.main import _report_service_health

    _report_service_health(fix=False)
    out = capsys.readouterr().out
    assert "could not be checked" in out
    assert out.count("could not be checked") == 2, "each check must fail independently"
