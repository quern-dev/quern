"""Reaching quern from another machine (#254, phase 3a).

The certificate and the pairing proofs as units; then the real thing: the
remote listener started in-process on a free port, with TLS and quern's own
certificate, and a client pairing with it, using it, and being refused --
the owner's key, a wrong pin, a foreign Host -- over real sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from server.remote import cert as cert_mod
from server.remote import client, pairing
from server.remote.pairing import PairingCodes, PairingError

KEY = "k" * 32


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cert_mod, "cert_dir", lambda: tmp_path / "remote")
    monkeypatch.setattr(client, "remotes_path", lambda: tmp_path / "remotes.json")
    return tmp_path


# ---------------------------------------------------------------------------
# The certificate
# ---------------------------------------------------------------------------


class TestCertificate:
    def test_made_once_kept_private_and_fingerprinted_by_its_der(self, isolated):
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding

        made = cert_mod.load_or_create(["quern.test", "192.168.1.5"])
        assert stat.S_IMODE(os.stat(made.key_path).st_mode) == 0o600
        der = x509.load_pem_x509_certificate(made.cert_path.read_bytes()).public_bytes(Encoding.DER)
        assert made.fingerprint == cert_mod.fingerprint_of(der)
        assert cert_mod.load_or_create([]).fingerprint == made.fingerprint, "not remade"

    def test_rotating_changes_the_fingerprint(self, isolated):
        first = cert_mod.load_or_create([])
        assert cert_mod.rotate([]).fingerprint != first.fingerprint

    def test_an_unreadable_certificate_is_raised_not_replaced(self, isolated):
        made = cert_mod.load_or_create([])
        made.cert_path.write_text("not a certificate")
        with pytest.raises(ValueError):
            cert_mod.load_or_create([])
        assert made.cert_path.read_text() == "not a certificate", "ending every pin silently"

    def test_a_missing_key_beside_its_certificate_is_raised_not_replaced(self, isolated):
        made = cert_mod.load_or_create([])
        made.key_path.unlink()
        with pytest.raises(FileNotFoundError):
            cert_mod.load_or_create([])
        assert not made.key_path.exists(), "a new pair would end every pin silently"

    def test_fingerprints_read_however_they_are_written(self):
        fp = "ab" * 32
        assert cert_mod.normalise(cert_mod.show(fp)) == fp
        assert cert_mod.normalise(fp.upper()) == fp
        with pytest.raises(ValueError):
            cert_mod.normalise("SHA256:AB:CD")


# ---------------------------------------------------------------------------
# The pairing proofs
# ---------------------------------------------------------------------------


FP = "1" * 64
MITM = "2" * 64


def _exchange(codes, typed, *, server_fp=FP, client_fp=FP):
    """One pairing as the client runs it, against `codes` as the server holds
    them; the label, or the PairingError either side raised."""
    code_id, secret = pairing.parse(typed)
    nonce_c = pairing.new_nonce()
    nonce_s, server_proof = codes.hello(code_id, nonce_c, server_fp)
    if server_proof != pairing.proof("server", secret, code_id, nonce_c, nonce_s, client_fp):
        raise PairingError("client: the server's proof does not match", 0)
    return codes.finish(code_id, nonce_c, nonce_s,
                        pairing.proof("client", secret, code_id, nonce_c, nonce_s, client_fp),
                        server_fp)


class TestPairingProofs:
    def test_a_good_code_pairs_once(self):
        codes = PairingCodes()
        typed, seconds = codes.create("ci")
        assert seconds == 300 and len(typed.replace("-", "")) == 16
        assert _exchange(codes, typed) == "ci"
        with pytest.raises(PairingError) as e:
            _exchange(codes, typed)
        assert e.value.status == 404, "used up"

    def test_a_man_in_the_middle_presenting_its_own_certificate_is_caught(self):
        """The client saw the MITM's certificate; the server signed its own.
        The client refuses the server's proof before sending anything that
        would let the MITM pair in its place."""
        codes = PairingCodes()
        typed, _ = codes.create("ci")
        with pytest.raises(PairingError, match="client"):
            _exchange(codes, typed, server_fp=FP, client_fp=MITM)

    def test_a_relayed_client_proof_over_the_wrong_certificate_is_refused(self):
        codes = PairingCodes()
        typed, _ = codes.create("ci")
        code_id, secret = pairing.parse(typed)
        nonce_c = pairing.new_nonce()
        nonce_s, _ = codes.hello(code_id, nonce_c, FP)
        relayed = pairing.proof("client", secret, code_id, nonce_c, nonce_s, MITM)
        with pytest.raises(PairingError) as e:
            codes.finish(code_id, nonce_c, nonce_s, relayed, FP)
        assert e.value.status == 403

    def test_a_wrong_secret_fails_and_five_attempts_void_the_code(self):
        codes = PairingCodes()
        typed, _ = codes.create("ci")
        wrong = typed[:5] + ("0" if typed[5] != "0" else "1") + typed[6:]
        # A wrong secret fails at the client, after the hello: one attempt each.
        for _ in range(pairing.MAX_ATTEMPTS):
            with pytest.raises(PairingError):
                _exchange(codes, wrong)
        with pytest.raises(PairingError) as e:
            _exchange(codes, typed)
        assert e.value.status == 403, "the attempt past the limit voids it"
        with pytest.raises(PairingError) as e:
            _exchange(codes, typed)
        assert e.value.status == 404, "void, even with the right secret"

    def test_failed_finishes_count_against_the_code(self):
        codes = PairingCodes()
        typed, _ = codes.create("ci")
        code_id, _ = pairing.parse(typed)
        assert pairing.MAX_ATTEMPTS == 5
        for _ in range(2):              # hello + failed finish: two attempts each
            nonce_c = pairing.new_nonce()
            nonce_s, _ = codes.hello(code_id, nonce_c, FP)
            with pytest.raises(PairingError):
                codes.finish(code_id, nonce_c, nonce_s, "0" * 64, FP)
        codes.hello(code_id, pairing.new_nonce(), FP)       # the fifth
        # The sixth voids it -- but only if the finishes counted: three
        # hellos alone would leave room for a good exchange.
        with pytest.raises(PairingError) as e:
            _exchange(codes, typed)
        assert e.value.status == 403

    def test_a_malformed_hello_counts_as_an_attempt(self):
        """Or a live id answers 400 where an unknown one answers 404, for free."""
        codes = PairingCodes()
        typed, _ = codes.create("ci")
        code_id, _ = pairing.parse(typed)
        for _ in range(pairing.MAX_ATTEMPTS):
            with pytest.raises(PairingError):
                codes.hello(code_id, "not-a-nonce", FP)
        with pytest.raises(PairingError) as e:
            _exchange(codes, typed)
        assert e.value.status in (403, 404), "voided by the malformed attempts"

    def test_a_new_code_replaces_one_pending_for_the_same_label(self):
        """A leftover second code would pair the label again after its key
        was revoked."""
        codes = PairingCodes()
        first, _ = codes.create("ci")
        second, _ = codes.create("ci")
        other, _ = codes.create("build")
        with pytest.raises(PairingError) as e:
            _exchange(codes, first)
        assert e.value.status == 404
        assert _exchange(codes, second) == "ci"
        assert _exchange(codes, other) == "build"

    def test_a_finish_for_another_hello_is_refused(self):
        codes = PairingCodes()
        typed, _ = codes.create("ci")
        code_id, secret = pairing.parse(typed)
        first_c = pairing.new_nonce()
        first_s, _ = codes.hello(code_id, first_c, FP)
        codes.hello(code_id, pairing.new_nonce(), FP)       # a second hello replaces it
        with pytest.raises(PairingError) as e:
            codes.finish(code_id, first_c, first_s,
                         pairing.proof("client", secret, code_id, first_c, first_s, FP), FP)
        assert e.value.status == 409

    def test_a_code_expires(self):
        now = [1000.0]
        codes = PairingCodes(clock=lambda: now[0])
        typed, _ = codes.create("ci")
        now[0] += 301
        with pytest.raises(PairingError) as e:
            _exchange(codes, typed)
        assert e.value.status == 404

    def test_codes_are_read_as_people_type_them(self):
        assert pairing.parse("abcd-efgh-jkmn-pqrs") == ("ABCD", "EFGHJKMNPQRS")
        assert pairing.parse("O1L0 0000 0000 0000")[0] == "0110"
        with pytest.raises(PairingError):
            pairing.parse("too-short")


# ---------------------------------------------------------------------------
# The listener, over real TLS
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    """The real app with its remote listener, on a loop in a thread."""

    def __init__(self, tmp_path):
        import server.main as m
        from server.config import ServerConfig
        from server.remote.listener import start_remote

        self.app = m.create_app(ServerConfig(api_key=KEY))
        self.app.state.clients.path = tmp_path / "clients.json"
        self.app.state.clients.seen_path = tmp_path / "clients-seen.json"
        self.port = _free_port()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        settings = {"enabled": True, "host": "127.0.0.1", "port": self.port,
                    "bind_error": None, "hosts": ["quern.test"]}
        self.listener = asyncio.run_coroutine_threadsafe(
            start_remote(self.app, settings), self.loop).result(15)
        assert self.listener.port == self.port, self.listener.error
        self.app.state.remote = self.listener
        self.url = f"https://127.0.0.1:{self.port}"

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.listener.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def running(isolated):
    r = Running(isolated)
    try:
        yield r
    finally:
        r.stop()


class Recorder:
    """A TLS server with a certificate nobody paired with, keeping every
    byte it is sent: a wrong pin must leave it with nothing."""

    def __init__(self, cert):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert.cert_path), str(cert.key_path))
        self.received = b""
        self.handshakes = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self._ctx = ctx
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self._ctx.wrap_socket(raw, server_side=True) as conn:
                    self.handshakes += 1
                    conn.settimeout(2)
                    while data := conn.recv(65536):
                        self.received += data
            except (OSError, ssl.SSLError):
                pass

    def close(self):
        self.sock.close()


@pytest.fixture
def recorder(isolated):
    r = Recorder(cert_mod.load_or_create(["127.0.0.1"]))
    try:
        yield r
    finally:
        r.close()


def _get(remote, path, *, key=None, host=None):
    req = urllib.request.Request(remote.url + path, headers={
        "Authorization": f"Bearer {key or remote.key}", "X-Quern-Session": "s1",
        **({"Host": host} if host else {})})
    try:
        with client.opener(remote).open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class TestAWrongPinSendsNothing:
    """Against a server that keeps what it is sent: the handshake happens,
    and then not one byte -- the key travels in the first request's headers."""

    def _remote(self, recorder):
        return client.Remote(name="x", url=f"https://127.0.0.1:{recorder.port}",
                             fingerprint="0" * 64, key="qc_secret")

    def test_over_https(self, recorder):
        with pytest.raises(client.PinMismatch):
            _get(self._remote(recorder), "/api/v1/sessions")
        time.sleep(0.2)
        assert recorder.handshakes == 1 and recorder.received == b""

    def test_over_the_session_websocket(self, recorder):
        """websockets sends its upgrade -- the key, the resume token -- before
        connect() returns: a check on what it returns came too late (review)."""
        with pytest.raises(client.PinMismatch):
            client.ws_connect(self._remote(recorder), f"wss://127.0.0.1:{recorder.port}/live",
                              additional_headers={"Authorization": "Bearer qc_secret"},
                              open_timeout=5)
        time.sleep(0.2)
        assert recorder.handshakes == 1 and recorder.received == b""


