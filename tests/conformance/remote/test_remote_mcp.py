"""The MCP's `use_remote`, driven for real: one agent, two servers.

The agent's side of the remote features -- switching servers, refusing while
devices are held, mocks and the knowledge base following a switch -- lives in
the MCP, and was tested only against fake servers (`mcp/test/use-remote*`).
Here the checkout's own MCP runs over stdio with a sandboxed HOME, exactly as
an agent's client starts it. Its "local" server is a scratch quern whose state
is that HOME's `~/.quern`, paired with `quern connect` to a second scratch
quern's remote listener, named `studio`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from .conftest import REPO, Server, _free_port, pairing_code, quern_cli
from .mcp_client import DIST, McpClient

#: The MCP's own floor (`mcp/src/launcher.cjs`).
NODE_FLOOR = (22, 19)


@pytest.fixture(scope="module")
def node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH; the MCP needs Node 22.19 or later")
    version = subprocess.run([path, "--version"], capture_output=True, text=True,  # noqa: S603
                             timeout=10, check=False).stdout.strip().lstrip("v")
    if tuple(int(p) for p in version.split(".")[:2]) < NODE_FLOOR:
        pytest.skip(f"node {version} is older than the MCP's floor 22.19")
    return path


@pytest.fixture(scope="module")
def built_mcp(node: str) -> Path:
    """This checkout's MCP, built if its sources are newer than the build:
    a stale `dist` would test some other version of the code."""
    src = REPO / "mcp" / "src"
    newest = max(p.stat().st_mtime for p in src.rglob("*.ts"))
    if DIST.exists() and DIST.stat().st_mtime >= newest:
        return DIST
    if not (REPO / "mcp" / "node_modules").exists():
        pytest.skip("mcp/dist is missing or older than mcp/src, and there is no "
                    "mcp/node_modules to build it with (npm ci in mcp/)")
    done = subprocess.run(["npm", "run", "build"], cwd=REPO / "mcp",  # noqa: S607
                          capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, f"building the MCP failed:\n{done.stdout}{done.stderr}"
    return DIST


@dataclass
class Pair:
    """This Mac (`local`), the agent's HOME and project, and `studio`."""

    home: Path
    project: Path
    local: Server
    studio: Server


def _warm(server: Server) -> None:
    """Wait out the first device scan, which a fresh server does at start:
    asked during it, the device list times out and reads as a finding."""
    owner = server.owner()
    try:
        owner.json_ok("GET", "/api/v1/device/list", timeout=120)
        owner.json_ok("GET", "/api/v1/proxy/status", timeout=60)
    finally:
        owner.close()


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory, built_mcp: Path) -> Iterator[Pair]:
    home = tmp_path_factory.mktemp("agent-home")
    local = Server(state_dir=home / ".quern", port=_free_port(), proxy_port=_free_port(),
                   remote_port=_free_port(), proxy=True)
    studio = Server(state_dir=tmp_path_factory.mktemp("studio"), port=_free_port(),
                    proxy_port=_free_port(), remote_port=_free_port(), proxy=True)
    project = tmp_path_factory.mktemp("project")
    shutil.copytree(REPO / "tools" / "probe-app" / "knowledge",
                    project / ".quern" / "knowledge")
    (project / ".quern" / "config.json").write_text(
        json.dumps({"bundle_id": "com.quern.probe", "platform": "ios"}))
    started: list[Server] = []
    try:
        for srv in (local, studio):
            srv.configure()
            srv.start()
            started.append(srv)
        for srv in started:
            _warm(srv)
        done = quern_cli(local.state_dir, "connect", studio.remote_url,
                         "--code", pairing_code(studio, "laptop"), "--name", "studio")
        assert done.returncode == 0, f"quern connect: {done.stdout}{done.stderr}"
        yield Pair(home=home, project=project, local=local, studio=studio)
    finally:
        for srv in started:
            srv.stop()


@pytest.fixture
def agent(node: str, pair: Pair) -> Iterator[McpClient]:
    """A fresh agent for each test: its own MCP process and held session."""
    mcp = McpClient(node, pair.home, pair.project)
    try:
        yield mcp
    finally:
        mcp.close()


def _holder(server: Server, udid: str) -> str | None:
    """Who holds `udid`, as that server's owner sees it."""
    owner = server.owner()
    try:
        devices = owner.json_ok("GET", "/api/v1/device/list", timeout=60)["devices"]
    finally:
        owner.close()
    for dev in devices:
        if dev["udid"] == udid:
            return (dev.get("reservation") or {}).get("held_by")
    raise AssertionError(f"{udid} is not listed on {server.url}")


def _mock_patterns(server: Server) -> list[str]:
    owner = server.owner()
    try:
        return [r["pattern"] for r in owner.json_ok("GET", "/api/v1/proxy/mocks")["rules"]]
    finally:
        owner.close()


