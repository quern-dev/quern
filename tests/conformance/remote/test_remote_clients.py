"""Two paired clients and one simulator: what a client key may change.

A client may watch everything a shared server shows, change only devices it
holds, and never change one another client holds. The unit suite checks each
rule against a fake registry; this checks them as two real pairings over TLS,
where the principal comes from the key the listener was handed.
"""

from __future__ import annotations

import contextlib

from tests.conformance import client as client_mod

from .conftest import Server, session_for


def _press(client: client_mod.QuernClient, udid: str, session: str | None = None):
    """A harmless write: the home button."""
    headers = {"X-Quern-Session": session} if session else {}
    return client.post("/api/v1/device/ui/press", json={"button": "HOME", "udid": udid},
                       headers=headers, timeout=60)


def _reserve(client: client_mod.QuernClient, held: client_mod.HeldSession, udid: str):
    return client.post(f"/api/v1/sessions/{held.name}/reservations",
                       json={"udids": [udid], "auto_boot": False},
                       headers={"X-Quern-Session": held.name}, timeout=60)


def _release(client: client_mod.QuernClient, held: client_mod.HeldSession):
    return client.delete(f"/api/v1/sessions/{held.name}/reservations",
                         headers={"X-Quern-Session": held.name}, timeout=120)


@contextlib.contextmanager
def _holding(client: client_mod.QuernClient, udid: str):
    held = session_for(client)
    try:
        resp = _reserve(client, held, udid)
        assert resp.status_code == 200, resp.text
        yield held
    finally:
        with contextlib.suppress(Exception):
            _release(client, held)
        held.close()


def test_a_client_cannot_change_a_device_it_does_not_hold(as_alice, simulator) -> None:
    resp = _press(as_alice, simulator)
    assert resp.status_code == 403, resp.text
    assert "reserve" in resp.text, "the refusal should say how to get the device"


def test_the_holder_may_change_its_device(as_alice, simulator) -> None:
    with _holding(as_alice, simulator) as held:
        resp = _press(as_alice, simulator, held.name)
    assert resp.status_code == 200, resp.text


def test_another_client_is_refused_and_told_who_holds_it(
    as_alice, as_bob, simulator,
) -> None:
    with _holding(as_alice, simulator):
        resp = _press(as_bob, simulator)
    assert resp.status_code == 423, resp.text
    assert resp.json()["detail"]["held_by"].startswith("alice:"), resp.text


def test_another_client_can_still_watch_it(as_alice, as_bob, simulator) -> None:
    """Shared by default: reads work for every client, whoever holds it."""
    with _holding(as_alice, simulator):
        shot = as_bob.get("/api/v1/device/screenshot",
                          params={"udid": simulator, "scale": 0.2}, timeout=60)
    assert shot.status_code == 200, shot.text[:300]
    assert shot.headers.get("content-type", "").startswith("image/")
    assert len(shot.content) > 1000, "a screenshot with no image in it"


def test_a_held_device_cannot_be_reserved_by_another_client(
    as_alice, as_bob, simulator,
) -> None:
    with _holding(as_alice, simulator):
        bob = session_for(as_bob)
        try:
            resp = _reserve(as_bob, bob, simulator)
        finally:
            bob.close()
    assert resp.status_code == 409, resp.text


def test_a_released_device_is_free_for_the_next_client(as_alice, as_bob, simulator) -> None:
    with _holding(as_alice, simulator) as held:
        freed = _release(as_alice, held)
        assert freed.status_code == 200, freed.text
        assert simulator in freed.json()["freed"]
        with _holding(as_bob, simulator) as bob:
            resp = _press(as_bob, simulator, bob.name)
    assert resp.status_code == 200, resp.text


def test_a_blind_server_shows_a_client_only_what_it_holds(
    restore_server: Server, alice, bob, simulator,
) -> None:
    """`sessions.visibility: blind`, for a server shared by people who should
    not see each other's work."""
    restore_server.restart(visibility="blind")
    as_alice, as_bob = alice.client(restore_server), bob.client(restore_server)
    try:
        with _holding(as_alice, simulator):
            mine = as_alice.json_ok("GET", "/api/v1/device/list", timeout=60)["devices"]
            theirs = as_bob.json_ok("GET", "/api/v1/device/list", timeout=60)["devices"]
            shot = as_bob.get("/api/v1/device/screenshot",
                              params={"udid": simulator, "scale": 0.2}, timeout=60)
    finally:
        as_alice.close()
        as_bob.close()
    assert simulator in {d["udid"] for d in mine}, "the holder lost sight of its device"
    assert simulator not in {d["udid"] for d in theirs}, "blind showed another's device"
    assert shot.status_code != 200, "blind let another client watch the device"
