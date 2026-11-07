"""The CLI's calls to this Mac's own server carry the owner's key, or a
client's, so they go straight to it: never through a proxy, and never after a
redirect, either of which urllib would hand the Authorization header (#613).

Measured over real loopback sockets rather than by inspecting the opener: what
matters is where the key arrived.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from collections.abc import Callable, Iterator

import pytest

KEY = "owner-key-613"


class _Recorder(http.server.ThreadingHTTPServer):
    """A loopback server that records what reached it and answers with
    `answer(path) -> (status, headers, body)`."""

    def __init__(self, answer: Callable[[str], tuple[int, dict, bytes]]) -> None:
        self.seen: list[tuple[str, str]] = []     # (request target, Authorization)
        self.answer = answer
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                recorder.seen.append((self.path, self.headers.get("Authorization", "")))
                status, headers, body = recorder.answer(self.path)
                self.send_response(status)
                for name, value in {"Content-Length": str(len(body)), **headers}.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            # CONNECT is how a WebSocket client asks a proxy for a tunnel.
            do_GET = do_POST = do_DELETE = do_CONNECT = _any

            def log_message(self, *args) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def _json(body: dict) -> Callable[[str], tuple[int, dict, bytes]]:
    return lambda path: (200, {"Content-Type": "application/json"}, json.dumps(body).encode())


#: An answer every call site reads as success, so a leaked call is not hidden
#: behind an error: the disk report, a pairing code, a session list.
_OK = {"sessions": [], "code": "c", "id": "s", "stores": {}, "devices": []}


@pytest.fixture
def servers(monkeypatch) -> Iterator[dict[str, _Recorder]]:
    """`server`, the quern being asked; `proxy`, set as the environment's
    proxy; `sink`, where `redirector` sends every request."""
    started = {"server": _Recorder(_json(_OK)), "proxy": _Recorder(_json(_OK)),
               "sink": _Recorder(_json(_OK))}
    sink = started["sink"].url
    started["redirector"] = _Recorder(lambda path: (302, {"Location": sink + path}, b""))
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, started["proxy"].url)
    # `urlopen` builds one opener on first use and keeps the proxies it read
    # then. Without this, an earlier test's call fixes them: one made with no
    # proxy set lets a regression through, and one made under another test's
    # proxy dials a server that is gone. Measured: the control below failed
    # after the other tests had run against the unfixed code.
    monkeypatch.setattr(urllib.request, "_opener", None)
    yield started
    for s in started.values():
        s.shutdown()
        s.server_close()


def _owner_call(base: str) -> None:
    from server import remote_cli

    remote_cli._owner_call("GET", "/api/v1/remote/status")


def _held_sessions(base: str) -> None:
    from server import main

    main._report_held_sessions()


def _session_open(base: str) -> None:
    from server import session_cli

    req = urllib.request.Request(base + "/api/v1/sessions",
                                 headers={"Authorization": f"Bearer {KEY}"})
    try:
        session_cli._open(req, timeout=5).close()
    except urllib.error.HTTPError:
        pass


def _status_get(base: str) -> None:
    from server.remote import client, status

    try:
        status._get(client.Remote(name="local", url=base, fingerprint="", key=KEY),
                    "/api/v1/device/list", 5)
    except urllib.error.HTTPError:
        pass


def _disk_fetch(base: str) -> None:
    from server import disk_usage

    disk_usage.fetch(int(base.rsplit(":", 1)[1]), timeout=5)


def _recording_call(base: str) -> None:
    from server.recording import cli

    cli._call("GET", "/api/v1/recordings")


def _session_socket(base: str) -> None:
    """`quern session hold` and `run` hold a WebSocket, with the key in its
    upgrade request. Nothing here answers it as a WebSocket, so it fails
    after the request is sent: where the request went is the point."""
    from server import session_cli

    try:
        session_cli._Socket(base, KEY, "s613", "token").close()
    except Exception:
        pass


CALL_SITES = {"remote_cli._owner_call": _owner_call,
              "recording.cli._call": _recording_call,
              "session_cli._Socket": _session_socket,
              "main._report_held_sessions": _held_sessions,
              "session_cli._open": _session_open,
              "remote.status._get": _status_get,
              "disk_usage.fetch": _disk_fetch}


@pytest.fixture
def aimed_at(monkeypatch, tmp_path):
    """Point every call site at `base`, with the owner's key on disk."""
    key_file = tmp_path / "api-key"
    key_file.write_text(KEY)
    monkeypatch.setattr("server.config.API_KEY_FILE", key_file)
    monkeypatch.setenv("QUERN_CLIENT_KEY", KEY)
    monkeypatch.delenv("QUERN_REMOTE", raising=False)
    monkeypatch.setattr("server.session_cli._remote", lambda: None)

    def aim(base: str) -> None:
        monkeypatch.setattr("server.__main__._server_base_url", lambda: base)
    return aim


@pytest.mark.parametrize("site", CALL_SITES)
def test_the_key_goes_to_the_server_and_not_to_the_environments_proxy(
    servers, aimed_at, site,
):
    aimed_at(servers["server"].url)

    CALL_SITES[site](servers["server"].url)

    assert servers["proxy"].seen == [], f"{site} went through the proxy"
    assert [auth for _, auth in servers["server"].seen] == [f"Bearer {KEY}"]


@pytest.mark.parametrize("site", CALL_SITES)
def test_a_redirect_is_not_followed_with_the_key(servers, aimed_at, site):
    """quern's routes answer where they are asked, so a 3xx is not quern's:
    whatever answered gets nothing more."""
    aimed_at(servers["redirector"].url)

    CALL_SITES[site](servers["redirector"].url)

    assert len(servers["redirector"].seen) == 1
    assert servers["sink"].seen == [], f"{site} followed the redirect"
    assert servers["proxy"].seen == []


def test_a_refused_redirect_reads_as_an_answer_not_a_crash(servers, aimed_at):
    """Each caller already says an HTTPError; the 302 must arrive as one."""
    from server import remote_cli

    aimed_at(servers["redirector"].url)
    status, _ = remote_cli._owner_call("GET", "/api/v1/remote/status")

    assert status == 302


def test_the_proxy_in_these_tests_is_one_urllib_would_use(servers):
    """Without this, every test above passes on a machine where urllib would
    never have used the proxy anyway."""
    with urllib.request.urlopen(servers["server"].url + "/x", timeout=5):
        pass

    assert [path for path, _ in servers["proxy"].seen] == [servers["server"].url + "/x"]
    assert servers["server"].seen == []
