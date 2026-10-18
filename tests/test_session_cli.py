"""`quern session run|list|release` (#254), with the server and the socket faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server import session_cli


@pytest.fixture
def server(monkeypatch):
    calls = []
    answers = {}

    def fake_call(method, url, key, path, session="", body=None):
        calls.append({"method": method, "path": path, "session": session, "body": body})
        return answers.get((method, path), (200, {}))

    sockets = []

    class FakeSocket:
        lose = False

        def __init__(self, url, key, session, token, grace_seconds=120):
            self.session, self.token, self.closed = session, token, False
            self.lost = FakeSocket.lose
            sockets.append(self)

        def close(self):
            self.closed = True

        def _lose(self, code):
            self.lost = True

    monkeypatch.setattr(session_cli, "_server", lambda: ("http://127.0.0.1:9", "KEY"))
    monkeypatch.setattr(session_cli, "_call", fake_call)
    monkeypatch.setattr(session_cli, "_Socket", FakeSocket)
    monkeypatch.delenv("QUERN_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return SimpleNamespace(calls=calls, answers=answers, sockets=sockets, Socket=FakeSocket)


@pytest.fixture
def child(monkeypatch):
    seen = {}

    class FakePopen:
        pid = 4242

        def __init__(self, cmd, env, **kw):
            seen["cmd"], seen["env"], seen["popen_kw"] = cmd, env, kw

        def wait(self):
            return seen.get("rc", 0)

        def poll(self):
            return None

        def send_signal(self, sig):
            seen["signal"] = sig

    monkeypatch.setattr(session_cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(session_cli.signal, "signal", lambda *a: None)
    monkeypatch.setattr(session_cli, "_terminal", lambda: None)
    monkeypatch.setattr(session_cli, "_wait_as_a_shell", lambda pid, group, tty: seen.get("rc", 0))
    monkeypatch.setattr(session_cli, "_signal_group",
                        lambda group, signum: seen.__setitem__("signal", (group, signum)))
    return seen


def _opened(server, name="ci-job"):
    server.answers[("POST", "/api/v1/sessions")] = (200, {"id": name, "resume_token": "TOK"})
    server.answers[("POST", f"/api/v1/sessions/{name}/reservations")] = (
        200, {"holding": ["SIM-1", "SIM-2"]})
    server.answers[("GET", f"/api/v1/sessions/{name}")] = (
        200, {"reservations": [{"udid": "SIM-1"}, {"udid": "SIM-2"}]})


def test_run_holds_reserves_runs_and_releases(server, child):
    _opened(server)
    rc = session_cli.main(["run", "--name", "ci-job", "--reserve", "2", "--device-type",
                           "simulator", "--on-release", "shutdown_booted", "--",
                           "xcodebuild", "test", "--flag"])
    assert rc == 0
    assert child["cmd"] == ["xcodebuild", "test", "--flag"], "everything after -- is the command"
    env = child["env"]
    assert (env["QUERN_SESSION"], env["QUERN_SESSION_TOKEN"], env["QUERN_DEVICES"]) == \
        ("ci-job", "TOK", "SIM-1,SIM-2")
    reserve = next(c for c in server.calls if c["path"].endswith("/reservations"))
    assert reserve["session"] == "ci-job"
    assert reserve["body"] == {"on_release": "shutdown_booted", "auto_boot": True, "count": 2,
                               "device_type": "simulator"}
    assert server.sockets[0].closed, "the session is released when the command exits"


def test_the_childs_exit_code_is_the_answer(server, child):
    _opened(server)
    child["rc"] = 7
    assert session_cli.main(["run", "--name", "ci-job", "--", "false"]) == 7
    assert server.sockets[0].closed


@pytest.mark.parametrize("error, rc", [(FileNotFoundError(2, "No such file or directory"), 127),
                                       (PermissionError(13, "Permission denied"), 126)])
def test_a_command_that_cannot_start_exits_as_a_shell_would(server, child, monkeypatch,
                                                           capsys, error, rc):
    """It raised out of main as a traceback (CodeRabbit)."""
    _opened(server)

    def refuse(*a, **kw):
        raise error

    monkeypatch.setattr(session_cli.subprocess, "Popen", refuse)
    assert session_cli.main(["run", "--name", "ci-job", "--", "no-such-tool"]) == rc
    assert "cannot run no-such-tool" in capsys.readouterr().err
    assert server.sockets[0].closed, "the session is still released"


def test_a_refused_reservation_exits_2_and_still_releases(server, child):
    _opened(server)
    server.answers[("POST", "/api/v1/sessions/ci-job/reservations")] = (
        409, {"detail": "SIM-1 is reserved by session other"})
    assert session_cli.main(["run", "--name", "ci-job", "--udid", "SIM-1", "--", "true"]) == 2
    assert "cmd" not in child, "the command does not run without its devices"
    assert server.sockets[0].closed


def test_no_command_is_a_usage_error(server, child):
    assert session_cli.main(["run", "--reserve", "1"]) == 2


def test_list_and_release(server, capsys):
    server.answers[("GET", "/api/v1/sessions")] = (200, {"sessions": [
        {"id": "a", "state": "live", "grace_remaining_s": None,
         "reservations": [{"udid": "SIM-1"}]},
        {"id": "b", "state": "grace", "grace_remaining_s": 42.4, "reservations": []},
    ]})
    assert session_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "a  [live]  SIM-1" in out and "b  [grace, 42s left]  no devices" in out
    server.answers[("DELETE", "/api/v1/sessions/b")] = (200, {"freed": []})
    assert session_cli.main(["release", "b"]) == 0
    # Both name a session: a client key that names none is refused with 400,
    # and may release only a session it is (CodeRabbit, #254 phase 2).
    listing = next(c for c in server.calls if c["method"] == "GET")
    release = next(c for c in server.calls if c["method"] == "DELETE")
    assert listing["session"], "list sends a session header"
    assert release["session"] == "b", "release is sent as the session itself"


def test_no_server_exits_1(monkeypatch, capsys):
    def nothing():
        raise ConnectionError("no server answering")

    monkeypatch.setattr(session_cli, "_server", nothing)
    assert session_cli.main(["list"]) == 1
    assert "no server answering" in capsys.readouterr().err


def test_doctor_calls_out_a_session_in_grace(monkeypatch, capsys, tmp_path):
    import io
    import json

    from server import main as main_mod

    key = tmp_path / "api-key"
    key.write_text("K")
    monkeypatch.setattr("server.config.API_KEY_FILE", key)
    monkeypatch.setattr("server.__main__._server_base_url", lambda: "http://127.0.0.1:9")
    body = json.dumps({"sessions": [
        {"id": "claude-a", "state": "live", "reservations": [{"udid": "SIM-1"}]},
        {"id": "claude-b", "state": "grace", "grace_remaining_s": 95.2,
         "reservations": [{"udid": "SIM-2"}]},
    ]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: Resp(body))
    main_mod._report_held_sessions()
    out = capsys.readouterr().out
    assert "✓ claude-a: live, holding SIM-1" in out
    assert "⚠ claude-b: connection lost, holding SIM-2 for 95s more" in out
    assert "quern session release claude-b" in out


def test_doctor_with_no_server_says_not_checked(monkeypatch, capsys):
    from server import main as main_mod

    monkeypatch.setattr("server.__main__._server_base_url", lambda: None)
    main_mod._report_held_sessions()
    assert "not checked" in capsys.readouterr().out


def test_the_default_name_is_its_own_never_the_callers(server, child, monkeypatch):
    """Under Claude Code the caller's name is the agent's own MCP session; a
    run under it collided with, or took over, the agent's session (review)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc123")
    server.answers[("POST", "/api/v1/sessions")] = (200, {"id": "x", "resume_token": "T"})
    server.answers[("POST", "/api/v1/sessions/x/reservations")] = (200, {"holding": ["S"]})
    server.answers[("GET", "/api/v1/sessions/x")] = (200, {"reservations": [{"udid": "S"}]})
    session_cli.main(["run", "--", "true"])
    opened = next(c for c in server.calls if c["path"] == "/api/v1/sessions")
    name = opened["body"]["name"]
    assert name.startswith("claude-abc123.run-") and name != "claude-abc123"