def _ok(answer) -> dict:
    assert not answer.is_error and isinstance(answer.data, dict), answer
    return answer.data


def test_list_remotes_puts_this_mac_first_and_the_paired_one_after(agent) -> None:
    rows = _ok(agent.call("list_remotes", timeout=120))["remotes"]
    assert [r["name"] for r in rows] == ["local", "studio"], rows
    local, studio = rows
    assert local["current"] is True and studio["current"] is False
    for row in rows:
        assert row["reachable"] is True, row
        assert isinstance(row["devices"], list), f"devices not listed: {row}"


def test_use_remote_moves_the_agents_calls_to_the_other_server(
    agent, pair: Pair, simulator: str,
) -> None:
    """Judged from the servers' side: after the switch, a reservation lands
    on studio, under the key `quern connect` was paired with, and not here."""
    switched = _ok(agent.call("use_remote", name="studio", timeout=60))
    assert switched["current"] == "studio", switched

    _ok(agent.call("reserve_devices", udids=[simulator], timeout=60))
    holder = _holder(pair.studio, simulator)
    assert holder is not None and holder.startswith("laptop:"), holder
    assert _holder(pair.local, simulator) is None, "the reservation landed on local too"


def test_use_remote_refuses_while_devices_are_held_here(
    agent, pair: Pair, simulator: str,
) -> None:
    _ok(agent.call("reserve_devices", udids=[simulator], timeout=60))

    refused = agent.call("use_remote", name="studio", timeout=60)
    assert refused.is_error, refused
    assert "release=true" in refused.text, refused
    assert _holder(pair.local, simulator) is not None, "a refused switch let go of the device"

    current = _ok(agent.call("list_remotes", timeout=120))["remotes"]
    assert next(r for r in current if r["current"])["name"] == "local"


def test_release_true_releases_here_and_switches(agent, pair: Pair, simulator: str) -> None:
    _ok(agent.call("reserve_devices", udids=[simulator], timeout=60))

    switched = _ok(agent.call("use_remote", name="studio", release=True, timeout=60))
    assert switched["current"] == "studio"
    assert simulator in switched.get("released", []), switched
    assert _holder(pair.local, simulator) is None, "released, but still held on local"


def test_a_mock_follows_the_agent_to_the_other_server(
    agent, pair: Pair, simulator: str,
) -> None:
    """Removed from the server left, and set up again on the next
    reservation's simulators there, with a new rule id."""
    pattern = "~d conformance-carry.invalid"
    _ok(agent.call("reserve_devices", udids=[simulator], timeout=60))
    _ok(agent.call("set_mock", pattern=pattern, simulator_udid=simulator, body="{}"))
    assert pattern in _mock_patterns(pair.local)

    switched = _ok(agent.call("use_remote", name="studio", release=True, timeout=60))
    assert switched.get("carried", {}).get("mocks") == 1, switched
    assert pattern not in _mock_patterns(pair.local), "the mock stayed on the server left"

    reserved = _ok(agent.call("reserve_devices", udids=[simulator], timeout=60))
    recreated = reserved.get("carried", {}).get("mocks_recreated", [])
    assert [m["pattern"] for m in recreated] == [pattern], reserved
    assert pattern in _mock_patterns(pair.studio), "said recreated, but studio has no such mock"


def test_the_projects_knowledge_base_follows_the_agent(agent) -> None:
    switched = _ok(agent.call("use_remote", name="studio", timeout=60))
    knowledge = switched.get("knowledge") or {}
    assert knowledge.get("kind") in {"loaded", "session_loaded", "already"}, switched
    assert knowledge.get("screens", 0) > 0, knowledge


def test_use_remote_local_comes_back(agent, pair: Pair, simulator: str) -> None:
    _ok(agent.call("use_remote", name="studio", timeout=60))
    back = _ok(agent.call("use_remote", name="local", timeout=60))
    assert back["current"] == "local"

    _ok(agent.call("reserve_devices", udids=[simulator], timeout=60))
    assert _holder(pair.local, simulator) is not None
    assert _holder(pair.studio, simulator) is None


def test_a_remote_that_does_not_answer_is_refused_and_the_agent_stays(
    agent, pair: Pair,
) -> None:
    """The switch asks the new server first: a server that is down leaves
    the agent where it was, and says why."""
    pair.studio.stop()
    try:
        refused = agent.call("use_remote", name="studio", timeout=60)
        current = _ok(agent.call("list_remotes", timeout=120))["remotes"]
    finally:
        pair.studio.start()
        _warm(pair.studio)
    assert refused.is_error, refused
    assert "did not answer" in refused.text and "still using local" in refused.text, refused
    assert next(r for r in current if r["current"])["name"] == "local"
