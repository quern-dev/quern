"""Fixtures for the remote tier: a real remote listener and real pairings.

The rest of the suite talks to the server this machine runs, over its local
listener. Every remote feature -- pairing, the pinned certificate, client keys,
one client's reservation refusing another -- was tested only against fakes. So
this tier stands up its own server, from this checkout, with the remote listener
on loopback, and pairs clients to it exactly as a person would: `quern pair` on
the server's side, `quern connect` on the client's. Each side has its own
`QUERN_STATE_DIR`; nothing reads or writes `~/.quern`, and the live server is
never asked anything.

Opt in with `QUERN_CONFORMANCE_REMOTE=1`. Tests that need a simulator also need
`QUERN_CONFORMANCE_DEVICES` naming one: the scratch server sees every simulator
on the Mac, and a reservation on it binds nobody on the live server, so the run
uses only a device it was handed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.conformance import client as client_mod

GATE_REMOTE = "QUERN_CONFORMANCE_REMOTE"
GATE_SLOW = "QUERN_CONFORMANCE_SLOW"

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

#: A pairing code as `quern pair` prints it: four groups of four.
CODE = re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}\b")

#: The shortest grace the server accepts (`sessions.grace_seconds`, 120-300).
MIN_GRACE_S = 120


def _gate_open(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"slow: takes minutes; runs only when {GATE_SLOW} is set")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Gate this directory. Called with every item in the session, so only
    the ones under it are touched."""
    remote_open, slow_open = _gate_open(GATE_REMOTE), _gate_open(GATE_SLOW)
    for item in items:
        if not str(item.fspath).startswith(str(HERE)):
            continue
        if not remote_open:
            item.add_marker(pytest.mark.skip(
                reason=f"remote tier is off; it starts its own server and pairs clients "
                       f"to it (set {GATE_REMOTE}=1 to run it)"))
        elif item.get_closest_marker("slow") and not slow_open:
            item.add_marker(pytest.mark.skip(
                reason=f"slow test; set {GATE_SLOW}=1 to run it"))


@pytest.fixture(scope="session", autouse=True)
def _report_environment() -> None:
    """Overrides the suite's: that one discovers devices on the live server,
    which this tier neither needs nor asks."""


# -- the scratch server ------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(state_dir: Path) -> dict[str, str]:
    """This checkout's quern, with its own state. The root conftest points
    QUERN_STATE_DIR at a temp directory for the unit suite; replaced, not
    inherited, so each process here has exactly the state it was given."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUERN_")}
    env["QUERN_STATE_DIR"] = str(state_dir)
    env["PYTHONPATH"] = str(REPO)
    return env


def quern_cli(state_dir: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """`quern <args>` from this checkout, as the person at that machine."""
    return subprocess.run(  # noqa: S603 - this checkout's own CLI
        [sys.executable, "-m", "server", *args], cwd=REPO, env=_env(state_dir),
        capture_output=True, text=True, timeout=timeout, check=False)


@dataclass
class Server:
    """A quern with its remote listener on loopback."""

    state_dir: Path
    port: int
    proxy_port: int
    remote_port: int
    process: subprocess.Popen | None = None
    log: Path | None = None
    #: Run the proxy too: mocks and capture need it. Off by default, since
    #: only the MCP tests use it and each one is another mitmdump.
    proxy: bool = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def remote_url(self) -> str:
        return f"https://127.0.0.1:{self.remote_port}"

    @property
    def owner_key(self) -> str:
        return (self.state_dir / "api-key").read_text().strip()

    @property
    def certificate(self) -> Path:
        return self.state_dir / "remote" / "cert.pem"

    def fingerprint(self) -> str:
        """SHA-256 of the listener certificate's DER, as `quern connect` pins it."""
        der = ssl.PEM_cert_to_DER_cert(self.certificate.read_text())
        return hashlib.sha256(der).hexdigest()

    def configure(self, **sessions) -> None:
        settings = {
            "update_check": False,
            "remote": {"enabled": True, "bind": f"127.0.0.1:{self.remote_port}"},
        }
        if sessions:
            settings["sessions"] = sessions
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "config.json").write_text(json.dumps(settings))

    def start(self) -> None:
        self.log = self.state_dir / "conformance-server.log"
        handle = self.log.open("ab")
        self.process = subprocess.Popen(  # noqa: S603 - this checkout's own server
            [sys.executable, "-m", "server", "start", "--foreground",
             "--port", str(self.port), "--proxy-port", str(self.proxy_port),
             *([] if self.proxy else ["--no-proxy"])],
            cwd=REPO, env=_env(self.state_dir), stdout=handle, stderr=subprocess.STDOUT)
        handle.close()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"the scratch server exited ({self.process.returncode}):\n"
                                   f"{self.tail()}")
            with contextlib.suppress(httpx.HTTPError, OSError, ValueError):
                status = httpx.get(f"{self.url}/api/v1/remote/status", timeout=2,
                                   headers={"Authorization": f"Bearer {self.owner_key}"})
                if status.status_code == 200 and status.json().get("listening"):
                    return
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"the scratch server's remote listener never came up:\n{self.tail()}")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)

    def restart(self, **sessions) -> None:
        self.stop()
        self.configure(**sessions)
        self.start()

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])
        except (OSError, AttributeError):
            return "(no server log)"

    def owner(self) -> client_mod.QuernClient:
        """The owner, on the local listener: what the person at the Mac sees."""
        return client_mod.QuernClient(client_mod.ServerTarget(
            url=self.url, api_key=self.owner_key, source="scratch server, owner key"))


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    srv = Server(state_dir=tmp_path_factory.mktemp("remote-server"), port=_free_port(),
                 proxy_port=_free_port(), remote_port=_free_port())
    srv.configure()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def restore_server(server: Server) -> Iterator[Server]:
    """For a test that restarts the server with other settings: put the
    default ones back after it, so the tests after it see what they expect."""
    try:
        yield server
    finally:
        server.restart()