class Relay:
    """A man in the middle: TLS with its own certificate toward the client,
    TLS to the real listener, and every decrypted byte the client sent kept."""

    def __init__(self, upstream_port, cert_path, key_path):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert_path), str(key_path))
        self.up = upstream_port
        self.sent = b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import select

        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            try:
                down = self.ctx.wrap_socket(raw, server_side=True)
                up = client._unverified_context().wrap_socket(
                    socket.create_connection(("127.0.0.1", self.up)))
            except (OSError, ssl.SSLError):
                continue
            # One thread per connection: an SSL socket is not safe to read
            # on one thread while another writes to it.
            threading.Thread(target=self._splice, args=(down, up, select),
                             daemon=True).start()

    def _splice(self, down, up, select):
        # Reads are non-blocking: a readable TLS socket may hold only a
        # record with no data in it (TLS 1.3's session tickets), and a
        # blocking recv then waited for data while the other side's request
        # sat unforwarded -- the client timed out about one run in five.
        for s in (down, up):
            s.setblocking(False)
        try:
            while True:
                ready, _, _ = select.select([down, up], [], [], 5)
                if not ready:
                    return
                for src in ready:
                    data = b""
                    try:
                        data = src.recv(65536)
                        if not data:
                            return
                        while src.pending():    # decrypted already: select can't see it
                            data += src.recv(src.pending())
                    except ssl.SSLWantReadError:
                        if not data:
                            continue
                    dst = up if src is down else down
                    if src is down:
                        self.sent += data
                    dst.setblocking(True)
                    dst.sendall(data)
                    dst.setblocking(False)
        except OSError:
            pass
        finally:
            for s in (down, up):
                with contextlib.suppress(OSError):
                    s.close()

    def close(self):
        self.sock.close()


