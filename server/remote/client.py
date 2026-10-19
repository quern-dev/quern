"""The client side of a remote quern: what it is called, and pinned TLS to it.

`quern connect` pairs with a server and records it in `~/.quern/remotes.json`
(0600) under a name: its URL, the fingerprint of its certificate, and the
client key pairing issued. `QUERN_REMOTE=<name>` then points `quern session
run` and `quern record` (and the MCP, in TypeScript) at it.

Every connection is pinned: the certificate is not checked against any CA or
name, only against the recorded fingerprint, and a mismatch refuses -- naming
both fingerprints, since the usual cause is a rotated certificate and the
other is someone in the middle.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import http.client
import json
import os
import ssl
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from server.remote import cert as cert_mod
from server.remote import pairing


class RemoteError(Exception):
    """A remote that cannot be used; the message says why and what to do."""


class PinMismatch(RemoteError):
    pass


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    fingerprint: str
    key: str
    label: str = ""
    paired_at: str = ""


def remotes_path() -> Path:
    from server.config import CONFIG_DIR

    return CONFIG_DIR / "remotes.json"


def _read() -> dict[str, dict]:
    try:
        data = json.loads(remotes_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RemoteError(f"{remotes_path()} is not readable ({e}); fix or move it aside") from e
    remotes = data.get("remotes") if isinstance(data, dict) else None
    if not isinstance(remotes, dict):
        raise RemoteError(f"{remotes_path()} has no 'remotes' object; fix or move it aside")
    return remotes


def load(name: str) -> Remote:
    entry = _read().get(name)
    if not isinstance(entry, dict):
        raise RemoteError(f"no remote called {name!r}: pair with one first "
                          f"(quern connect <url> --code <code> --name {name})")
    if not str(entry.get("url", "")).lower().startswith("https://"):
        raise RemoteError(f"the remote {name!r} in {remotes_path()} is not an https:// "
                          "address: its key would travel in clear; pair with it again")
    try:
        return Remote(name=name, url=str(entry["url"]).rstrip("/"),
                      fingerprint=cert_mod.normalise(str(entry["fingerprint"])),
                      key=str(entry["key"]), label=str(entry.get("label", "")),
                      paired_at=str(entry.get("paired_at", "")))
    except (KeyError, ValueError) as e:
        raise RemoteError(f"the remote {name!r} in {remotes_path()} is incomplete ({e}); "
                          "pair with it again") from e


def list_remotes() -> list[Remote]:
    return [load(name) for name in sorted(_read())]


def save(remote: Remote) -> None:
    with _locked() as remotes:
        remotes[remote.name] = {k: v for k, v in asdict(remote).items() if k != "name"}


def forget(name: str) -> bool:
    with _locked() as remotes:
        return remotes.pop(name, None) is not None


@contextlib.contextmanager
def _locked():
    path = remotes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            remotes = _read()
            yield remotes
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(json.dumps({"remotes": remotes}, indent=2) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def from_env() -> Remote | None:
    """The remote `QUERN_REMOTE` names, or None when it names none."""
    name = os.environ.get("QUERN_REMOTE", "").strip()
    return load(name) if name else None


# -- pinned TLS -----------------------------------------------------------------


def _unverified_context() -> ssl.SSLContext:
    """TLS that checks no CA and no name: the pin is the check, made on the
    certificate itself once the handshake has happened."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def presented(sock) -> str:
    """The fingerprint of the certificate a TLS socket was shown."""
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise RemoteError("the server presented no certificate")
    return cert_mod.fingerprint_of(der)