def test_a_lost_hold_is_not_a_success(server, child):
    """The command succeeded but ran partly unprotected: exit 4, not 0."""
    _opened(server)
    server.Socket.lose = True
    try:
        assert session_cli.main(["run", "--name", "ci-job", "--", "true"]) == 4
        child["rc"] = 9
        assert session_cli.main(["run", "--name", "ci-job", "--", "false"]) == 9, \
            "a failing command keeps its own code"
    finally:
        server.Socket.lose = False


def test_a_hold_lost_unnoticed_by_the_socket_is_caught_at_the_end(server, child):
    """A server restart near the end of a run: the socket was still
    reconnecting when the command finished. The final check asks the server."""
    _opened(server)
    server.answers[("GET", "/api/v1/sessions/ci-job")] = (404, {"detail": "no held session"})
    assert session_cli.main(["run", "--name", "ci-job", "--", "true"]) == 4


def test_a_hold_that_cannot_be_confirmed_is_not_a_success(server, child):
    """The server could not be asked at the end: said as that, and exit 4."""
    _opened(server)
    server.answers[("GET", "/api/v1/sessions/ci-job")] = (503, {"detail": "busy"})
    assert session_cli.main(["run", "--name", "ci-job", "--", "true"]) == 4


def test_the_loss_warning_prints_once(capsys):
    """The socket thread and the final check can both find the loss (review)."""
    import threading

    sock = session_cli._Socket.__new__(session_cli._Socket)
    sock._session, sock.lost, sock._lock = "s", False, threading.Lock()
    sock._lose(4404)
    sock._lose("unconfirmed")
    err = capsys.readouterr().err
    assert err.count("lost its hold") == 1 and "no longer knows it" in err


