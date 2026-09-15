"""Discovery self-tests: prove the capability map itself is trustworthy.

These run first and are the suite's own foundation. If the environment block is
wrong, every skip and every failure downstream is suspect -- a run that reports
"no Android" because the device-type string changed looks identical to a run on
a machine with no phone. So the mapping is asserted, not assumed.
"""

from __future__ import annotations

import pytest

from tests.conformance.capabilities import ROLE_DEVICE_TYPE, Environment, Role
from tests.conformance.client import HealthReport, QuernClient


def test_server_is_reachable_and_names_its_version(health: HealthReport) -> None:
    assert health.reachable
    assert health.version, "/health answered without a version field"


def test_environment_reports_something_testable(environment: Environment) -> None:
    """At least one role must be usable, or the run is vacuous.

    Deliberately a failure and not a skip. A conformance suite that skips every
    tier has not passed; it has not run, and a release gate needs to tell those
    apart.
    """
    if any(environment.has(role) for role in Role):
        return
    pytest.fail(
        "no testable target on this machine — the suite would report a pass "
        "having exercised nothing.\n" + environment.summary(),
        pytrace=False,
    )


def test_tool_discovery_answered(environment: Environment) -> None:
    """`/tools` is what `quern doctor` calls; it must answer, and promptly.

    Separated from the device probe because the failure modes differ: `/tools`
    shelling out to a wedged CLI is a server-side bug, and it presents as
    doctor hanging for the user.
    """
    if environment.tools_error:
        pytest.fail(environment.tools_error, pytrace=False)
    assert environment.tools, "/tools returned an empty tool map"


def test_device_discovery_answered(environment: Environment) -> None:
    if environment.devices_error:
        pytest.fail(environment.devices_error, pytrace=False)


def test_every_role_is_explained(environment: Environment) -> None:
    """An unavailable role must say why. Silence here becomes a silent skip."""
    for role in Role:
        avail = environment.role(role)
        assert avail.available or avail.blocked_reason, (
            f"{role.value} is unavailable with no reason recorded"
        )


def test_reported_device_types_are_the_documented_set(
    environment: Environment,
) -> None:
    """Guard the role mapping against a server-side rename.

    `/device/list` documents its `device_type` filter as exactly these four
    values. If a device arrives with a fifth, the role map silently drops it and
    the affected tier reports "no hardware" on a machine that has it.
    """
    known = set(ROLE_DEVICE_TYPE.values())
    unknown = {d.device_type for d in environment.devices} - known
    assert not unknown, (
        f"device types {sorted(unknown)} are not in the role map {sorted(known)}; "
        "tests for those devices would be skipped as 'no hardware'"
    )


def test_device_list_filters_agree_with_the_full_list(
    quern: QuernClient, environment: Environment, authenticated: None
) -> None:
    """Server-side filtering must be a subset of, and consistent with, the whole.

    This is the first real API assertion in the suite and it is here on purpose:
    every later test selects a device through these filters, so a filter that
    silently returns everything would make the rest of the suite test the wrong
    device without ever failing.
    """
    for role, device_type in ROLE_DEVICE_TYPE.items():
        expected = {
            d.udid for d in environment.devices if d.device_type == device_type
        }
        body = quern.json_ok(
            "GET",
            "/api/v1/device/list",
            params={"device_type": device_type, "include_disconnected": True},
            timeout=90.0,
        )
        got = {d.get("udid") for d in body.get("devices") or []}
        assert got == expected, (
            f"device_type={device_type!r} returned {len(got)} device(s), but the "
            f"unfiltered list has {len(expected)} of that type "
            f"(role {role.value})"
        )


def test_state_filter_matches_the_full_list(
    quern: QuernClient, environment: Environment, authenticated: None
) -> None:
    """`state=booted` must agree with the states in the unfiltered list.

    Worth its own test because "which devices are booted" is the question the
    device fixtures answer, and getting it wrong wastes a boot or targets a
    shut-down device.
    """
    expected = {d.udid for d in environment.devices if d.state == "booted"}
    body = quern.json_ok(
        "GET", "/api/v1/device/list", params={"state": "booted"}, timeout=90.0
    )
    got = {d.get("udid") for d in body.get("devices") or []}
    assert got == expected, (
        f"state=booted returned {sorted(got)}, unfiltered list says {sorted(expected)}"
    )


def test_unauthorized_devices_are_not_offered_as_usable(
    environment: Environment,
) -> None:
    """An Android device at an unaccepted debugging prompt must not be picked.

    It lists, it looks connected, and every operation against it fails. Picking
    one turns a single unaccepted dialog into a whole tier of red.
    """
    for role in Role:
        for device in environment.role(role).devices:
            assert device.state != "unauthorized", (
                f"{device.describe()} is unauthorized but was offered for "
                f"{role.value}"
            )