# -- pairing -----------------------------------------------------------------


@dataclass
class Paired:
    """A client machine paired with the server, as `quern connect` left it."""

    label: str
    state_dir: Path
    name: str
    url: str
    key: str
    fingerprint: str

    def client(self, server: Server) -> client_mod.QuernClient:
        """A client of the remote listener, pinned to the server's certificate:
        it is the only trust anchor, and host names are not checked, which is
        what pinning a certificate by fingerprint amounts to."""
        ctx = ssl.create_default_context(cafile=str(server.certificate))
        ctx.check_hostname = False
        # The certificate is its own issuer, not a CA: trust it as the anchor.
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return client_mod.QuernClient(
            client_mod.ServerTarget(url=self.url, api_key=self.key,
                                    source=f"remote listener, client {self.label!r}"),
            ssl_context=ctx)


def pairing_code(server: Server, label: str) -> str:
    done = quern_cli(server.state_dir, "pair", label)
    assert done.returncode == 0, f"quern pair {label}: {done.stdout}{done.stderr}"
    found = CODE.search(done.stdout)
    assert found, f"no pairing code in:\n{done.stdout}"
    return found.group(0)


def connect(server: Server, state_dir: Path, code: str, name: str = "scratch"
            ) -> subprocess.CompletedProcess:
    state_dir.mkdir(parents=True, exist_ok=True)
    return quern_cli(state_dir, "connect", server.remote_url, "--code", code, "--name", name)


def read_remote(state_dir: Path, name: str = "scratch") -> dict | None:
    try:
        return json.loads((state_dir / "remotes.json").read_text())["remotes"].get(name)
    except (OSError, ValueError, KeyError):
        return None


def pair(server: Server, tmp_path_factory: pytest.TempPathFactory, label: str) -> Paired:
    state_dir = tmp_path_factory.mktemp(f"client-{label}")
    done = connect(server, state_dir, pairing_code(server, label))
    assert done.returncode == 0, f"quern connect: {done.stdout}{done.stderr}"
    entry = read_remote(state_dir)
    assert entry, f"quern connect recorded nothing in {state_dir / 'remotes.json'}"
    return Paired(label=label, state_dir=state_dir, name="scratch", url=entry["url"],
                  key=entry["key"], fingerprint=entry["fingerprint"])


@pytest.fixture(scope="session")
def alice(server: Server, tmp_path_factory: pytest.TempPathFactory) -> Paired:
    return pair(server, tmp_path_factory, "alice")


@pytest.fixture(scope="session")
def bob(server: Server, tmp_path_factory: pytest.TempPathFactory) -> Paired:
    return pair(server, tmp_path_factory, "bob")


@pytest.fixture
def as_alice(server: Server, alice: Paired) -> Iterator[client_mod.QuernClient]:
    c = alice.client(server)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def as_bob(server: Server, bob: Paired) -> Iterator[client_mod.QuernClient]:
    c = bob.client(server)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def as_owner(server: Server) -> Iterator[client_mod.QuernClient]:
    c = server.owner()
    try:
        yield c
    finally:
        c.close()


# -- a device ------------------------------------------------------------------


@pytest.fixture(scope="session")
def simulator(server: Server) -> str:
    """The one iOS simulator this run was handed, booted.

    Pinned, never picked: the scratch server sees every simulator on the Mac,
    and its reservations bind nobody using the live server.
    """
    pinned = [p.strip() for p in os.environ.get("QUERN_CONFORMANCE_DEVICES", "").split(",")
              if p.strip()]
    if not pinned:
        pytest.skip("needs a simulator: set QUERN_CONFORMANCE_DEVICES to one this run "
                    "may use (the scratch server sees every simulator on the Mac)")
    owner = server.owner()
    try:
        devices = owner.json_ok("GET", "/api/v1/device/list", timeout=60)["devices"]
    finally:
        owner.close()
    simulators = [d for d in devices if d.get("device_type") == "simulator"]
    for pin in pinned:
        matches = [d for d in simulators if d["udid"].startswith(pin)]
        if len(matches) > 1:
            # A prefix two simulators share would quietly use whichever is
            # listed first: say so, and which ones, rather than guess (CR).
            pytest.fail(f"QUERN_CONFORMANCE_DEVICES entry {pin!r} matches "
                        f"{len(matches)} simulators: "
                        f"{', '.join(d['udid'] for d in matches)}; give more of the UDID")
        if matches:
            dev = matches[0]
            if dev.get("state") != "booted":
                pytest.skip(f"{dev['name']} ({dev['udid']}) is not booted")
            return dev["udid"]
    pytest.skip(f"no iOS simulator matches QUERN_CONFORMANCE_DEVICES={','.join(pinned)}")


def session_for(client: client_mod.QuernClient) -> client_mod.HeldSession:
    """A live session for `client`, closed by the caller."""
    return client_mod.HeldSession(client).open()


def drop(held: client_mod.HeldSession) -> None:
    """End a session's socket without a close frame: a lid shut, a network
    lost. The server keeps its devices for the grace period."""
    held._closing = True
    with contextlib.suppress(OSError):
        held._ws.socket.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        held._ws.socket.close()
