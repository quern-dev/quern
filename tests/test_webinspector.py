"""Tests for simulator Web Inspector socket discovery.

The protocol paths need a booted simulator, so they are not unit-testable here.
What is worth locking down is the socket selection, because the failure it
guards against is silent: stale socket files outnumber live ones on any machine
that has booted more than one simulator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket

import pytest

from server.device.web import webinspector


def test_find_sockets_returns_newest_first(tmp_path, monkeypatch):
    older = tmp_path / "com.apple.launchd.AAA" / "com.apple.webinspectord_sim.socket"
    newer = tmp_path / "com.apple.launchd.BBB" / "com.apple.webinspectord_sim.socket"
    for p in (older, newer):
        p.parent.mkdir(parents=True)
        p.write_text("")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    monkeypatch.setattr(
        webinspector,
        "SIM_SOCKET_GLOBS",
        (str(tmp_path / "com.apple.launchd.*" / "com.apple.webinspectord_sim.socket"),),
    )
    assert webinspector.find_simulator_sockets() == [str(newer), str(older)]


def test_find_sockets_empty_when_none_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        webinspector, "SIM_SOCKET_GLOBS", (str(tmp_path / "nope" / "*.socket"),),
    )
    assert webinspector.find_simulator_sockets() == []


async def test_connect_reports_how_many_candidates_were_dead(tmp_path, monkeypatch):
    """A dead socket file must not be mistaken for an absent simulator."""
    dead = tmp_path / "com.apple.launchd.AAA" / "com.apple.webinspectord_sim.socket"
    dead.parent.mkdir(parents=True)
    dead.write_text("")
    monkeypatch.setattr(
        webinspector,
        "SIM_SOCKET_GLOBS",
        (str(tmp_path / "com.apple.launchd.*" / "com.apple.webinspectord_sim.socket"),),
    )

    inspector = webinspector.SimulatorWebInspector()
    try:
        await inspector.connect()
    except webinspector.WebInspectorError as exc:
        assert "1 candidate" in str(exc)
        assert "stale" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected WebInspectorError")


# ------------------------------------------------- attributing a connection

async def test_an_application_is_traced_to_its_simulator(monkeypatch):
    """The application id is the app's host pid, and simulator processes descend
    from a launchd_sim whose command line names the device directory."""
    udid = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
    tree = {
        "91962": {"command": "/…/Metatext.app/Metatext", "ppid": "28860"},
        "28860": {"command": f"launchd_sim /…/CoreSimulator/Devices/{udid}/data/var/run/x.plist",
                  "ppid": "1"},
    }

    async def fake_field(pid, field):
        return tree.get(pid, {}).get(field, "")

    monkeypatch.setattr(webinspector, "_process_field", fake_field)
    assert await webinspector.simulator_udid_for_application("PID:91962") == udid


async def test_a_process_outside_any_simulator_is_not_attributed(monkeypatch):
    async def fake_field(pid, field):
        return {"command": "/usr/bin/something", "ppid": "1"}[field]

    monkeypatch.setattr(webinspector, "_process_field", fake_field)
    assert await webinspector.simulator_udid_for_application("PID:5") is None


async def test_a_parent_walk_cannot_loop_forever(monkeypatch):
    """A cycle in reported parents must terminate rather than hang the request.

    The mock raises once the walk runs long, so a regressed guard fails here
    instead of looping forever -- an assertion after the call could never run.
    """
    calls = []

    async def fake_field(pid, field):
        calls.append(pid)
        if len(calls) > 40:
            raise AssertionError("parent walk did not terminate on a cycle")
        # A two-node cycle: 777 -> 888 -> 777. A self-parent would be caught by
        # the parent == pid check without ever exercising the bounded walk.
        return {"command": "no udid here",
                "ppid": "888" if pid == "777" else "777"}[field]

    monkeypatch.setattr(webinspector, "_process_field", fake_field)
    assert await webinspector.simulator_udid_for_application("PID:777") is None


async def test_non_pid_application_ids_are_not_attributed():
    assert await webinspector.simulator_udid_for_application("com.example.app") is None
    assert await webinspector.simulator_udid_for_application("") is None


# ------------------------------------------------- releasing the connection

class _FakeService:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeSock:
    def __init__(self):
        self.closed = False

    def fileno(self):
        return -1 if self.closed else 11

    def close(self):
        self.closed = True


async def test_closing_releases_the_service_transport_not_just_the_socket():
    """ServiceConnection owns an asyncio transport over the same descriptor.

    Closing only the raw socket leaves that transport polling a descriptor the
    OS may reissue, and the next connect() then fails with "File descriptor ...
    is used by transport" -- one reconnect poisoning the inspector until the
    server restarts. Reproduced live during an OAuth flow.
    """
    inspector = webinspector.SimulatorWebInspector()
    service, sock = _FakeService(), _FakeSock()
    inspector._service, inspector._sock = service, sock

    await inspector.close()

    assert service.closed, "the transport was left polling the descriptor"
    assert inspector._service is None
    assert inspector._sock is None


async def test_closing_forgets_state_tied_to_the_old_connection():
    """Sessions and targets are keyed to the connection that opened them; a
    reconnect that kept them would address pages through a dead session."""
    inspector = webinspector.SimulatorWebInspector()
    inspector._service, inspector._sock = _FakeService(), _FakeSock()
    inspector._sessions.add(("PID:1", 1))
    inspector._targets[("PID:1", 1)] = "target"
    inspector._applications["PID:1"] = {"bundle_id": "x"}

    await inspector.close()

    assert not inspector._sessions
    assert not inspector._targets
    assert not inspector._applications


async def test_closing_twice_is_harmless():
    inspector = webinspector.SimulatorWebInspector()
    inspector._service, inspector._sock = _FakeService(), _FakeSock()
    await inspector.close()
    await inspector.close()


async def test_a_service_that_fails_to_close_still_releases_the_socket():
    """Otherwise a raising transport would strand the descriptor for good."""
    class Exploding:
        async def close(self):
            raise OSError("already gone")

    inspector = webinspector.SimulatorWebInspector()
    sock = _FakeSock()
    inspector._service, inspector._sock = Exploding(), sock
    await inspector.close()
    assert sock.closed
    assert inspector._service is None


def test_both_socket_directories_are_searched(tmp_path, monkeypatch):
    """#184: Xcode 27 publishes the socket under /private/var/tmp, and quern
    looked only in /private/tmp — so every candidate it found predated the
    upgrade and `get_web_content` had no transport at all.

    Measured on the machine that reported it: 11 dead sockets in the old
    directory, and the live one in the new.
    """
    old_dir = tmp_path / "private" / "tmp" / "com.apple.launchd.OLD"
    new_dir = tmp_path / "private" / "var" / "tmp" / "com.apple.launchd.NEW"
    old = old_dir / "com.apple.webinspectord_sim.socket"
    new = new_dir / "com.apple.webinspectord_sim.socket"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.write_text("")
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))

    monkeypatch.setattr(webinspector, "SIM_SOCKET_GLOBS", (
        str(tmp_path / "private" / "var" / "tmp" / "com.apple.launchd.*"
            / "com.apple.webinspectord_sim.socket"),
        str(tmp_path / "private" / "tmp" / "com.apple.launchd.*"
            / "com.apple.webinspectord_sim.socket"),
    ))

    found = webinspector.find_simulator_sockets()
    assert str(new) in found, "the new location was not searched"
    assert str(old) in found, "the old location stopped being searched"
    assert found == [str(new), str(old)], "not newest-first across both"


def test_a_socket_found_by_both_patterns_is_listed_once(tmp_path, monkeypatch):
    """The two patterns can overlap on a machine where one path is a symlink
    to the other; a duplicate would be dialled twice for no reason."""
    sock = tmp_path / "com.apple.launchd.AAA" / "com.apple.webinspectord_sim.socket"
    sock.parent.mkdir(parents=True)
    sock.write_text("")
    pattern = str(tmp_path / "com.apple.launchd.*" / "com.apple.webinspectord_sim.socket")
    monkeypatch.setattr(webinspector, "SIM_SOCKET_GLOBS", (pattern, pattern))

    assert webinspector.find_simulator_sockets() == [str(sock)]


def test_both_real_socket_directories_are_configured():
    """The bug was the constant, not the search.

    Quern looked only under `/private/tmp` while Xcode 27 publishes the
    socket under `/private/var/tmp` (#184). Every test above patches
    `SIM_SOCKET_GLOBS`, so none of them can see that — this one asserts the
    shipped value, and it is the one that fails if the new location is
    dropped again.

    Measured on the reporting machine: 11 stale sockets in the old
    directory, the live one in the new, and launchd naming the new path via
    `launchctl print system/com.apple.webinspectord`.
    """
    globs = webinspector.SIM_SOCKET_GLOBS
    assert any(g.startswith("/private/var/tmp/") for g in globs), (
        f"the Xcode 27 location is not searched: {globs}"
    )
    assert any(g.startswith("/private/tmp/") for g in globs), (
        f"the pre-Xcode-27 location stopped being searched: {globs}"
    )
    assert all(
        g.endswith("/com.apple.webinspectord_sim.socket") for g in globs
    ), f"a pattern does not name the socket: {globs}"


# ── #551: pymobiledevice3 11.43 changed how a ServiceConnection is made ──

class _OldStyleService:
    """Before 11.43: the constructor took the socket."""

    def __init__(self, sock):
        self.sock = sock


class _NewStyleService:
    """11.43 and later: `from_socket` takes the socket; the constructor takes
    a reader and writer, so passing a socket fails as it does for real."""

    made_from: list = []

    def __init__(self, reader, writer, mux_device=None):
        self.reader, self.writer = reader, writer

    @classmethod
    async def from_socket(cls, sock):
        cls.made_from.append(sock)
        return cls("reader", "writer")


async def test_the_old_constructor_is_used_when_there_is_no_from_socket():
    sock = object()
    service = await webinspector._service_over(_OldStyleService, sock)
    assert service.sock is sock


async def test_from_socket_is_used_when_the_class_has_it():
    sock = object()
    _NewStyleService.made_from = []
    service = await webinspector._service_over(_NewStyleService, sock)
    assert _NewStyleService.made_from == [sock]
    assert (service.reader, service.writer) == ("reader", "writer")


def _listening_unix_socket():
    """A live socket for connect() to reach. Short path: macOS caps AF_UNIX
    paths at 104 bytes, which pytest's tmp_path can exceed."""
    import tempfile
    path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "wi.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    return server, path


async def test_a_failed_from_socket_is_a_webinspector_error_and_drops_the_socket(monkeypatch):
    """from_socket closes the socket itself when it fails, so connect() must
    let go of it rather than keep or reuse it."""
    import pymobiledevice3.service_connection as sc

    class Failing(_NewStyleService):
        @classmethod
        async def from_socket(cls, sock):
            sock.close()
            raise ConnectionError("device went away")

    monkeypatch.setattr(sc, "ServiceConnection", Failing)
    server, path = _listening_unix_socket()
    try:
        inspector = webinspector.SimulatorWebInspector(socket_path=path)
        with pytest.raises(webinspector.WebInspectorError, match="device went away"):
            await inspector.connect()
        assert inspector._sock is None and inspector._service is None
    finally:
        server.close()


async def test_the_installed_pymobiledevice3_makes_a_working_connection():
    """The real class, whatever version is installed: two connections over a
    socket pair, one plist across. CI installs the newest pymobiledevice3, so
    a release that changes construction again fails here rather than in a
    user's install -- 11.43 broke `ServiceConnection(sock)` and nothing here
    noticed, because every test used a stand-in."""
    from pymobiledevice3.service_connection import ServiceConnection

    a, b = socket.socketpair()
    left = await webinspector._service_over(ServiceConnection, a)
    right = await webinspector._service_over(ServiceConnection, b)
    try:
        await left.send_plist({"__selector": "_rpc_reportIdentifier:", "n": 1})
        got = await asyncio.wait_for(right.recv_plist(), timeout=5)
        assert got == {"__selector": "_rpc_reportIdentifier:", "n": 1}
    finally:
        for service in (left, right):
            with contextlib.suppress(Exception):
                await service.close()
        for sock in (a, b):
            with contextlib.suppress(Exception):
                sock.close()
