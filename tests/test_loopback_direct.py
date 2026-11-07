"""quern's requests to itself, tunneld, WDA and the viewer's stream go direct:
never through a proxy from the environment, nor the macOS system proxy that
`configure_system_proxy` turns on (#634). Through one, a proxy that is down
made a running server read as stopped, and quern's own device traffic went
through quern's own mitmproxy.

The functions that can be driven are driven over real loopback sockets. The
rest -- thirteen httpx clients, mostly WDA calls deep in a device flow -- are
held by a scan below: every client in server/ goes direct or is listed with
the reason it may use a proxy.
"""

from __future__ import annotations

import ast
import urllib.request
from pathlib import Path

import pytest

from tests.test_loopback_opener import _json, _Recorder

SERVER = Path(__file__).resolve().parent.parent / "server"


@pytest.fixture
def proxy(monkeypatch):
    """A live proxy, set the two ways a request can find one: the environment
    for urllib, and -- for httpx, which reads it from `getproxies()` -- the
    shape macOS returns while the system proxy is on, with no exceptions
    list, since httpx reads none."""
    recorder = _Recorder(_json({"proxied": True}))
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, recorder.url)
    monkeypatch.setattr("httpx._utils.getproxies",
                        lambda: {"http": recorder.url, "https": recorder.url})
    monkeypatch.setattr(urllib.request, "_opener", None)
    yield recorder
    recorder.shutdown()
    recorder.server_close()


@pytest.fixture
def server():
    s = _Recorder(_json({"status": "ok"}))
    yield s
    s.shutdown()
    s.server_close()


def _port(s: _Recorder) -> int:
    return s.server_address[1]


def test_a_running_server_is_found_with_a_proxy_set(proxy, server):
    from server.lifecycle import state

    assert state.is_server_healthy(_port(server))
    assert proxy.seen == []
    assert [path for path, _ in server.seen] == ["/health"]


def test_a_running_server_is_found_with_a_dead_proxy_set(monkeypatch, server):
    """The report on #634: an unreachable proxy, and every CLI command said
    "no server answering"."""
    from server.lifecycle import state

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)

    assert state.is_server_healthy(_port(server))


def test_the_servers_tools_are_read_direct(proxy, server):
    from server.lifecycle import state

    assert state.fetch_tools(_port(server)) == {"status": "ok"}
    assert proxy.seen == []


def test_tunneld_is_asked_direct_from_the_cli(proxy, server, monkeypatch):
    from server.device.ios import tunneld

    monkeypatch.setattr(tunneld, "TUNNELD_URL", server.url)

    assert tunneld._tunneld_devices() == (True, {"status": "ok"})
    assert proxy.seen == []


async def test_tunneld_is_asked_direct_from_the_server(proxy, server, monkeypatch):
    from server.device.ios import tunneld

    monkeypatch.setattr(tunneld, "TUNNELD_URL", server.url)

    assert await tunneld.is_tunneld_running()
    assert await tunneld.get_tunneld_devices() == {"status": "ok"}
    assert proxy.seen == []
    assert len(server.seen) == 2


async def test_wda_is_polled_direct(proxy, server):
    from server.device.ios import wda

    assert await wda._poll_wda_status(server.url, timeout=5)
    assert proxy.seen == []


def test_the_proxy_in_these_tests_is_one_httpx_would_use(proxy, server):
    """Without this, the httpx tests above pass however httpx is built."""
    import httpx

    with httpx.Client() as client:
        assert client.get(server.url + "/x").json() == {"proxied": True}

    assert [path for path, _ in proxy.seen] == [server.url + "/x"]


# -- every client in server/ ------------------------------------------------

#: The ones that may use a proxy, and why: each reaches the internet, where a
#: proxy may be the only way out, or is quern's own proxy on purpose.
MAY_USE_A_PROXY = {
    ("lifecycle/update_check.py", "urlopen"): "quern.dev",
    ("lifecycle/updater.py", "urlopen"): "GitHub and quern.dev",
    ("lifecycle/setup.py", "urlopen"): "GitHub downloads",
    ("profiling/trace_processor.py", "urlopen"): "a release download",
    ("tooling/tool_updates.py", "AsyncClient"): "PyPI",
    ("device/ios/wda_source.py", "AsyncClient"): "WebDriverAgent's tarball on GitHub",
    ("api/proxy_intercept.py", "AsyncClient"): "replays through quern's own proxy",
}

#: urlopen calls each file is allowed: setup.py's two downloads only, so a
#: loopback check added beside them is not covered by its entry.
URLOPEN_COUNTS = {"lifecycle/setup.py": 2, "lifecycle/updater.py": 3}


def _clients() -> list[tuple[str, str, int, bool]]:
    """(file, kind, line, direct) for each urlopen and httpx client in server/.

    A reference to `urlopen` counts, not only a call: #613 missed one that
    was handed out by name."""
    found = []
    for path in sorted(SERVER.rglob("*.py")):
        rel = path.relative_to(SERVER).as_posix()
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Attribute) and node.attr == "urlopen":
                found.append((rel, "urlopen", node.lineno, False))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("AsyncClient", "Client")
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx"):
                direct = any(k.arg == "trust_env" and isinstance(k.value, ast.Constant)
                             and k.value.value is False for k in node.keywords)
                found.append((rel, "AsyncClient", node.lineno, direct))
    return found


def test_every_client_goes_direct_or_says_why_it_may_not():
    undeclared = [f"{rel}:{line} ({kind})" for rel, kind, line, direct in _clients()
                  if not direct and (rel, kind) not in MAY_USE_A_PROXY]

    assert undeclared == [], (
        "these may go through a proxy: use client.loopback_opener() or "
        "httpx's trust_env=False, or add the file to MAY_USE_A_PROXY with "
        f"why it reaches the internet: {undeclared}")


def test_a_file_allowed_a_proxy_has_no_extra_urlopen():
    counts: dict[str, int] = {}
    for rel, kind, _, _ in _clients():
        if kind == "urlopen":
            counts[rel] = counts.get(rel, 0) + 1

    for rel, allowed in URLOPEN_COUNTS.items():
        assert counts.get(rel, 0) == allowed, (
            f"{rel} has {counts.get(rel, 0)} urlopen calls, {allowed} of which "
            "reach the internet: is the new one to quern, tunneld or a device?")


def test_the_scan_sees_what_it_is_meant_to():
    """It found #613's reference-not-call case when that was the bug, and it
    sees both kinds now. Without this, a scan that matched nothing passes."""
    kinds = {kind for _, kind, _, _ in _clients()}
    assert kinds == {"urlopen", "AsyncClient"}
    assert sum(1 for *_, direct in _clients() if direct) >= 13
    tree = ast.parse("f = urllib.request.urlopen\n")
    assert any(isinstance(n, ast.Attribute) and n.attr == "urlopen" for n in ast.walk(tree))
