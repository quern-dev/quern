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
