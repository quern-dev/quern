"""Tests for server.lifecycle.ports — port availability and scanning."""

from __future__ import annotations

import socket

import pytest

from server.lifecycle.ports import find_available_port, is_port_available


def test_port_available_when_free():
    """A port that nobody is using should be available."""
    # Use a high ephemeral port unlikely to be in use
    assert is_port_available(59123) is True


def test_port_taken_when_bound():
    """A port that's already bound should not be available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 59124))
        s.listen(1)
        assert is_port_available(59124) is False


def test_find_available_returns_preferred_when_free():
    """find_available_port should return the preferred port if it's free."""
    port = find_available_port(59125)
    assert port == 59125


def test_find_available_skips_taken():
    """find_available_port should skip ports that are in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 59126))
        s.listen(1)
        port = find_available_port(59126)
        assert port == 59127


def test_find_available_respects_exclude():
    """find_available_port should skip ports in the exclude set."""
    port = find_available_port(59128, exclude={59128, 59129})
    assert port == 59130


def test_find_available_raises_when_exhausted():
    """find_available_port should raise RuntimeError when all ports are taken."""
    # Use max_attempts=1 with the port excluded
    with pytest.raises(RuntimeError, match="No available port found"):
        find_available_port(59131, max_attempts=1, exclude={59131})


class TestWhatCountsAsAQuernProcess:
    """`reclaim_port` SIGTERMs and then SIGKILLs whatever this says yes to,
    so a loose answer here is a loose answer to "may I kill that process".

    It used to say yes to any argv containing `uvicorn`. That is the most
    widely used ASGI server in Python, so an unrelated app holding the port
    was identified as a stale quern and killed by `quern start` — on 9100,
    which is also Prometheus node_exporter's default and the JetDirect
    printing port, so sharing it is ordinary rather than unlucky. Quern's own
    daemon never matched that pattern anyway: its argv is `<python> -m
    server`.
    """

    @staticmethod
    def _argv(argv: str) -> bool:
        from types import SimpleNamespace
        from unittest.mock import patch

        from server.lifecycle.ports import _is_quern_process

        with patch(
            "server.lifecycle.ports.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=argv),
        ):
            return _is_quern_process(1234)

    @pytest.mark.parametrize("argv", [
        "/opt/homebrew/bin/python3.12 -m server",
        "/Users/x/.local/share/quern/.venv/bin/python -m server --port 9100",
        "/usr/bin/python3 -m server.main",
    ])
    def test_ours_is_recognised(self, argv):
        assert self._argv(argv) is True, "a stale quern would never be reclaimed"

    @pytest.mark.parametrize("argv", [
        # The regression. Someone else's ASGI app, which quern would have
        # killed to take the port.
        "/usr/local/bin/uvicorn myapp:api --host 0.0.0.0 --port 9100",
        "/usr/bin/python3 -m uvicorn myapp:app",
        # A module whose name merely starts the same way.
        "/usr/bin/python3 -m serverfoo --port 9100",
        # node_exporter, which owns 9100 by convention.
        "/usr/local/bin/node_exporter --web.listen-address=:9100",
        "",
    ])
    def test_somebody_else_is_not_killed(self, argv):
        assert self._argv(argv) is False, f"quern would have killed: {argv}"

    def test_the_mitmdump_addon_has_to_be_ours(self):
        """An orphaned mitmdump running *our* addon is ours to clean up. A
        file that merely shares the path tail is not."""
        assert self._argv(
            "mitmdump -s /Users/x/.local/share/quern/server/proxy/addon.py"
        ) is True
        assert self._argv("mitmdump -s /Users/x/vendor/proxy/addon.py") is False


class TestTheChildIsToldWhichPort:
    """`daemonize` re-invokes the command for the child, and used to rebuild
    it from `sys.argv` — so the child re-derived the port instead of being
    told the one the parent had settled on.

    They agreed only by coincidence. `quern start --port N` put the flag in
    argv and the child re-parsed it; `quern restart` has an empty argv, so the
    child bound the default while the parent waited on health at the port the
    server had actually been using. The symptom was "Server started but
    health check timed out after 30.1s" beside a perfectly healthy server on
    another port, and it made `quern update`'s restart fail on any install not
    using 9100.
    """

    def test_port_flags_are_stripped_before_the_resolved_ones_are_added(self):
        from server.lifecycle.daemon import _without_port_flags

        assert _without_port_flags(["--port", "9190", "--verbose"]) == ["--verbose"]
        assert _without_port_flags(["--proxy-port", "9191", "-v"]) == ["-v"]
        assert _without_port_flags(["--port=9190", "--oslog"]) == ["--oslog"]
        assert _without_port_flags(["--proxy-port=9191"]) == []
        assert _without_port_flags(["--process", "Safari"]) == ["--process", "Safari"]

    def test_the_child_command_carries_the_resolved_ports(self, monkeypatch, tmp_path):
        """What the parent resolved, not what the user typed: the parent may
        have scanned upward because the port was busy."""
        import subprocess as sp

        from server.lifecycle import daemon

        seen = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                seen["cmd"] = cmd
                self.pid = 4321

        monkeypatch.setattr(daemon.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "server.log")
        monkeypatch.setattr(daemon.sys, "argv", ["quern", "restart"])
        monkeypatch.setattr(daemon, "_parent_wait_and_exit",
                            lambda pid, port: None)

        daemon.daemonize(9190, 9191)

        cmd = seen["cmd"]
        assert "--port" in cmd and cmd[cmd.index("--port") + 1] == "9190", cmd
        assert "--proxy-port" in cmd and cmd[cmd.index("--proxy-port") + 1] == "9191", cmd
        assert cmd.count("--port") == 1, f"two --port would let the wrong one win: {cmd}"
        assert sp  # imported for the type only

    def test_an_argv_port_does_not_survive_to_contradict_it(self, monkeypatch, tmp_path):
        from server.lifecycle import daemon

        seen = {}
        monkeypatch.setattr(daemon.subprocess, "Popen",
                            lambda cmd, **kw: seen.update(cmd=cmd)
                            or type("P", (), {"pid": 1})())
        monkeypatch.setattr(daemon, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "server.log")
        monkeypatch.setattr(daemon.sys, "argv", ["quern", "start", "--port", "9100"])
        monkeypatch.setattr(daemon, "_parent_wait_and_exit", lambda pid, port: None)

        daemon.daemonize(9102, None)

        cmd = seen["cmd"]
        assert cmd.count("--port") == 1
        assert cmd[cmd.index("--port") + 1] == "9102", (
            "the scanned-for port lost to the one the user asked for and "
            "could not have"
        )
