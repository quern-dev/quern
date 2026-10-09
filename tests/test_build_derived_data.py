"""Every build_and_install gets a DerivedData of its own (#442).

It was `~/.quern/builds/<scheme>`. Two checkouts building a scheme of one name
shared it, and the second failed on a precompiled module the first had left.
And a request for a phone and a simulator ran both xcodebuilds at once inside
it, which Xcode does not support: measured 1 in 3, "The Xcode build system has
crashed. Build again to continue."
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.api import build_app as route
from server.builds import build_records
from server.models import BuildResult
from tests.test_build_records import HEADERS, FakeController, FakeDsymutil, _app

PHONE, SIM = "00008101-PHONE", "SIM-UDID"


@pytest.fixture
def builds(tmp_path, monkeypatch):
    """The route with xcodebuild replaced by a fake that notes where each build
    went and how many ran into one directory at once."""
    from server.config import ServerConfig
    from server.main import create_app

    app = create_app(config=ServerConfig(api_key="test-key-12345"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    app.state.build_adapter = object()
    app.state.device_controller = FakeController({PHONE: "device", SIM: "simulator"})
    built = _app(tmp_path)
    state = {"dirs": [], "active": {}, "overlap": set(), "most": 0, "release": None}

    async def fake_build(proj_flag, proj_path, scheme, config, destination, derived, *_a):
        state["dirs"].append((proj_path, destination, derived))
        state["active"][derived] = state["active"].get(derived, 0) + 1
        if state["active"][derived] > 1:
            state["overlap"].add(derived)
        state["most"] = max(state["most"], sum(state["active"].values()))
        try:
            if state["release"] is not None:
                await state["release"].wait()
            else:
                await asyncio.sleep(0.05)
        finally:
            state["active"][derived] -= 1
        return BuildResult(succeeded=True)

    monkeypatch.setattr(route, "_build", fake_build)
    monkeypatch.setattr(route, "_find_app", lambda derived, config, physical: built)
    monkeypatch.setattr(route, "CONFIG_DIR", tmp_path / "state")
    monkeypatch.setattr(build_records, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(build_records, "_run", FakeDsymutil())
    state["app"] = app
    state["root"] = tmp_path
    return state


def _project(root: Path, checkout: str) -> Path:
    project = root / checkout / "MyApp.xcworkspace"
    project.mkdir(parents=True, exist_ok=True)
    return project


async def _post(app, project: Path, udids: list[str], scheme="Internal"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/device/build-and-install", headers=HEADERS,
                                 json={"project_path": str(project), "scheme": scheme,
                                       "udids": udids})


async def test_two_checkouts_of_one_scheme_build_apart(builds):
    one, two = _project(builds["root"], "spike"), _project(builds["root"], "develop")
    assert (await _post(builds["app"], one, [SIM])).status_code == 200
    assert (await _post(builds["app"], two, [SIM])).status_code == 200
    [(_, _, first), (_, _, second)] = builds["dirs"]
    assert first != second
    assert first.parent.name == second.parent.name == "Internal"


async def test_one_checkout_builds_into_the_same_place_each_time(builds):
    """Incremental builds keep working: the key is stable."""
    project = _project(builds["root"], "develop")
    await _post(builds["app"], project, [SIM])
    await _post(builds["app"], project, [SIM])
    [(_, _, first), (_, _, again)] = builds["dirs"]
    assert first == again
    assert first.parent.parent.name.startswith("MyApp-"), "the name says which project"


async def test_a_phone_and_a_simulator_build_into_two_directories_at_once(builds):
    project = _project(builds["root"], "develop")
    resp = await _post(builds["app"], project, [PHONE, SIM])
    assert resp.status_code == 200, resp.text
    dirs = {destination: derived for _, destination, derived in builds["dirs"]}
    assert dirs["generic/platform=iOS"] != dirs["generic/platform=iOS Simulator"]
    assert builds["overlap"] == set()
    assert builds["most"] == 2, "the two platforms still build concurrently"


async def test_two_requests_for_one_directory_take_turns(builds):
    project = _project(builds["root"], "develop")
    first, second = await asyncio.gather(_post(builds["app"], project, [SIM]),
                                         _post(builds["app"], project, [SIM]))
    assert first.status_code == second.status_code == 200
    assert len(builds["dirs"]) == 2
    assert builds["overlap"] == set(), "two builds ran in one DerivedData at once"


async def test_the_turn_lasts_until_the_install_is_done(builds, monkeypatch):
    """The products are what is installed: a second build must not replace
    them while the first request is still copying them."""
    project = _project(builds["root"], "develop")
    events = []
    controller = builds["app"].state.device_controller
    original = controller.install_app

    async def install(app_path, udid):
        events.append("install start")
        await asyncio.sleep(0.05)
        events.append("install end")
        await original(app_path, udid)

    controller.install_app = install

    real_build = route._build

    async def build(*args):
        events.append("build")
        return await real_build(*args)

    monkeypatch.setattr(route, "_build", build)
    await asyncio.gather(_post(builds["app"], project, [SIM]),
                         _post(builds["app"], project, [SIM]))
    assert events == ["build", "install start", "install end",
                      "build", "install start", "install end"]


async def test_two_projects_still_build_at_once(builds):
    one, two = _project(builds["root"], "a"), _project(builds["root"], "b")
    await asyncio.gather(_post(builds["app"], one, [SIM]), _post(builds["app"], two, [SIM]))
    assert builds["most"] == 2


async def test_a_request_cancelled_while_waiting_holds_nothing(tmp_path):
    """Cancelled while waiting for its second directory, it releases its first."""
    a, b = tmp_path / "a", tmp_path / "b"
    blocker_holds, release = asyncio.Event(), asyncio.Event()

    async def blocker():
        async with route._holding([b]):
            blocker_holds.set()
            await release.wait()

    async def waiter():
        async with route._holding([a, b]):
            pass

    held_b = asyncio.create_task(blocker())
    await blocker_holds.wait()
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert route._DERIVED_LOCKS[a].locked(), "it took the first and waits for the second"
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    lock_a = route._DERIVED_LOCKS[a]
    assert not lock_a.locked()
    release.set()
    await held_b


async def test_the_order_is_fixed_so_two_requests_cannot_deadlock(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    done = []

    async def take(dirs, name):
        async with route._holding(dirs):
            await asyncio.sleep(0.01)
            done.append(name)

    await asyncio.wait_for(asyncio.gather(take([a, b], "ab"), take([b, a], "ba")), 2)
    assert sorted(done) == ["ab", "ba"]


def test_the_key_is_the_resolved_project_path():
    one = route._derived_data("/src/develop/MyApp.xcworkspace", "Internal", "iphoneos")
    two = route._derived_data("/src/spike/MyApp.xcworkspace", "Internal", "iphoneos")
    sim = route._derived_data("/src/develop/MyApp.xcworkspace", "Internal", "iphonesimulator")
    assert len({one, two, sim}) == 3
    assert one.parent == sim.parent
