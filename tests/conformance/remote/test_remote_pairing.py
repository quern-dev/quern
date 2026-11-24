"""Pairing over a real remote listener, and what a paired key may reach.

Each test pairs the way a person does -- `quern pair` on the server's side,
`quern connect` on the client's -- and then asks the listener, over TLS, what
the pairing bought. The unit suite proves the proofs and the pin with fakes;
this proves the pieces agree when they are the real ones.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conformance import client as client_mod

from .conftest import (
    Paired,
    Server,
    connect,
    pair,
    pairing_code,
    quern_cli,
    read_remote,
)


def _hex(fingerprint: str) -> str:
    return fingerprint.replace(":", "").replace("SHA256", "").lower()


def test_a_paired_client_reaches_the_listener_with_its_key(as_alice) -> None:
    resp = as_alice.get("/api/v1/device/list", timeout=60)
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json().get("devices"), list)


def test_connect_pins_the_certificate_the_listener_actually_serves(
    server: Server, alice: Paired,
) -> None:
    """The fingerprint `quern connect` recorded is the one the listener's
    certificate has: the pin is the real certificate, not whatever pairing
    said it was."""
    assert _hex(alice.fingerprint) == server.fingerprint()


def test_a_pairing_code_works_once(server: Server, tmp_path) -> None:
    code = pairing_code(server, "once")
    first = connect(server, tmp_path / "first", code)
    assert first.returncode == 0, first.stdout + first.stderr

    again = connect(server, tmp_path / "second", code)
    assert again.returncode != 0, "a used code paired a second machine"
    # Refused for the code, not only because its label now has a key: with
    # codes left reusable, the label check refused this alone, and a test
    # reading only the exit code passed against it (mutation).
    assert "no such pairing code" in again.stderr, again.stderr
    assert read_remote(tmp_path / "second") is None, "a refused pairing was recorded"


def test_a_wrong_code_pairs_nothing(server: Server, tmp_path) -> None:
    pairing_code(server, "wrong")       # one pending, so a refusal is not "no code"
    done = connect(server, tmp_path / "client", "ABCD-EFGH-JKMN-PQRS")
    assert done.returncode != 0
    assert read_remote(tmp_path / "client") is None


def test_the_owners_key_is_refused_on_the_remote_listener(
    server: Server, alice: Paired,
) -> None:
    """The listener is for client keys. The owner's key works only on
    loopback, so it is never worth stealing off the network."""
    owner_over_tls = Paired(label="owner", state_dir=alice.state_dir, name=alice.name,
                            url=alice.url, key=server.owner_key,
                            fingerprint=alice.fingerprint).client(server)
    try:
        resp = owner_over_tls.get("/api/v1/device/list", timeout=30)
    finally:
        owner_over_tls.close()
    assert resp.status_code in (401, 403), resp.text


def test_no_key_is_refused_on_a_protected_route(server: Server, alice: Paired) -> None:
    keyless = Paired(label="none", state_dir=alice.state_dir, name=alice.name,
                     url=alice.url, key="", fingerprint=alice.fingerprint).client(server)
    try:
        resp = keyless.get("/api/v1/device/list", timeout=30)
    finally:
        keyless.close()
    assert resp.status_code == 401, resp.text


def test_a_host_the_listener_was_not_given_is_421(as_alice) -> None:
    """A DNS-rebinding page names a host of its own; the listener answers
    only the names it was given."""
    resp = as_alice.get("/api/v1/device/list", headers={"Host": "rebind.example"},
                        timeout=30)
    assert resp.status_code == 421, resp.text


def test_pairing_answers_on_the_remote_listener(as_alice) -> None:
    """The other half of the local listener's 404: here a malformed hello is
    read, and refused for what it says."""
    for path in ("/api/v1/remote/pair/hello", "/api/v1/remote/pair/finish"):
        resp = as_alice.post(path, json={}, authenticated=False, timeout=30)
        assert resp.status_code == 422, (path, resp.status_code, resp.text)


def test_a_revoked_client_is_refused(server: Server, tmp_path_factory) -> None:
    carol = pair(server, tmp_path_factory, "carol")
    as_carol = carol.client(server)
    try:
        assert as_carol.get("/api/v1/device/list", timeout=60).status_code == 200

        done = quern_cli(server.state_dir, "clients", "revoke", "carol")
        assert done.returncode == 0, done.stdout + done.stderr

        resp = as_carol.get("/api/v1/device/list", timeout=30)
    finally:
        as_carol.close()
    assert resp.status_code in (401, 403), resp.text


def test_a_client_pinned_to_another_certificate_sends_nothing(
    server: Server, alice: Paired,
) -> None:
    """The other side of the pin: a client expecting some other certificate
    refuses the handshake, so its key never reaches the server."""
    import ssl

    wrong = ssl.create_default_context()      # the system's CAs: not this certificate
    wrong.check_hostname = False
    c = client_mod.QuernClient(
        client_mod.ServerTarget(url=alice.url, api_key=alice.key, source="wrong pin"),
        ssl_context=wrong)
    try:
        with pytest.raises(httpx.ConnectError):
            c.get("/api/v1/device/list", timeout=30)
    finally:
        c.close()