def test_a_reconnect_after_close_is_closed_cleanly(monkeypatch):
    """Reconnected after the command finished: that socket is closed too, or
    the server would hold the devices for the grace period (review)."""
    import threading

    closed = []
    new_ws = type("WS", (), {"close": lambda self, code=1000, reason="": closed.append(code)})()
    sock = session_cli._Socket.__new__(session_cli._Socket)
    sock._session, sock.lost, sock._lock = "s", False, threading.Lock()
    sock._grace, sock._closing, sock._ws = 30, False, None

    def connect():
        sock._closing = True            # close() ran while the reconnect was in flight
        return new_ws

    monkeypatch.setattr(sock, "_connect", connect)
    monkeypatch.setattr(session_cli.time, "sleep", lambda s: None)
    assert sock._reconnect() is False
    assert closed == [1000] and sock._ws is None


class TestWaitingAsAShell:
    """The job-control loop, with waitpid and the terminal faked."""

    def _run(self, monkeypatch, statuses, tty=None, owners=None):
        """`owners`: what tcgetpgrp answers, in order (default: the child's
        group while it runs, this process's after it resumes)."""
        calls = []
        owner_seq = iter(owners or [4242, 100])
        monkeypatch.setattr(session_cli, "_owner", lambda fd: next(owner_seq))
        seq = iter(statuses)
        monkeypatch.setattr(session_cli.os, "waitpid", lambda pid, opts: (pid, next(seq)))
        monkeypatch.setattr(session_cli, "_hand_terminal_to",
                            lambda fd, pgrp: calls.append(("terminal", pgrp)))
        monkeypatch.setattr(session_cli, "_signal_group",
                            lambda group, sig: calls.append(("signal", group, sig)))
        monkeypatch.setattr(session_cli.os, "kill",
                            lambda pid, sig: calls.append(("stop-self", sig)))
        monkeypatch.setattr(session_cli.os, "getpgrp", lambda: 100)
        rc = session_cli._wait_as_a_shell(4242, 4242, tty)
        return rc, calls

    @staticmethod
    def exited(code):
        return code << 8

    @staticmethod
    def killed(sig):
        return sig

    @staticmethod
    def stopped(sig):
        return (sig << 8) | 0x7f

    @pytest.mark.parametrize("status,expected", [
        (0, 0), (3 << 8, 3), (15, 143), (2, 130),
    ])
    def test_status_as_the_shell_reports_it(self, monkeypatch, status, expected):
        """A child killed by signal N is 128+N: passed through as -N, sys.exit
        made SIGTERM 241, not 143 (CodeRabbit)."""
        assert self._run(monkeypatch, [status])[0] == expected

    def test_a_stopped_child_gives_the_terminal_back_and_resumes_on_continue(self, monkeypatch):
        """Ctrl-Z stops only the child's group now; waiting on regardless left
        the terminal held by a stopped job (CodeRabbit)."""
        import signal as sig

        rc, calls = self._run(monkeypatch, [self.stopped(sig.SIGTSTP), self.exited(0)], tty=0,
                              owners=[4242, 100])          # child held it; then `fg`
        assert rc == 0
        assert calls == [("terminal", 100), ("stop-self", sig.SIGSTOP),
                         ("terminal", 4242), ("signal", 4242, sig.SIGCONT)]

    def test_bg_continues_the_child_without_taking_the_terminal(self, monkeypatch):
        """After Ctrl-Z, `bg` keeps the terminal with the shell; handing it to
        the child then took it from the shell (CodeRabbit)."""
        import signal as sig

        rc, calls = self._run(monkeypatch, [self.stopped(sig.SIGTSTP), self.exited(0)], tty=0,
                              owners=[4242, 7])            # after resume: the shell's
        assert ("terminal", 4242) not in calls[1:], "the terminal stays with the shell"
        assert calls[-1] == ("signal", 4242, sig.SIGCONT)

    def test_started_in_the_background_a_stop_is_still_handled(self, monkeypatch):
        """Started with `&`, the child never had the terminal; a stop must still
        stop this job, so the shell can report and resume it (CodeRabbit)."""
        import signal as sig

        rc, calls = self._run(monkeypatch, [self.stopped(sig.SIGTTIN), self.exited(0)], tty=0,
                              owners=[7, 100])             # the shell's; then `fg`
        assert ("stop-self", sig.SIGSTOP) in calls
        assert calls[-2:] == [("terminal", 4242), ("signal", 4242, sig.SIGCONT)]