def check_pin(sock, expected: str) -> None:
    seen = presented(sock)
    if seen != expected:
        raise PinMismatch(
            f"the server's certificate is not the one paired with: expected "
            f"{cert_mod.show(expected)}, presented {cert_mod.show(seen)}. Its certificate "
            "was rotated (pair again: quern connect), or something is between you and it")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that refuses any certificate but the pinned one."""

    def __init__(self, *args, fingerprint: str, **kwargs) -> None:
        kwargs.setdefault("context", _unverified_context())
        super().__init__(*args, **kwargs)
        self._fingerprint = fingerprint

    def connect(self) -> None:
        super().connect()
        try:
            check_pin(self.sock, self._fingerprint)
        except RemoteError:
            self.close()
            raise


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, fingerprint: str) -> None:
        super().__init__(context=_unverified_context())
        self._fingerprint = fingerprint

    def https_open(self, req):
        return self.do_open(functools.partial(PinnedHTTPSConnection,
                                              fingerprint=self._fingerprint), req)


def opener(remote: Remote) -> urllib.request.OpenerDirector:
    """A urllib opener whose every HTTPS connection is pinned to `remote`."""
    return urllib.request.build_opener(_PinnedHandler(remote.fingerprint))


class _PinningContext(ssl.SSLContext):
    """A context whose sockets are pin-checked as the handshake completes.

    websockets' `connect` wraps the socket and sends the upgrade -- the key
    and resume token in its headers -- before it returns, so a check on the
    connection it hands back comes after they have gone (review). Here the
    check is inside `wrap_socket`, before anything is written."""

    fingerprint = ""

    def wrap_socket(self, *args, **kwargs):
        sock = super().wrap_socket(*args, **kwargs)
        try:
            check_pin(sock, self.fingerprint)
        except RemoteError:
            sock.close()
            raise
        return sock


def ws_connect(remote: Remote, url: str, **kwargs):
    """A websockets sync connection to `url`, pinned to `remote`."""
    from websockets.sync.client import connect

    ctx = _PinningContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.fingerprint = remote.fingerprint
    return connect(url, ssl=ctx, **kwargs)


# -- pairing ------------------------------------------------------------------------


def with_port(url: str) -> str:
    """`url` as https://host:port, the listener's 9130 when it names none."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return f"https://{host}:{parts.port or 9130}"


def pair(url: str, code: str, name: str, *, timeout: float = 15.0) -> Remote:
    """Pair with the server at `url` using `code`, and return what to store.

    Both halves of the exchange go over one TLS connection, and the proofs are
    over that connection's certificate: a second connection could be to
    someone else, who would relay the client's proof to the real server."""
    code_id, secret = pairing.parse(code)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise RemoteError(f"{url!r} is not an https:// address: the remote listener "
                          "speaks only TLS")
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 9130,
                                       context=_unverified_context(), timeout=timeout)
    # Never a second connection: the proofs are over this one's certificate.
    conn.auto_open = 0
    try:
        conn.connect()
        seen = presented(conn.sock)
        nonce_c = pairing.new_nonce()
        status, hello = _post(conn, "/api/v1/remote/pair/hello",
                              {"code_id": code_id, "nonce": nonce_c})
        if status != 200:
            raise RemoteError(f"the server refused the code: {hello.get('detail', status)}")
        nonce_s, server_proof = str(hello.get("nonce", "")), str(hello.get("proof", ""))
        expected = pairing.proof("server", secret, code_id, nonce_c, nonce_s, seen)
        import hmac

        if not hmac.compare_digest(server_proof, expected):
            raise RemoteError(
                "the server could not prove it holds this code for the certificate it "
                f"presented ({cert_mod.show(seen)}): the code is mistyped, or the connection "
                "is not to the server that made it. Nothing was sent that would let it "
                "pair in your place")
        client_proof = pairing.proof("client", secret, code_id, nonce_c, nonce_s, seen)
        status, done = _post(conn, "/api/v1/remote/pair/finish",
                             {"code_id": code_id, "client_nonce": nonce_c,
                              "server_nonce": nonce_s, "proof": client_proof})
        if status != 200:
            raise RemoteError(f"the server refused to pair: {done.get('detail', status)}")
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        raise RemoteError(f"could not reach {url}: {e}") from e
    finally:
        conn.close()
    # The port it paired on, written out: a URL without one means 443 to
    # every later request, not the 9130 used here (review).
    return Remote(name=name, url=with_port(url), fingerprint=seen,
                  key=str(done["key"]), label=str(done.get("label", "")),
                  paired_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _post(conn: http.client.HTTPSConnection, path: str, body: dict) -> tuple[int, dict]:
    conn.request("POST", path, body=json.dumps(body).encode(),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {"detail": raw[:200].decode("latin-1")}
    return resp.status, data if isinstance(data, dict) else {"detail": str(data)}


def health(remote: Remote, *, timeout: float = 10.0) -> dict:
    """The remote's /health, over the pinned connection."""
    with opener(remote).open(remote.url + "/health", timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")