class TestTheListener:
    def test_pairing_through_a_man_in_the_middle_is_refused_before_finish(self, running,
                                                                         tmp_path):
        """The real client against the real listener, through a relay that
        terminates TLS with its own certificate: the server's proof is over
        its certificate, the client saw another, and it stops after hello --
        no finish, no secret, nothing the relay could complete pairing with."""
        cert_mod._generate(tmp_path / "mitm.pem", tmp_path / "mitm.key", ["127.0.0.1"])
        relay = Relay(running.port, tmp_path / "mitm.pem", tmp_path / "mitm.key")
        try:
            typed, _ = running.listener.codes.create("victim")
            with pytest.raises(client.RemoteError, match="could not prove"):
                client.pair(f"https://127.0.0.1:{relay.port}", typed, "office")
            time.sleep(0.2)
            assert b"/pair/hello" in relay.sent, "the relay was really in the path"
            assert b"/pair/finish" not in relay.sent
            assert pairing.parse(typed)[1].encode() not in relay.sent
            assert not running.app.state.clients.is_active("victim")
        finally:
            relay.close()

    def test_pair_then_use_it_with_the_key_pairing_issued(self, running):
        typed, _ = running.listener.codes.create("laptop")
        remote = client.pair(running.url, typed, "office")
        assert remote.fingerprint == running.listener.cert.fingerprint
        assert running.app.state.clients.is_active("laptop")
        status, body = _get(remote, "/api/v1/device/list")
        assert status != 401 and status != 403, body
        status, health = _get(remote, "/health")
        assert status == 200 and len(health["host_id"]) == 32

    def test_the_owners_key_is_refused(self, running):
        remote = client.Remote(name="x", url=running.url,
                               fingerprint=running.listener.cert.fingerprint, key=KEY)
        status, body = _get(remote, "/api/v1/device/list", key=KEY)
        assert status == 403 and "owner" in body["detail"]

    def test_a_wrong_pin_sends_nothing_and_says_both_fingerprints(self, running):
        remote = client.Remote(name="x", url=running.url, fingerprint="0" * 64, key=KEY)
        with pytest.raises(client.PinMismatch) as e:
            _get(remote, "/health")
        assert cert_mod.show(running.listener.cert.fingerprint) in str(e.value)

    def test_a_host_it_was_not_given_is_421(self, running):
        remote = client.Remote(name="x", url=running.url,
                               fingerprint=running.listener.cert.fingerprint, key=KEY)
        # No valid key: /health is public, and the owner's key is refused here.
        assert _get(remote, "/health", host="evil.example", key="qc_none")[0] == 421
        assert _get(remote, "/health", host="quern.test", key="qc_none")[0] == 200

    def test_pairing_answers_only_on_the_remote_listener(self, running):
        from fastapi.testclient import TestClient

        # A real code, so a 404 is the listener check and not an unknown id.
        typed, _ = running.listener.codes.create("lan")
        code_id, _ = pairing.parse(typed)
        local = TestClient(running.app)
        r = local.post("/api/v1/remote/pair/hello", json={"code_id": code_id, "nonce": "0" * 64})
        assert r.status_code == 404
        assert client.pair(running.url, typed, "lan").fingerprint, "the code was still good"

    def test_the_owners_key_is_refused_on_a_websocket_too(self, running):
        from websockets.exceptions import InvalidStatus

        remote = client.Remote(name="x", url=running.url,
                               fingerprint=running.listener.cert.fingerprint, key=KEY)
        with pytest.raises(InvalidStatus) as e:
            ws = client.ws_connect(remote, f"wss://127.0.0.1:{running.port}/api/v1/sessions/s1/live",
                                   additional_headers={"Authorization": f"Bearer {KEY}"},
                                   open_timeout=5)
            ws.close()
        assert e.value.response.status_code in (401, 403)

    def test_a_connection_is_counted_and_timed_before_its_tls_handshake(self, running,
                                                                         monkeypatch):
        """asyncio hands a protocol its connection only after the handshake,
        so sockets that never sent a ClientHello escaped the cap and the
        timeout -- a minute each, as many as a LAN host liked (review)."""
        from server.remote import listener
        from server.viewer import protocol

        monkeypatch.setattr(protocol, "MAX_CONNECTIONS", 4)
        monkeypatch.setattr(listener, "HANDSHAKE_TIMEOUT", 0.5)
        idle = [socket.create_connection(("127.0.0.1", running.port)) for _ in range(8)]
        try:
            time.sleep(0.2)
            closed = 0
            for s in idle:
                s.settimeout(0.05)
                try:
                    closed += s.recv(1) == b""
                except TimeoutError:
                    pass
                except ConnectionResetError:
                    closed += 1
            assert closed == 4, "past the cap, closed on arrival"
            time.sleep(0.8)
            for s in idle:
                s.settimeout(0.5)
                try:
                    assert s.recv(1) == b"", "no handshake: closed at the timeout"
                except ConnectionResetError:
                    pass
        finally:
            for s in idle:
                s.close()
        # And the listener still answers once they are gone.
        remote = client.Remote(name="x", url=running.url,
                               fingerprint=running.listener.cert.fingerprint, key="qc_none")
        assert _get(remote, "/health")[0] == 200

    def test_a_held_session_socket_outlives_the_requestless_timeout(self, running, monkeypatch):
        """The viewer's guard closes connections that send no request; an
        upgraded socket never completes one, and was closed (found writing)."""
        from server.viewer import protocol

        monkeypatch.setattr(protocol, "REQUEST_TIMEOUT", 0.3)
        typed, _ = running.listener.codes.create("ws")
        remote = client.pair(running.url, typed, "ws")
        req = urllib.request.Request(remote.url + "/api/v1/sessions", method="POST",
                                     data=b"{}", headers={"Authorization": f"Bearer {remote.key}",
                                                          "X-Quern-Session": "s1",
                                                          "Content-Type": "application/json"})
        from server.session_registry import SessionRegistry

        running.app.state.sessions = SessionRegistry(grace_seconds=180)
        with client.opener(remote).open(req, timeout=10) as resp:
            opened = json.loads(resp.read())
        ws = client.ws_connect(remote, f"wss://127.0.0.1:{running.port}{opened['live_path']}",
                               additional_headers={"Authorization": f"Bearer {remote.key}",
                                                   "X-Quern-Session": "s1",
                                                   "X-Quern-Resume-Token": opened["resume_token"]},
                               open_timeout=10)
        try:
            assert json.loads(ws.recv(timeout=5))["event"] == "attached"
            time.sleep(0.8)
            ws.ping().wait(5)                   # still open
        finally:
            ws.close()


