"""Tests for simulator Web Inspector socket discovery.

The protocol paths need a booted simulator, so they are not unit-testable here.
What is worth locking down is the socket selection, because the failure it
guards against is silent: stale socket files outnumber live ones on any machine
that has booted more than one simulator.
"""

from __future__ import annotations

import os

from server.device import webinspector


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
        "SIM_SOCKET_GLOB",
        str(tmp_path / "com.apple.launchd.*" / "com.apple.webinspectord_sim.socket"),
    )
    assert webinspector.find_simulator_sockets() == [str(newer), str(older)]


def test_find_sockets_empty_when_none_present(tmp_path, monkeypatch):
    monkeypatch.setattr(webinspector, "SIM_SOCKET_GLOB", str(tmp_path / "nope" / "*.socket"))
    assert webinspector.find_simulator_sockets() == []


async def test_connect_reports_how_many_candidates_were_dead(tmp_path, monkeypatch):
    """A dead socket file must not be mistaken for an absent simulator."""
    dead = tmp_path / "com.apple.launchd.AAA" / "com.apple.webinspectord_sim.socket"
    dead.parent.mkdir(parents=True)
    dead.write_text("")
    monkeypatch.setattr(
        webinspector,
        "SIM_SOCKET_GLOB",
        str(tmp_path / "com.apple.launchd.*" / "com.apple.webinspectord_sim.socket"),
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