def test_the_child_is_its_own_job_and_each_signal_arrives_once(server, child, monkeypatch):
    """Its own process group, so a signal to this process or its group reaches
    the child only by forwarding -- once. Inferring 'from the terminal' from
    foreground membership dropped a kill -INT aimed at this process
    (CodeRabbit)."""
    import signal as sig

    installed = {}

    def fake_signal(s, h):
        previous = installed.get(s, "original")
        installed[s] = h
        return previous

    monkeypatch.setattr(session_cli.signal, "signal", fake_signal)
    monkeypatch.setattr(session_cli, "_terminal", lambda: None)
    during = {}

    def wait(pid, group, tty):
        during.update(installed)            # the handlers while the child runs
        return 0

    _opened(server)
    monkeypatch.setattr(session_cli, "_wait_as_a_shell", wait)
    session_cli.main(["run", "--name", "ci-job", "--", "x"])
    assert child["popen_kw"].get("process_group") == 0
    during[sig.SIGINT](sig.SIGINT, None)
    assert child["signal"] == (4242, sig.SIGINT), "SIGINT forwarded, to the child's group"
    during[sig.SIGTERM](sig.SIGTERM, None)
    assert child["signal"] == (4242, sig.SIGTERM), "SIGTERM forwarded, to the child's group"
    assert installed[sig.SIGINT] == "original" and installed[sig.SIGTERM] == "original", \
        "handlers put back once the child is gone, before the hold check"
