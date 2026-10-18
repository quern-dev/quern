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
        def __init__(self, url, key, session, token):
            self.session, self.token, self.closed = session, token, False
            sockets.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(session_cli, "_server", lambda: ("http://127.0.0.1:9", "KEY"))
    monkeypatch.setattr(session_cli, "_call", fake_call)
    monkeypatch.setattr(session_cli, "_Socket", FakeSocket)
    monkeypatch.delenv("QUERN_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return SimpleNamespace(calls=calls, answers=answers, sockets=sockets)


@pytest.fixture
def child(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, env):
            seen["cmd"], seen["env"] = cmd, env

        def wait(self):
            return seen.get("rc", 0)

        def send_signal(self, sig):
            seen["signal"] = sig

    monkeypatch.setattr(session_cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(session_cli.signal, "signal", lambda *a: None)
    return seen


def _opened(server, name="ci-job"):
    server.answers[("POST", "/api/v1/sessions")] = (200, {"id": name, "resume_token": "TOK"})
    server.answers[("POST", f"/api/v1/sessions/{name}/reservations")] = (
        200, {"holding": ["SIM-1", "SIM-2"]})


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
