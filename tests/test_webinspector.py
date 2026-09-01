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