# ---------------------------------------------------------------------------
# Settings, host id, the remotes file and registering the MCP
# ---------------------------------------------------------------------------


def test_remote_settings_are_off_unless_literally_enabled(monkeypatch):
    from server import config

    monkeypatch.setattr(config, "read_user_config",
                        lambda: {"remote": {"enabled": "yes", "bind": "nonsense"}})
    s = config.remote_settings()
    assert s["enabled"] is False and s["port"] == 9130 and s["bind_error"]
    monkeypatch.setattr(config, "read_user_config",
                        lambda: {"remote": {"enabled": True, "bind": "[::]:9444"}})
    s = config.remote_settings()
    assert (s["enabled"], s["host"], s["port"]) == (True, "::", 9444)


def test_host_id_is_made_once_and_kept(tmp_path, monkeypatch):
    from server import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "_HOST_ID", None)
    first = config.host_id()
    monkeypatch.setattr(config, "_HOST_ID", None)
    assert config.host_id() == first == (tmp_path / "host-id").read_text().strip()


def test_remotes_are_kept_private_and_read_back(isolated):
    client.save(client.Remote(name="ci", url="https://ci.lan:9130", fingerprint="ab" * 32,
                              key="qc_x"))
    assert stat.S_IMODE(os.stat(isolated / "remotes.json").st_mode) == 0o600
    assert client.load("ci").key == "qc_x"
    assert client.forget("ci") and not client.forget("ci")
    with pytest.raises(client.RemoteError, match="pair"):
        client.load("ci")


