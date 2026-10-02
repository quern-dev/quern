"""Simulated location, read back from QuernProbe's Location tab.

`set_location` answers `{"status": "ok"}` for a coordinate the device may
never deliver, so every assertion here reads what the *app* received: the
latitude and longitude labels, and an update counter that shows a new fix
arrived rather than an old one being displayed.

The app needs location permission. The probe fixtures grant it before the
first launch and fail setup if the grant is refused, and these tests then
require the app's `location_auth` label to show it.
"""

from __future__ import annotations

import random
import time

import pytest

from tests.conformance.probe import Ids


def _labels(probe) -> dict[str, str | None]:
    ids = probe.contract
    return {
        name: probe.text_of(ids.id_for(logical))
        for name, logical in (
            ("auth", Ids.LOCATION_AUTH), ("lat", Ids.LOCATION_LAT),
            ("lon", Ids.LOCATION_LON), ("count", Ids.LOCATION_COUNT),
        )
    }


def _updates(text: str | None) -> int:
    try:
        return int((text or "").rsplit(":", 1)[-1].strip())
    except ValueError:
        return -1


def _physical_android(probe) -> bool:
    # Location simulation rides the emulator console, which only an
    # `emulator-NNNN` serial reaches.
    return probe.contract.platform == "android" and not probe.udid.startswith("emulator-")


@pytest.fixture
def location_tab(probe):
    if _physical_android(probe):
        pytest.skip("a physical Android phone has no emulator console to simulate location")
    probe.goto("location")
    auth = _labels(probe)["auth"] or ""
    # A failure, not a skip: the fixture granted the permission and required
    # the grant to succeed, so an app that still reports none is the defect.
    # As a skip it hid exactly that -- the iOS probe never read its own
    # authorization, and these tests skipped on every simulator run.
    assert any(word in auth for word in ("granted", "whenInUse", "always")), (
        f"the probe app reports no location permission after it was granted: {auth!r}"
    )
    return probe


def _set_and_wait(quern, probe, lat: float, lon: float, timeout_s: float = 20.0) -> dict:
    quern.json_ok(
        "POST", "/api/v1/device/location",
        json={"udid": probe.udid, "latitude": lat, "longitude": lon}, timeout=60.0,
    )
    deadline = time.monotonic() + timeout_s
    seen: dict = {}
    while time.monotonic() < deadline:
        seen = _labels(probe)
        got_lat, got_lon = _number(seen["lat"]), _number(seen["lon"])
        # Within 1e-5 degrees (about a metre), not to the printed digit: the
        # emulator's console path rounds the last place, measured -58.464019
        # arriving as -58.464018.
        if (got_lat is not None and got_lon is not None
                and abs(got_lat - lat) < 1e-5 and abs(got_lon - lon) < 1e-5):
            return seen
        time.sleep(1.0)
    raise AssertionError(
        f"set_location({lat}, {lon}) never reached the app within {timeout_s:.0f}s; "
        f"it shows {seen}"
    )


def _number(text: str | None) -> float | None:
    try:
        return float((text or "").rsplit(":", 1)[-1].strip())
    except ValueError:
        return None


def _point() -> tuple[float, float]:
    """A coordinate no earlier run left on screen, so a stale fix cannot pass."""
    return round(random.uniform(-60, 60), 6), round(random.uniform(-170, 170), 6)


def test_a_set_location_reaches_the_app(quern, location_tab) -> None:
    lat, lon = _point()
    _set_and_wait(quern, location_tab, lat, lon)


def test_a_second_location_replaces_the_first_as_a_new_update(quern, location_tab) -> None:
    """The counter has to move: identical labels from an old fix would also
    'show the coordinate', which is the false pass this rules out."""
    first = _set_and_wait(quern, location_tab, *_point())
    second = _set_and_wait(quern, location_tab, *_point())
    assert _updates(second["count"]) > _updates(first["count"]), (first, second)


@pytest.mark.parametrize("lat, lon", [(91.0, 0.0), (0.0, 181.0), (-91.0, 0.0)])
def test_a_coordinate_off_the_globe_is_refused(quern, probe, lat, lon) -> None:
    """Refused before it reaches the device, rather than handed to simctl or
    adb to fail -- or not -- in its own way."""
    resp = quern.post(
        "/api/v1/device/location",
        json={"udid": probe.udid, "latitude": lat, "longitude": lon}, timeout=60.0,
    )
    # 422 and nothing else: a physical Android phone refuses any location with
    # a 400 (F28), so accepting 400 here would pass there with validation gone.
    assert resp.status_code == 422, f"{resp.status_code}: {resp.text[:300]}"


def test_a_physical_android_phone_refuses_clearly(quern, probe) -> None:
    """A 400 that says why, not a 500 that reads as quern breaking (F28)."""
    if not _physical_android(probe):
        pytest.skip("only a physical Android phone lacks the emulator console")
    resp = quern.post(
        "/api/v1/device/location",
        json={"udid": probe.udid, "latitude": 10.0, "longitude": 10.0}, timeout=60.0,
    )
    assert resp.status_code == 400, f"{resp.status_code}: {resp.text[:300]}"
    assert "emulator console" in resp.text, resp.text[:300]

