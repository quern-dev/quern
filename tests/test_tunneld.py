"""Tests for tunneld module — binary discovery, health check, plist generation, CLI."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import server.device.tunneld as tunneld_module
from server.device.tunneld import (
    LAUNCHCTL,
    LOG_PATH,
    TUNNELD_LABEL,
    _restart_daemon,
    _run_sudo,
    _tunnel_udid_cache,
    can_recover_unattended,
    cli_tunneld,
    find_pymobiledevice3_binary,
    generate_plist,
    get_tunneld_devices,
    install_daemon,
    install_recovery_grant,
    installed_plist_is_current,
    installed_plist_log_path,
    is_tunneld_running,
    recover_wedged_tunneld,
    recovery_grant_line,
    resolve_tunnel_udid,
)

# ---------------------------------------------------------------------------
# find_pymobiledevice3_binary
# ---------------------------------------------------------------------------


class TestFindBinary:
    def test_found_on_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/pymobiledevice3"):
            with patch.object(Path, "resolve", return_value=Path("/usr/local/bin/pymobiledevice3")):
                result = find_pymobiledevice3_binary()
                assert result == Path("/usr/local/bin/pymobiledevice3")

    def test_found_in_pipx(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=True):
                with patch.object(Path, "resolve", return_value=Path("/resolved/pymobiledevice3")):
                    result = find_pymobiledevice3_binary()
                    assert result == Path("/resolved/pymobiledevice3")

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = find_pymobiledevice3_binary()
                assert result is None


# ---------------------------------------------------------------------------
# is_tunneld_running
# ---------------------------------------------------------------------------


class TestIsTunneldRunning:
    async def test_running(self):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await is_tunneld_running() is True

    async def test_not_running(self):
        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await is_tunneld_running() is False


# ---------------------------------------------------------------------------
# get_tunneld_devices
# ---------------------------------------------------------------------------


class TestGetTunneldDevices:
    async def test_returns_devices(self):
        devices = {
            "00008130-AAAA": [{"tunnel-address": "fd35::1", "tunnel-port": 61952}],
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = devices

        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_tunneld_devices()
            assert result == devices
            assert "00008130-AAAA" in result

    async def test_connection_error_returns_empty(self):
        with patch("server.device.tunneld.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_tunneld_devices()
            assert result == {}


# ---------------------------------------------------------------------------
# resolve_tunnel_udid
# ---------------------------------------------------------------------------


class TestResolveTunnelUdid:
    def setup_method(self):
        _tunnel_udid_cache.clear()

    async def test_cache_hit(self):
        _tunnel_udid_cache["53DA57AA-1234"] = "00008130-AAAA"
        result = await resolve_tunnel_udid("53DA57AA-1234")
        assert result == "00008130-AAAA"

    async def test_single_tunnel_maps_via_devicectl(self):
        """With one tunneled device, devicectl JSON maps the CoreDevice UUID."""
        devices = {"00008130-AAAA": [{"tunnel-address": "fd35::1"}]}
        devicectl_output = {
            "result": {
                "devices": [
                    {
                        "identifier": "53DA57AA-1234",
                        "hardwareProperties": {"udid": "00008130-AAAA"},
                    },
                ]
            }
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def fake_subprocess(*args, **kwargs):
            json_path = args[-1]
            Path(json_path).write_text(json.dumps(devicectl_output))
            return mock_proc

        with (
            patch("server.device.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1234")
            assert result == "00008130-AAAA"
            assert _tunnel_udid_cache["53DA57AA-1234"] == "00008130-AAAA"

    async def test_not_found(self):
        with patch("server.device.tunneld.get_tunneld_devices", return_value={}):
            result = await resolve_tunnel_udid("UNKNOWN-UUID")
            assert result is None

    async def test_multiple_tunnels_maps_via_devicectl(self, tmp_path):
        """With multiple tunneled devices, devicectl JSON maps CoreDevice UUIDs."""
        devices = {
            "00008130-AAAA1111": [{"tunnel-address": "fd35::1"}],
            "00008130-BBBB2222": [{"tunnel-address": "fd35::2"}],
        }
        devicectl_output = {
            "result": {
                "devices": [
                    {
                        "identifier": "53DA57AA-1111",
                        "hardwareProperties": {"udid": "00008130-AAAA1111"},
                    },
                    {
                        "identifier": "53DA57AA-2222",
                        "hardwareProperties": {"udid": "00008130-BBBB2222"},
                    },
                ]
            }
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def fake_subprocess(*args, **kwargs):
            # Write devicectl JSON to the temp file (second-to-last arg)
            json_path = args[-1]
            Path(json_path).write_text(json.dumps(devicectl_output))
            return mock_proc

        with (
            patch("server.device.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1111")
            assert result == "00008130-AAAA1111"
            assert _tunnel_udid_cache["53DA57AA-1111"] == "00008130-AAAA1111"
            assert _tunnel_udid_cache["53DA57AA-2222"] == "00008130-BBBB2222"

    async def test_multiple_tunnels_devicectl_fails_gracefully(self):
        """When devicectl fails with multiple tunnels, returns None."""
        devices = {
            "00008130-AAAA1111": [{"tunnel-address": "fd35::1"}],
            "00008130-BBBB2222": [{"tunnel-address": "fd35::2"}],
        }

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with (
            patch("server.device.tunneld.get_tunneld_devices", return_value=devices),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await resolve_tunnel_udid("53DA57AA-1111")
            assert result is None


# ---------------------------------------------------------------------------
# generate_plist
# ---------------------------------------------------------------------------


class TestGeneratePlist:
    def test_contains_required_keys(self):
        plist = generate_plist(Path("/usr/bin/pymobiledevice3"))
        assert TUNNELD_LABEL in plist
        assert "/usr/bin/pymobiledevice3" in plist
        assert "<string>remote</string>" in plist
        assert "<string>tunneld</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist
        assert str(LOG_PATH) in plist

    def test_log_path_is_system_location(self):
        # Regression: the log path must NOT reference the user's home directory.
        # A user-home path baked into the plist caused launchd to pre-create
        # /Volumes/<HomeVolume>/... at boot for users whose home lived on an
        # external volume, blocking the real volume from mounting at its name.
        plist = generate_plist(Path("/usr/bin/pymobiledevice3"))
        assert "/Users/" not in plist
        assert "/Volumes/" not in plist
        assert "/.quern/" not in plist
        assert str(LOG_PATH).startswith("/Library/Logs/")


# ---------------------------------------------------------------------------
# installed_plist_is_current / installed_plist_log_path
# ---------------------------------------------------------------------------


class TestInstalledPlistFreshness:
    def test_missing_plist_reports_outdated(self, tmp_path):
        with patch("server.device.tunneld.PLIST_PATH", tmp_path / "absent.plist"):
            assert installed_plist_log_path() is None
            assert installed_plist_is_current() is False

    def test_current_plist_reports_current(self, tmp_path):
        binary = Path("/usr/bin/pymobiledevice3")
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(binary))
        with (
            patch("server.device.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=binary,
            ),
        ):
            assert installed_plist_log_path() == LOG_PATH
            assert installed_plist_is_current() is True

    def test_legacy_plist_reports_outdated(self, tmp_path):
        # Simulate the pre-migration plist that pointed StandardOutPath at the
        # user home. After upgrade, installed_plist_is_current() should be
        # False so setup / status surface the reinstall prompt.
        plist_file = tmp_path / "com.quern.tunneld.plist"
        legacy = generate_plist(Path("/usr/bin/pymobiledevice3")).replace(
            str(LOG_PATH), "/Users/somebody/.quern/tunneld.log",
        )
        plist_file.write_text(legacy)
        with patch("server.device.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() == Path(
                "/Users/somebody/.quern/tunneld.log",
            )
            assert installed_plist_is_current() is False

    def test_binary_path_drift_reports_outdated(self, tmp_path):
        # Regression: after `sudo pipx install --global pymobiledevice3` adds
        # a binary at /usr/local/bin/, the plist still bakes in the old
        # per-user pipx path. Detection must catch this so the existing
        # migration prompt fires.
        binary_in_plist = Path(
            "/Volumes/Home/jham/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
        )
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(binary_in_plist))
        with (
            patch("server.device.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/local/bin/pymobiledevice3"),
            ),
        ):
            assert installed_plist_is_current() is False

    def test_missing_binary_with_no_alternative_reports_outdated(self, tmp_path):
        # If the plist program path doesn't exist AND nothing else is
        # discoverable, we should still flag — the daemon will crash-loop.
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(
            generate_plist(Path("/nonexistent/pymobiledevice3")),
        )
        with (
            patch("server.device.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=None,
            ),
        ):
            assert installed_plist_is_current() is False

    def test_missing_binary_with_discoverable_one_uses_discovered(self, tmp_path):
        # When find_pymobiledevice3_binary returns a different path than the
        # plist's program, plist is outdated regardless of whether the plist's
        # program exists on disk.
        binary_in_plist = Path("/usr/bin/pymobiledevice3")
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text(generate_plist(binary_in_plist))
        with (
            patch("server.device.tunneld.PLIST_PATH", plist_file),
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/local/bin/pymobiledevice3"),
            ),
        ):
            assert installed_plist_is_current() is False

    def test_unparseable_plist_reports_outdated(self, tmp_path):
        plist_file = tmp_path / "com.quern.tunneld.plist"
        plist_file.write_text("not a real plist")
        with patch("server.device.tunneld.PLIST_PATH", plist_file):
            assert installed_plist_log_path() is None
            assert installed_plist_is_current() is False


# ---------------------------------------------------------------------------
# _run_sudo + install_daemon upgrade path
# ---------------------------------------------------------------------------


class TestRunSudo:
    def test_returns_true_on_zero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _run_sudo(["ls"], timeout=5) is True
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == ["sudo", "ls"]
            assert kwargs["timeout"] == 5

    def test_returns_false_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert _run_sudo(["ls"], timeout=5) is False

    def test_returns_false_on_timeout_without_raising(self, capsys):
        import subprocess as sp
        with patch(
            "subprocess.run",
            side_effect=sp.TimeoutExpired(cmd=["sudo", "ls"], timeout=5),
        ):
            # Regression: previously a hung launchctl would raise
            # TimeoutExpired all the way to main() and dump a traceback.
            assert _run_sudo(["ls"], timeout=5) is False
        assert "timed out" in capsys.readouterr().out

    def test_returns_false_on_oserror(self, capsys):
        with patch("subprocess.run", side_effect=OSError("no sudo for you")):
            assert _run_sudo(["ls"], timeout=5) is False
        assert "no sudo for you" in capsys.readouterr().out


class TestInstallDaemonUpgradePath:
    def test_unloads_existing_before_bootstrap(self, tmp_path):
        """Regression: bootstrap-over-loaded returns EIO 5 and the previous
        kickstart -k fallback hung launchctl. The upgrade path must call
        bootout *before* bootstrap and skip kickstart entirely."""
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.tunneld.time.sleep"),  # speed up the test
        ):
            assert install_daemon() == 0

        operations = [a[0] for a in calls]
        assert operations == ["launchctl", "cp", "chown", "chmod", "launchctl"]
        # bootout must come first; bootstrap last; no kickstart anywhere.
        assert calls[0] == ["launchctl", "bootout", f"system/{TUNNELD_LABEL}"]
        assert calls[-1][:3] == ["launchctl", "bootstrap", "system"]
        for call in calls:
            assert "kickstart" not in call

    def test_chmods_plist_to_644(self, tmp_path):
        """LaunchDaemon plists should be mode 644 (Apple convention).
        NamedTemporaryFile produces 600 and `cp` preserves that."""
        calls: list[list[str]] = []

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch(
                "server.device.tunneld._run_sudo",
                side_effect=lambda args, timeout: (calls.append(args), True)[1],
            ),
            patch("server.device.tunneld.time.sleep"),
        ):
            assert install_daemon() == 0

        assert ["chmod", "644", str(tmp_path / "tunneld.plist")] in calls

    def test_bootstrap_retry_recovers_after_eio_race(self, tmp_path):
        """Regression: macOS 26 (Tahoe) launchd briefly races bootout/bootstrap
        and returns EIO 5 on the first bootstrap. install_daemon must retry
        once after a settle delay before giving up."""
        bootstrap_attempts = [0]

        def fake_run_sudo(args, timeout, non_interactive=False):
            if args[:3] == ["launchctl", "bootstrap", "system"]:
                bootstrap_attempts[0] += 1
                # First bootstrap fails (the race); second succeeds.
                return bootstrap_attempts[0] > 1
            return True

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.tunneld.time.sleep"),
        ):
            assert install_daemon() == 0

        # Both bootstrap attempts must have actually happened.
        assert bootstrap_attempts[0] == 2

    def test_bootstrap_failure_after_retry_returns_nonzero(self, tmp_path):
        """If bootstrap still fails after the settle retry, surface a
        diagnostic message and return 1 — don't silently fall back to
        anything destructive."""
        def fake_run_sudo(args, timeout, non_interactive=False):
            # bootstrap fails every time; everything else succeeds.
            return "bootstrap" not in args

        with (
            patch(
                "server.device.tunneld.find_pymobiledevice3_binary",
                return_value=Path("/usr/bin/pymobiledevice3"),
            ),
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "tunneld.plist"),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.tunneld.time.sleep"),
        ):
            assert install_daemon() == 1


# ---------------------------------------------------------------------------
# cli_tunneld
# ---------------------------------------------------------------------------


class TestRestartDaemon:
    """#73: restart is the command reached for when tunneld already looks
    wrong, so it must neither hang nor lie about having fixed anything."""

    @staticmethod
    def _patches(tmp_path, calls, serving, loaded=True):
        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        plist = tmp_path / "tunneld.plist"
        plist.write_text("")
        return (
            patch("server.device.tunneld.PLIST_PATH", plist),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.tunneld.time.sleep"),
            patch("server.device.tunneld._tunneld_devices", return_value=(serving, [])),
            patch(
                "server.device.tunneld.launchd_job",
                return_value={"state": "running", "pid": "1"} if loaded else {},
            ),
        )

    def test_loaded_job_is_signalled_not_reloaded(self, tmp_path):
        """KeepAlive respawns a killed daemon, so a loaded job needs only a
        signal — a smaller privilege than loading and unloading daemons."""
        calls: list[list[str]] = []
        with contextlib.ExitStack() as stack:
            for ctx in self._patches(tmp_path, calls, serving=True):
                stack.enter_context(ctx)
            assert _restart_daemon() == 0

        assert calls == [[LAUNCHCTL, "kill", "SIGKILL", f"system/{TUNNELD_LABEL}"]]
        assert not any("bootout" in c for c in calls)

    def test_unloaded_job_falls_back_to_bootout_bootstrap(self, tmp_path):
        calls: list[list[str]] = []
        with contextlib.ExitStack() as stack:
            for ctx in self._patches(tmp_path, calls, serving=True, loaded=False):
                stack.enter_context(ctx)
            assert _restart_daemon() == 0

        operations = [c[1] for c in calls]
        assert operations == ["bootout", "bootstrap"]

    def test_never_kickstart(self, tmp_path):
        """kickstart -k sends SIGKILL *and* rebuilds the job, and hung
        launchctl on macOS 15. Neither path may reach for it."""
        for loaded in (True, False):
            calls: list[list[str]] = []
            with contextlib.ExitStack() as stack:
                for ctx in self._patches(tmp_path, calls, serving=True, loaded=loaded):
                    stack.enter_context(ctx)
                _restart_daemon()
            for call in calls:
                assert "kickstart" not in call

    def test_failure_when_reloaded_but_not_serving(self, tmp_path):
        """The wedge is a live daemon with no listener, which launchd calls
        healthy. Trusting launchctl's exit code would report success for
        exactly the state the restart was meant to clear."""
        calls: list[list[str]] = []
        with contextlib.ExitStack() as stack:
            for ctx in self._patches(tmp_path, calls, serving=False):
                stack.enter_context(ctx)
            assert _restart_daemon() == 1

    def test_signal_failure_falls_through_to_reload(self, tmp_path):
        """A signal that does not bring the daemon back is not the end of the
        road — the heavier reload path is still worth trying."""
        calls: list[list[str]] = []
        serving = iter([(False, [])] * 10 + [(True, [])] * 10)

        plist = tmp_path / "tunneld.plist"
        plist.write_text("")
        with (
            patch("server.device.tunneld.PLIST_PATH", plist),
            patch(
                "server.device.tunneld._run_sudo",
                side_effect=lambda args, timeout, non_interactive=False: calls.append(args) or True,
            ),
            patch("server.device.tunneld.time.sleep"),
            patch("server.device.tunneld._tunneld_devices", side_effect=lambda: next(serving)),
            patch("server.device.tunneld.launchd_job", return_value={"state": "running"}),
        ):
            assert _restart_daemon() == 0

        operations = [c[1] for c in calls]
        assert operations == ["kill", "bootout", "bootstrap"]

    def test_not_installed_touches_nothing(self, tmp_path, capsys):
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        with (
            patch("server.device.tunneld.PLIST_PATH", tmp_path / "absent.plist"),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
        ):
            assert _restart_daemon() == 1
        assert calls == []
        assert "not installed" in capsys.readouterr().out


class TestCanRecoverUnattended:
    """The probe must answer "runs without a password", not "is permitted"."""

    GRANTED = (
        "User jerimiah may run the following commands on host:\n"
        "    (ALL) ALL\n"
        "    (root) NOPASSWD: /bin/launchctl kill SIGKILL system/com.quern.tunneld\n"
    )
    BLANKET_ONLY = (
        "User jerimiah may run the following commands on host:\n"
        "    (ALL) ALL\n"
    )

    def _listing(self, stdout, returncode=0):
        return patch(
            "subprocess.run",
            return_value=MagicMock(returncode=returncode, stdout=stdout),
        )

    def test_true_when_the_rule_is_present(self):
        with self._listing(self.GRANTED):
            assert can_recover_unattended() is True

    def test_false_on_blanket_sudo_alone(self):
        """Regression: `sudo -l <command>` returns 0 for anything an admin
        could run *with* a password, so a machine with `(ALL) ALL` and no grant
        reported unattended recovery as available. A false all-clear here means
        the automatic path silently does nothing."""
        with self._listing(self.BLANKET_ONLY):
            assert can_recover_unattended() is False

    def test_false_for_a_different_signal(self):
        """The grant names SIGKILL. A rule for one signal must not read as a
        rule for another."""
        with self._listing(self.GRANTED):
            with patch.object(
                tunneld_module,
                "RECOVERY_ARGS",
                ["/bin/launchctl", "kill", "SIGSTOP", "system/com.quern.tunneld"],
            ):
                assert can_recover_unattended() is False

    def test_false_when_sudo_refuses(self):
        with self._listing("", returncode=1):
            assert can_recover_unattended() is False

    def test_false_when_sudo_missing(self):
        with patch("subprocess.run", side_effect=OSError("no sudo")):
            assert can_recover_unattended() is False


class TestRecoveryIsNonInteractiveWhenAutomatic:
    def test_passes_dash_n_so_it_cannot_prompt(self):
        """With no terminal, a password prompt is indistinguishable from a
        hang, so the automatic path must fail instead of asking."""
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=1)) as run,
            patch("server.device.tunneld._wait_until_serving", return_value=False),
        ):
            assert recover_wedged_tunneld(non_interactive=True) is False
        assert run.call_args[0][0][:2] == ["sudo", "-n"]

    def test_interactive_by_default_for_the_cli(self):
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as run,
            patch("server.device.tunneld._wait_until_serving", return_value=True),
        ):
            assert recover_wedged_tunneld() is True
        assert run.call_args[0][0][:2] == ["sudo", "/bin/launchctl"]


class TestRecoveryGrant:
    """The grant is what turns "quern noticed" into "quern fixed it", so it
    has to be tight enough to be worth granting and safe enough to install."""

    def test_rule_is_fully_specified(self):
        rule = recovery_grant_line()
        assert "NOPASSWD:" in rule
        assert f"{LAUNCHCTL} kill SIGKILL system/{TUNNELD_LABEL}" in rule
        # No wildcard may reach the command spec: ALL after NOPASSWD, or a
        # glob in the path, would authorise far more than one signal.
        command_spec = rule.split("NOPASSWD:", 1)[1]
        assert "*" not in command_spec
        assert "ALL" not in command_spec

    def test_names_the_human_not_root_under_sudo(self, monkeypatch):
        """Under sudo, getpass.getuser() is root. A rule granting root the
        right to do what root can already do reports success and leaves the
        real user still prompted."""
        monkeypatch.setenv("SUDO_USER", "jerimiah")
        monkeypatch.setattr("getpass.getuser", lambda: "root")
        assert recovery_grant_line().startswith("jerimiah ALL=")

    def test_falls_back_to_current_user_without_sudo(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setattr("getpass.getuser", lambda: "someone")
        assert recovery_grant_line().startswith("someone ALL=")

    def test_refuses_to_install_invalid_syntax(self, capsys):
        """A malformed drop-in breaks sudo for everything on the machine, so
        validation must gate the install rather than follow it."""
        installed: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            installed.append(args)
            return True

        with (
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stderr="parse error near line 1"),
            ),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
        ):
            assert install_recovery_grant() == 1

        assert installed == [], "must not touch /etc/sudoers.d after visudo rejects it"
        assert "not valid sudoers syntax" in capsys.readouterr().out

    def test_installs_readonly_root_owned(self, capsys):
        """sudoers silently ignores a drop-in that is group- or world-writable,
        so the mode is load-bearing rather than cosmetic."""
        calls: list[list[str]] = []

        def fake_run_sudo(args, timeout, non_interactive=False):
            calls.append(args)
            return True

        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
            patch("server.device.tunneld._run_sudo", side_effect=fake_run_sudo),
            patch("server.device.tunneld.can_recover_unattended", return_value=True),
        ):
            assert install_recovery_grant() == 0

        assert calls[0][0] == "install"
        assert "0440" in calls[0]
        assert "root" in calls[0]

    def test_reports_when_grant_does_not_take_effect(self, capsys):
        """Installing the file and sudo honouring it are different facts, and
        reporting the first as the second would be a false all-clear."""
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")),
            patch("server.device.tunneld._run_sudo", return_value=True),
            patch("server.device.tunneld.can_recover_unattended", return_value=False),
        ):
            assert install_recovery_grant() == 1
        assert "still asks for a password" in capsys.readouterr().out


class TestCliTunneld:
    def test_help(self, capsys):
        result = cli_tunneld(["--help"])
        assert result == 0
        captured = capsys.readouterr()
        assert "install" in captured.out
        assert "uninstall" in captured.out

    def test_no_args_shows_help(self, capsys):
        result = cli_tunneld([])
        assert result == 0
        captured = capsys.readouterr()
        assert "Usage:" in captured.out

    def test_unknown_command(self, capsys):
        result = cli_tunneld(["bogus"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Unknown command" in captured.out

    def test_status_command(self):
        with (
            patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=None),
            patch("server.device.tunneld.PLIST_PATH") as mock_plist,
        ):
            mock_plist.exists.return_value = False
            with patch("urllib.request.urlopen", side_effect=Exception("refused")):
                result = cli_tunneld(["status"])
                assert result == 0