def test_a_remote_that_is_not_https_is_refused(isolated, capsys):
    """Through urllib's plain handler it would go unpinned, the key in clear."""
    client.save(client.Remote(name="ci", url="http://ci.lan:9130", fingerprint="ab" * 32,
                              key="qc_x"))
    with pytest.raises(client.RemoteError, match="https"):
        client.load("ci")
    from server.remote_cli import main as remote_main

    assert remote_main("connect", ["http://ci.lan:9130", "--key", "qc_x",
                        "--fingerprint", "ab" * 32, "--name", "plain"]) == 1
    # Refused for the scheme, before any connection -- not because ci.lan
    # happens not to answer.
    assert "not an https:// address" in capsys.readouterr().err
    assert "plain" not in client._read()


def test_mcp_install_points_each_client_at_the_remote_and_back(tmp_path):
    import server.__main__ as cli

    claude = tmp_path / "claude.json"
    claude.write_text(json.dumps({"mcpServers": {"quern-debug": {"env": {"KEEP": "1"}}}}))
    cli._install_json_mcpservers(claude, tmp_path / "launcher.cjs", "node", "office")
    env = json.loads(claude.read_text())["mcpServers"]["quern-debug"]["env"]
    assert env == {"KEEP": "1", "QUERN_REMOTE": "office"}
    cli._install_json_mcpservers(claude, tmp_path / "launcher.cjs", "node", None)
    assert json.loads(claude.read_text())["mcpServers"]["quern-debug"]["env"] == {"KEEP": "1"}


