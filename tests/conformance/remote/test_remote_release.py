"""What a client's release puts back, and how long a dropped client keeps its hold.

Under the default `client_release: uninstall`, the apps a client installed are
removed when its hold ends -- and only those: an app that was there before its
install stays, and the release says why (#743). A client whose connection drops
without a goodbye keeps its devices for the grace period, then loses them.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conformance import client as client_mod
from tests.conformance import probe as probe_mod

from .conftest import MIN_GRACE_S, Server, drop, session_for


@pytest.fixture(scope="session")
def probe_bundle() -> Path:
    try:
        return probe_mod.build_ios()
    except probe_mod.ProbeUnavailable as e:
        pytest.skip(f"the probe app could not be built: {e}")


def _apps(owner: client_mod.QuernClient, udid: str) -> set[str]:
    body = owner.json_ok("GET", "/api/v1/device/app/list", params={"udid": udid}, timeout=60)
    return {a["bundle_id"] for a in body["apps"]}


def _upload(client: client_mod.QuernClient, bundle: Path) -> str:
    """Send a .app as the MCP does: a gzipped tar of the bundle."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        tar.add(bundle, arcname=bundle.name)
    resp = client.post("/api/v1/uploads", params={"name": f"{bundle.name}.tar.gz"},
                       content=gzip.compress(raw.getvalue()),
                       headers={"Content-Type": "application/octet-stream"}, timeout=120)
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def _install(client: client_mod.QuernClient, held: client_mod.HeldSession, udid: str,
             bundle: Path) -> None:
    resp = client.post("/api/v1/device/app/install",
                       json={"udid": udid, "upload_id": _upload(client, bundle)},
                       headers={"X-Quern-Session": held.name}, timeout=300)
    assert resp.status_code == 200, resp.text


def _release(client: client_mod.QuernClient, held: client_mod.HeldSession) -> dict:
    return client.json_ok("DELETE", f"/api/v1/sessions/{held.name}/reservations",
                          headers={"X-Quern-Session": held.name}, timeout=300)


@pytest.fixture
def no_probe(as_owner: client_mod.QuernClient, simulator: str) -> Iterator[None]:
    """Start and end without the probe on the simulator, so each test sees
    only what it did."""
    def remove() -> None:
        if probe_mod.BUNDLE_ID in _apps(as_owner, simulator):
            as_owner.json_ok("POST", "/api/v1/device/app/uninstall", timeout=120,
                             json={"udid": simulator, "bundle_id": probe_mod.BUNDLE_ID})
    remove()
    try:
        yield
    finally:
        remove()


def test_an_app_a_client_installed_is_removed_when_it_releases(
    as_alice, as_owner, simulator, probe_bundle, no_probe,
) -> None:
    held = session_for(as_alice)
    try:
        held.reserve(simulator)
        _install(as_alice, held, simulator, probe_bundle)
        assert probe_mod.BUNDLE_ID in _apps(as_owner, simulator), "the install did nothing"
        released = _release(as_alice, held)
    finally:
        held.close()
    assert simulator in released["freed"]
    assert probe_mod.BUNDLE_ID not in _apps(as_owner, simulator), (
        f"the client's app outlived its hold: {released}")


def test_an_app_that_was_already_there_is_kept_and_the_release_says_why(
    as_alice, as_owner, simulator, probe_bundle, no_probe,
) -> None:
    """An install over an app already present added nothing, so the release
    leaves it -- removing it would take away something the client never put
    there -- and says so rather than looking as if cleanup was skipped."""
    as_owner.json_ok("POST", "/api/v1/device/app/install", timeout=300,
                     json={"udid": simulator, "app_path": str(probe_bundle)})
    held = session_for(as_alice)
    try:
        held.reserve(simulator)
        _install(as_alice, held, simulator, probe_bundle)
        released = _release(as_alice, held)
    finally:
        held.close()
    assert probe_mod.BUNDLE_ID in _apps(as_owner, simulator), (
        f"the release removed an app the client did not add: {released}")
    assert probe_mod.BUNDLE_ID in str(released["actions"]), (
        f"the release did not say why it left the app: {released}")


@pytest.mark.slow
def test_a_dropped_client_keeps_its_device_for_the_grace_period_then_loses_it(
    restore_server: Server, alice, bob, simulator,
) -> None:
    """A closed laptop is not a release: the hold outlives the connection
    for `sessions.grace_seconds`, and then the device is free."""
    restore_server.restart(grace_seconds=MIN_GRACE_S)
    as_alice, as_bob = alice.client(restore_server), bob.client(restore_server)
    try:
        held = session_for(as_alice)
        held.reserve(simulator)
        dropped = time.monotonic()
        drop(held)

        bob_session = session_for(as_bob)
        try:
            def bob_reserves():
                return as_bob.post(f"/api/v1/sessions/{bob_session.name}/reservations",
                                   json={"udids": [simulator], "auto_boot": False},
                                   headers={"X-Quern-Session": bob_session.name}, timeout=60)

            assert bob_reserves().status_code == 409, "the drop freed the device at once"
            deadline = dropped + MIN_GRACE_S + 90
            while True:
                resp = bob_reserves()
                if resp.status_code == 200:
                    break
                assert resp.status_code == 409, resp.text
                assert time.monotonic() < deadline, (
                    f"still held {time.monotonic() - dropped:.0f}s after the drop")
                time.sleep(5)
            waited = time.monotonic() - dropped
        finally:
            bob_session.close()
    finally:
        as_alice.close()
        as_bob.close()
    assert waited >= MIN_GRACE_S - 5, f"freed after {waited:.0f}s, inside the grace period"
