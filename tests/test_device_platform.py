"""Which platform a device request is for (#582): server/device/platform.py,
and the pool, reservations and the device list that use it.

The case this exists for: iOS 16 and Android 16 both exist, so a bare "16"
matched both, and an agent asking for an Android 16 emulator could be handed
an iOS 16 simulator."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device import platform
from server.device.pool import DevicePool
from server.models import DevicePoolEntry, DevicePoolState, DeviceState, DeviceType


def _entry(udid, name, os_version, device_type, state=DeviceState.BOOTED, family="", model=""):
    return DevicePoolEntry(
        udid=udid, name=name, state=state, device_type=device_type, os_version=os_version,
        runtime="", device_family=family, model=model, last_used=datetime.now(UTC),
        is_available=True)


IOS16 = _entry("A1B2C3D4-0000-0000-0000-000000000016", "iPhone 14", "iOS 16.4",
               DeviceType.SIMULATOR, family="iPhone")
IOS18 = _entry("A1B2C3D4-0000-0000-0000-000000000018", "iPhone 16 Pro", "iOS 18.6",
               DeviceType.SIMULATOR, family="iPhone")
ANDROID16 = _entry("emulator-5554", "Pixel_8_API_36", "16", DeviceType.ANDROID_EMULATOR,
                   family="Android", model="Pixel 8")
ANDROID14 = _entry("emulator-5556", "Medium_Phone_API_34", "14", DeviceType.ANDROID_EMULATOR,
                   family="Android")


# ----------------------------------------------------------------------------
# Parsing and matching
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("requested, expected", [
    ("16", (None, "16")),
    ("18.6", (None, "18.6")),
    ("iOS 18.6", ("ios", "18.6")),
    ("ios18", ("ios", "18")),
    ("Android 16", ("android", "16")),
    ("android16", ("android", "16")),
    ("ANDROID", ("android", None)),
    ("", (None, None)),
    (None, (None, None)),
    ("Android API 34", (None, "34")),        # unrecognised: the old meaning
])
def test_parse_os(requested, expected):
    assert platform.parse_os(requested) == expected


@pytest.mark.parametrize("device_os, device_type, requested, expected", [
    ("iOS 16.4", DeviceType.SIMULATOR, "16", True),
    ("16", DeviceType.ANDROID_EMULATOR, "16", True),
    ("iOS 16.4", DeviceType.SIMULATOR, "Android 16", False),
    ("16", DeviceType.ANDROID_EMULATOR, "iOS 16", False),
    ("14", DeviceType.ANDROID_DEVICE, "android14", True),
    ("iOS 18.2", DeviceType.SIMULATOR, "18.6", False),
    ("iOS 180.1", DeviceType.SIMULATOR, "18", False),
    ("iOS 18.2", DeviceType.DEVICE, "ios", True),
])
def test_os_matches(device_os, device_type, requested, expected):
    assert platform.os_matches(device_os, device_type, requested) is expected


def test_two_named_platforms_conflict():
    with pytest.raises(platform.PlatformConflict):
        platform.requested_platform(platform=None, source=None,
                                    device_type=DeviceType.SIMULATOR, os_version="Android 16")


def test_a_platform_with_a_source_is_not_a_request():
    assert platform.requested_platform(platform="ios", source="knowledge_base",
                                       device_type=None, os_version=None) is None
    assert platform.requested_platform(platform="ios", source=None,
                                       device_type=None, os_version=None) == "ios"


# ----------------------------------------------------------------------------
# choose: the order
# ----------------------------------------------------------------------------


def _finder(devices, criteria=lambda d: True):
    def find(on):
        return [d for d in platform.on_platform(devices, on) if criteria(d)]
    return find


ALL = [IOS16, IOS18, ANDROID16, ANDROID14]


def test_a_requested_platform_restricts():
    c = platform.choose(_finder(ALL), requested="android", default=None,
                        default_source=None, has_criteria=False)
    assert {d.udid for d in c.devices} == {ANDROID16.udid, ANDROID14.udid}
    assert c.report() == {"platform": "android", "platform_source": "request"}


def test_the_projects_platform_is_the_default():
    c = platform.choose(_finder(ALL), requested=None, default="ios",
                        default_source="knowledge_base", has_criteria=False)
    assert {d.udid for d in c.devices} == {IOS16.udid, IOS18.udid}
    assert c.report() == {"platform": "ios", "platform_source": "knowledge_base"}


def test_one_match_on_the_other_platform_is_used():
    # Naming a Pixel from an iOS project is asking for it.
    pixel = _finder(ALL, lambda d: d.model == "Pixel 8")
    c = platform.choose(pixel, requested=None, default="ios",
                        default_source="project", has_criteria=True)
    assert [d.udid for d in c.devices] == [ANDROID16.udid]
    assert c.report() == {"platform": "android", "platform_source": "only_match"}


def test_several_on_the_other_platform_are_listed_not_picked():
    android = _finder(ALL, lambda d: d.device_family == "Android")
    with pytest.raises(platform.PlatformRefusal) as e:
        platform.choose(android, requested=None, default="ios",
                        default_source="project", has_criteria=True)
    assert e.value.detail["error"] == "other_platform"
    assert e.value.detail["retry_with"] == [{"platform": "android"}]
    assert {d["udid"] for d in e.value.detail["matches"]["android"]} == {
        ANDROID16.udid, ANDROID14.udid}


def test_no_criteria_never_crosses_to_the_other_platform():
    # An Android project with no emulators is not asking for any iOS device.
    c = platform.choose(_finder([IOS18]), requested=None, default="android",
                        default_source="project", has_criteria=False)
    assert c.devices == [] and c.platform == "android"


def test_a_bare_version_on_both_platforms_is_refused_with_both_sides():
    sixteen = _finder(ALL, lambda d: platform.os_matches(d.os_version, d.device_type, "16"))
    with pytest.raises(platform.PlatformRefusal) as e:
        platform.choose(sixteen, requested=None, default=None,
                        default_source=None, has_criteria=True)
    d = e.value.detail
    assert d["error"] == "ambiguous_platform"
    assert d["match_counts"] == {"ios": 1, "android": 1}
    assert d["retry_with"] == [{"platform": "ios"}, {"platform": "android"}]


def test_matches_on_one_platform_are_that_platforms():
    fourteen = _finder(ALL, lambda d: platform.os_matches(d.os_version, d.device_type, "14"))
    c = platform.choose(fourteen, requested=None, default=None,
                        default_source=None, has_criteria=True)
    assert [d.udid for d in c.devices] == [ANDROID14.udid]
    assert c.report() == {"platform": "android", "platform_source": "only_match"}


def test_a_listing_shows_both_rather_than_refusing():
    sixteen = _finder(ALL, lambda d: platform.os_matches(d.os_version, d.device_type, "16"))
    c = platform.choose(sixteen, requested=None, default=None, default_source=None,
                        has_criteria=True, refuse_ambiguous=False)
    assert {d.udid for d in c.devices} == {IOS16.udid, ANDROID16.udid}
    assert c.report() == {}


# ----------------------------------------------------------------------------
# The pool: resolve_device and ensure_devices
# ----------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path):
    controller = MagicMock()
    controller._active_udid = None
    p = DevicePool(controller)
    p._pool_file = tmp_path / "device-pool.json"
    state = DevicePoolState(updated_at=datetime.now(UTC),
                            devices={d.udid: d for d in ALL})
    p.refresh = AsyncMock()
    p._read_state = lambda: state
    return p


async def test_resolve_refuses_a_bare_version_both_platforms_have(pool):
    with pytest.raises(platform.PlatformRefusal) as e:
        await pool.resolve_device(os_version="16", default_device_type=DeviceType.SIMULATOR)
    assert e.value.detail["error"] == "ambiguous_platform"


async def test_resolve_takes_the_projects_platform(pool):
    report: dict = {}
    udid = await pool.resolve_device(os_version="16", requested_platform="android",
                                     platform_source="knowledge_base", report=report,
                                     default_device_type=DeviceType.SIMULATOR)
    assert udid == ANDROID16.udid
    assert report == {"platform": "android", "platform_source": "knowledge_base"}


async def test_resolve_with_no_criteria_in_an_android_project_is_an_emulator(pool):
    # The documented simulator default is iOS's: it never applies to Android.
    udid = await pool.resolve_device(requested_platform="android", platform_source="project",
                                     default_device_type=DeviceType.SIMULATOR)
    assert udid in {ANDROID16.udid, ANDROID14.udid}


async def test_resolve_with_nothing_said_keeps_the_simulator_default(pool):
    udid = await pool.resolve_device(default_device_type=DeviceType.SIMULATOR)
    assert udid in {IOS16.udid, IOS18.udid}


async def test_the_active_device_is_kept_only_on_the_requests_platform(pool):
    # No criteria returns the active device -- unless it is on another
    # platform than the request's, the project's included: the active device
    # is server-wide, so anyone may have set it, and a pick with no criteria
    # never crosses platforms (review).
    for source in (None, "project"):
        pool.controller._active_udid = IOS18.udid
        assert await pool.resolve_device(requested_platform="android",
                                         platform_source=source) in {
            ANDROID16.udid, ANDROID14.udid}, source
    pool.controller._active_udid = IOS18.udid
    assert await pool.resolve_device(requested_platform="ios",
                                     platform_source="project") == IOS18.udid


async def test_resolve_finds_an_avd_by_its_spaced_name(pool):
    # "Pixel 8 API 36" is the AVD Pixel_8_API_36 (#582).
    udid = await pool.resolve_device(name="Pixel 8 API 36", requested_platform="ios",
                                     platform_source="project",
                                     default_device_type=DeviceType.SIMULATOR)
    assert udid == ANDROID16.udid


async def test_resolve_with_a_prefixed_version_needs_no_platform(pool):
    report: dict = {}
    udid = await pool.resolve_device(os_version="iOS 16", report=report,
                                     default_device_type=DeviceType.SIMULATOR)
    assert udid == IOS16.udid
    assert report["platform_source"] == "request"


async def test_ensure_follows_the_same_rule(pool):
    with pytest.raises(platform.PlatformRefusal):
        await pool.ensure_devices(count=1, os_version="16",
                                  default_device_type=DeviceType.SIMULATOR)
    udids = await pool.ensure_devices(count=1, os_version="16", requested_platform="ios",
                                      platform_source="project",
                                      default_device_type=DeviceType.SIMULATOR)
    assert udids == [IOS16.udid]


# ----------------------------------------------------------------------------
# The routes: refusals are structured
# ----------------------------------------------------------------------------


@pytest.fixture
def client(pool):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.api.device_pool import router

    app = FastAPI()
    app.include_router(router)
    app.state.device_pool = pool
    pool.get_device_state = AsyncMock(return_value=SimpleNamespace(
        name="x", state=DeviceState.BOOTED, os_version="16"))
    return TestClient(app)


def test_the_resolve_route_answers_ambiguity_with_409_and_both_sides(client):
    r = client.post("/api/v1/devices/resolve", json={"os_version": "16"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "ambiguous_platform"
    assert set(detail["matches"]) == {"ios", "android"}


def test_the_resolve_route_reports_the_platform_it_used(client):
    r = client.post("/api/v1/devices/resolve",
                    json={"os_version": "16", "platform": "android",
                          "platform_source": "knowledge_base"})
    assert r.status_code == 200, r.text
    assert r.json()["udid"] == ANDROID16.udid
    assert r.json()["platform_source"] == "knowledge_base"


def test_the_resolve_route_refuses_two_platforms_with_400(client):
    r = client.post("/api/v1/devices/resolve",
                    json={"device_type": "simulator", "os_version": "android 16"})
    assert r.status_code == 400


# ----------------------------------------------------------------------------
# Review findings (#582)
# ----------------------------------------------------------------------------


PHONE14 = _entry("R5CR10XXXXX", "SM-G960F", "14", DeviceType.ANDROID_DEVICE,
                 family="Android", model="Galaxy S9")


def _pool_of(tmp_path, devices):
    controller = MagicMock()
    controller._active_udid = None
    p = DevicePool(controller)
    p._pool_file = tmp_path / "device-pool.json"
    state = DevicePoolState(updated_at=datetime.now(UTC), devices={d.udid: d for d in devices})
    p.refresh = AsyncMock()
    p._read_state = lambda: state
    return p


async def test_saying_nothing_never_resolves_a_physical_phone(tmp_path):
    # The simulator default means "a virtual device": crossing to Android by
    # criteria is to an emulator, not to a phone on the desk.
    p = _pool_of(tmp_path, [IOS18, PHONE14])
    with pytest.raises(Exception, match="No device matching"):
        await p.resolve_device(os_version="14", default_device_type=DeviceType.SIMULATOR)
    p = _pool_of(tmp_path, [IOS18, PHONE14, ANDROID14])
    assert await p.resolve_device(os_version="14",
                                  default_device_type=DeviceType.SIMULATOR) == ANDROID14.udid
    # Asked for by type, the phone is found as before.
    assert await p.resolve_device(os_version="14", device_type=DeviceType.ANDROID_DEVICE,
                                  default_device_type=DeviceType.SIMULATOR) == PHONE14.udid


async def test_crossing_takes_as_many_as_the_request_needs(tmp_path):
    p = _pool_of(tmp_path, [IOS18, ANDROID16, ANDROID14])
    udids = await p.ensure_devices(count=2, device_family="Android",
                                   requested_platform="ios", platform_source="project",
                                   default_device_type=DeviceType.SIMULATOR)
    assert set(udids) == {ANDROID16.udid, ANDROID14.udid}


async def test_an_exact_name_on_one_platform_beats_a_substring_on_the_other(tmp_path):
    ci_avd = _entry("emulator-5560", "CI", "15", DeviceType.ANDROID_EMULATOR, family="Android")
    ci_sim = _entry("A1B2C3D4-0000-0000-0000-0000000000C3", "CI 3", "iOS 18.6",
                    DeviceType.SIMULATOR, family="iPhone")
    p = _pool_of(tmp_path, [ci_avd, ci_sim])
    assert await p.resolve_device(name="CI", default_device_type=DeviceType.SIMULATOR) == \
        ci_avd.udid


async def test_the_error_is_about_the_platform_asked_for(tmp_path):
    shut = _entry("A1B2C3D4-0000-0000-0000-000000000017", "iPhone 15", "iOS 17.5",
                  DeviceType.SIMULATOR, state=DeviceState.SHUTDOWN, family="iPhone")
    p = _pool_of(tmp_path, [IOS18, shut])
    # No type default (a pool-level caller): the diagnosis used to look at
    # every platform and offer the iOS simulator as a shut-down "match".
    for kwargs in ({"default_device_type": DeviceType.SIMULATOR}, {}):
        with pytest.raises(Exception) as e:
            await p.resolve_device(requested_platform="android", auto_boot=False, **kwargs)
        assert "iPhone" not in str(e.value) and "auto_boot" not in str(e.value), kwargs


async def test_a_udid_is_not_refused_over_its_other_fields(tmp_path):
    p = _pool_of(tmp_path, [IOS18])
    assert await p.resolve_device(udid=IOS18.udid, device_type=DeviceType.SIMULATOR,
                                  os_version="Android 16") == IOS18.udid


def test_the_ensure_route_answers_refusals_and_conflicts(client):
    r = client.post("/api/v1/devices/ensure", json={"count": 1, "os_version": "16"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "ambiguous_platform"
    r = client.post("/api/v1/devices/ensure",
                    json={"count": 1, "device_type": "simulator", "os_version": "android 16"})
    assert r.status_code == 400


def test_an_unknown_platform_source_is_refused(client):
    r = client.post("/api/v1/devices/resolve",
                    json={"platform": "ios", "platform_source": "REQUEST"})
    assert r.status_code == 422