class TestCodexRegistration:
    """~/.codex/config.toml, which codex cannot read at all if it is not valid
    TOML -- so every case is read back with tomllib (review)."""

    def _install(self, home, monkeypatch, remote):
        import server.__main__ as cli

        monkeypatch.setattr(Path, "home", lambda: home)
        assert cli._install_codex(home / "launcher.cjs", "node", remote)[0]
        import tomllib

        return tomllib.loads((home / ".codex" / "config.toml").read_text())["mcp_servers"]["quern"]

    def _write(self, home, text):
        (home / ".codex").mkdir(exist_ok=True)
        (home / ".codex" / "config.toml").write_text(text)

    def test_an_inline_env_keeps_its_other_variables_both_ways(self, tmp_path, monkeypatch):
        self._write(tmp_path, '[mcp_servers.quern]\ncommand = "node"\nenv = { KEEP = "1" }\n')
        assert self._install(tmp_path, monkeypatch, "office")["env"] == {
            "KEEP": "1", "QUERN_REMOTE": "office"}
        assert self._install(tmp_path, monkeypatch, None)["env"] == {"KEEP": "1"}

    def test_an_env_sub_table_is_edited_in_place_not_doubled(self, tmp_path, monkeypatch):
        self._write(tmp_path, '[mcp_servers.quern]\ncommand = "node"\n\n'
                              '[mcp_servers.quern.env]\nKEEP = "1"\n')
        assert self._install(tmp_path, monkeypatch, "office")["env"] == {
            "KEEP": "1", "QUERN_REMOTE": "office"}
        assert self._install(tmp_path, monkeypatch, None)["env"] == {"KEEP": "1"}

    def test_another_servers_quern_remote_does_not_touch_querns_env(self, tmp_path, monkeypatch):
        self._write(tmp_path, '[mcp_servers.other]\nenv = { QUERN_REMOTE = "x" }\n\n'
                              '[mcp_servers.quern]\ncommand = "node"\nenv = { KEEP = "1" }\n')
        assert self._install(tmp_path, monkeypatch, None)["env"] == {"KEEP": "1"}

    def test_a_fresh_file(self, tmp_path, monkeypatch):
        assert self._install(tmp_path, monkeypatch, "office")["env"] == {"QUERN_REMOTE": "office"}


def test_a_paired_url_keeps_the_port_it_paired_on():
    """No port typed meant 9130 for pairing, then 443 for every request after."""
    assert client.with_port("https://ci.lan") == "https://ci.lan:9130"
    assert client.with_port("https://ci.lan:9443/") == "https://ci.lan:9443"
    assert client.with_port("https://[fe80::1]") == "https://[fe80::1]:9130"


def test_an_unverified_tls_context_still_refuses_old_protocols():
    ctx = client._unverified_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
