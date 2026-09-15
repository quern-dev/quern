"""Fixtures for the live conformance suite.

Every test here talks to a real server over HTTP and, mostly, to real hardware.
That makes three things the job of this file:

1. **Discover once.** Enumerating devices costs tens of seconds; it happens a
   single time per session and every test reads the same map.
2. **Gate honestly.** A tier that cannot run is skipped with the reason
   attached. A tier that is *gated off* says so differently from one that is
   *missing*, because a release report that cannot tell those apart is worse
   than no report.
3. **Keep the default run safe.** Nothing that mutates the host machine or the
   user's Quern install runs unless explicitly asked for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator

import pytest

from tests.conformance import client as client_mod
from tests.conformance import probe as probe_mod
from tests.conformance.capabilities import Device, Environment, Role, discover

#: Opt-in gates. Absent means off; any of 1/true/yes turns a tier on.
GATE_DESTRUCTIVE = "QUERN_CONFORMANCE_DESTRUCTIVE"
GATE_PHYSICAL = "QUERN_CONFORMANCE_PHYSICAL"


def _gate_open(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "destructive: mutates the host machine or the user's Quern install; "
        f"runs only when {GATE_DESTRUCTIVE} is set",
    )
    config.addinivalue_line(
        "markers",
        "physical: needs a physical phone attached; "
        f"runs only when {GATE_PHYSICAL} is set",
    )
    config.addinivalue_line(
        "markers", "conformance: live API conformance test"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark everything under `tests/conformance/` and apply the gates.

    Applied here rather than written on each test. The `integration` marker is
    what keeps these out of the default `pytest` run (see `addopts` in
    pyproject.toml), and one forgotten decorator would put a test that boots a
    simulator into the unit suite.
    """
    here = str(config.rootpath / "tests" / "conformance")
    destructive_open = _gate_open(GATE_DESTRUCTIVE)
    physical_open = _gate_open(GATE_PHYSICAL)

    for item in items:
        if not str(item.fspath).startswith(here):
            continue
        item.add_marker(pytest.mark.integration)
        item.add_marker(pytest.mark.conformance)

        if item.get_closest_marker("destructive") and not destructive_open:
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "destructive tier is off; this test changes host state "
                        f"(set {GATE_DESTRUCTIVE}=1 to run it)"
                    )
                )
            )
        if item.get_closest_marker("physical") and not physical_open:
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "physical-device tier is off "
                        f"(set {GATE_PHYSICAL}=1 to run it)"
                    )
                )
            )


@pytest.fixture(autouse=True)
def _the_real_machine_is_not_a_fixture(request: pytest.FixtureRequest):
    """Override the root guard, which this suite would otherwise trip by design.

    The root fixture of this name fails any test that modifies a watched path on
    the real machine. That is exactly right for the unit suite, and exactly
    wrong for a `destructive`-marked test whose entire purpose is to install a
    certificate or switch the update channel.

    So: re-run the root guard verbatim for everything else -- a conformance test
    that is *not* marked destructive but rewrites `~/.zshrc` is still a bug
    worth catching -- and stand down only for the tier that declared itself.
    """
    if request.node.get_closest_marker("destructive"):
        yield
        return

    from tests.conftest import _WATCHED, _describe

    before = {path: read(path) for _, path, read in _WATCHED}
    yield
    for kind, path, read in _WATCHED:
        after = read(path)
        if after == before[path]:
            continue
        pytest.fail(
            f"this test {_describe(before[path], after)} {path} — {kind}, on the "
            "machine the suite is running on. A conformance test that changes "
            "host state belongs in the destructive tier: mark it "
            "`@pytest.mark.destructive`."
        )


# -- session-scoped discovery ------------------------------------------------


@pytest.fixture(scope="session")
def quern_target() -> client_mod.ServerTarget:
    return client_mod.resolve_target()


@pytest.fixture(scope="session")
def quern(quern_target: client_mod.ServerTarget) -> Iterator[client_mod.QuernClient]:
    """The HTTP client, pointed at whatever server this machine resolved to."""
    client = client_mod.QuernClient(quern_target)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def health(quern: client_mod.QuernClient) -> client_mod.HealthReport:
    """Pre-flight. Fails the session loudly rather than skipping every test.

    A missing server is not a capability this machine lacks -- it is the one
    precondition the suite cannot work around, and a run that reports 400 skips
    because nothing was listening has told the operator nothing.
    """
    report = client_mod.check_health(quern)
    if not report.reachable:
        pytest.fail(report.detail, pytrace=False)
    return report


@pytest.fixture(scope="session")
def environment(
    quern: client_mod.QuernClient, health: client_mod.HealthReport
) -> Environment:
    """The capability map. Computed once; every role fixture reads it."""
    return discover(quern, authenticated=health.authenticated)


@pytest.fixture(scope="session", autouse=True)
def _report_environment(request: pytest.FixtureRequest) -> None:
    """Print the environment block once, before the first test runs.

    Autouse and session-scoped so it lands in every run, including one that
    skips everything -- that is precisely the run whose environment block is
    worth reading.
    """
    try:
        env = request.getfixturevalue("environment")
    except Exception as exc:  # noqa: BLE001 - reporting must not mask the cause
        print(f"\n[conformance] environment discovery failed: {exc!r}\n")
        raise
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    text = "\n" + env.summary() + "\n"
    if reporter is not None:
        reporter.write_line(text)
    else:
        print(text)


@pytest.fixture(scope="session")
def authenticated(health: client_mod.HealthReport) -> None:
    """Skip a test that needs a key when the run has none that works."""
    if not health.authenticated:
        pytest.skip("; ".join(health.notes) or "no working API key")


# -- role fixtures -----------------------------------------------------------


def _device_for(env: Environment, role: Role) -> Device:
    avail = env.role(role)
    if not avail.available:
        # Phrased so the skip line alone is actionable. A skip that reads
        # "no android_device" makes an unaccepted USB-debugging prompt look
        # like an absence of hardware.
        pytest.skip(f"{role.value} unavailable: {avail.blocked_reason}")
    device = avail.pick()
    if device is None:  # pragma: no cover - `available` already implies one
        pytest.skip(f"{role.value} unavailable: nothing usable")
    return device


@pytest.fixture(scope="session")
def ios_simulator(environment: Environment, authenticated: None) -> Device:
    return _device_for(environment, Role.IOS_SIMULATOR)


@pytest.fixture(scope="session")
def ios_device(environment: Environment, authenticated: None) -> Device:
    return _device_for(environment, Role.IOS_DEVICE)


@pytest.fixture(scope="session")
def android_emulator(environment: Environment, authenticated: None) -> Device:
    return _device_for(environment, Role.ANDROID_EMULATOR)


@pytest.fixture(scope="session")
def android_device(environment: Environment, authenticated: None) -> Device:
    return _device_for(environment, Role.ANDROID_DEVICE)


def _any_of(env: Environment, roles: tuple[Role, ...], platform: str) -> Device:
    """Pick across several roles, preferring one that is already booted.

    Booted-ness is weighed *before* role preference, and that ordering is load
    bearing. Preferring the emulator role first picked a shut-down AVD over an
    attached, running phone, and a shut-down emulator's identifier is not an adb
    serial at all -- `avd:Medium_Phone_API_36.1` -- so every call against it
    failed with `adb: unknown host service`. Ranking by role first is how a
    device fixture hands out something that cannot be driven.
    """
    available = [r for r in roles if env.has(r)]
    if not available:
        reasons = "; ".join(env.role(r).blocked_reason for r in roles)
        pytest.skip(f"no {platform} target: {reasons}")

    for role in available:
        device = env.role(role).pick(prefer_booted=True)
        if device is not None and device.booted:
            return device
    # Nothing booted anywhere: fall back to role order and let the caller boot.
    for role in available:
        device = env.role(role).pick()
        if device is not None:
            return device
    pytest.skip(f"no usable {platform} target")  # pragma: no cover


@pytest.fixture(scope="session")
def any_ios(environment: Environment, authenticated: None) -> Device:
    """A simulator if there is one, else a physical iPhone.

    For the many endpoints that do not care which, and should be exercised on
    whatever the machine has rather than skipped for want of a simulator.
    """
    return _any_of(environment, (Role.IOS_SIMULATOR, Role.IOS_DEVICE), "iOS")


@pytest.fixture(scope="session")
def any_android(environment: Environment, authenticated: None) -> Device:
    return _any_of(
        environment, (Role.ANDROID_EMULATOR, Role.ANDROID_DEVICE), "Android"
    )


# -- host tooling ------------------------------------------------------------


@pytest.fixture(scope="session")
def host_tool():
    """Look up a host CLI, for the few checks that must bypass the server.

    Used sparingly and only to corroborate: when a test asserts that Quern
    reports a device as booted, confirming it against `simctl`/`adb` directly is
    what distinguishes a wrong answer from a stale cache. Tests that merely
    *need* a device go through the API like everything else.
    """

    def _lookup(name: str) -> str:
        path = shutil.which(name)
        if not path:
            pytest.skip(f"host tool {name!r} is not on PATH")
        return path

    return _lookup


@pytest.fixture
def run_host(host_tool):
    """Run a host command with a hard timeout, skipping if the tool is wedged."""

    def _run(argv: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
        argv = [host_tool(argv[0]), *argv[1:]]
        try:
            return subprocess.run(  # noqa: S603 - argv is built from a which() lookup
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            pytest.skip(
                f"host command {' '.join(argv[:2])} did not return in {timeout:.0f}s"
            )

    return _run


# -- proxy -------------------------------------------------------------------


@pytest.fixture(scope="session")
def proxy_status(quern: client_mod.QuernClient, authenticated: None) -> dict:
    """Current proxy state, read once.

    The mock, intercept and bypass endpoints all answer 503 when the proxy is
    not running (`_require_running_proxy`), so tests for them skip rather than
    fail on a machine where the user has the proxy stopped. That is a
    configuration, not a defect.
    """
    resp = quern.get("/api/v1/proxy/status", timeout=20.0)
    if not resp.is_success:
        pytest.skip(f"/proxy/status returned {resp.status_code}")
    return resp.json()


@pytest.fixture(scope="session")
def proxy_running(proxy_status: dict) -> dict:
    if proxy_status.get("status") != "running":
        pytest.skip(
            f"proxy is {proxy_status.get('status')!r}; start it with "
            "`quern start` or POST /api/v1/proxy/start"
        )
    return proxy_status


@pytest.fixture
def mock_sandbox(quern: client_mod.QuernClient, proxy_running: dict):
    """Create mock rules and guarantee they are removed again.

    Removes only what it created. A blanket `DELETE /proxy/mocks` on teardown
    would be simpler and would also delete the rules the developer set up by
    hand before running the suite -- a test suite that quietly discards the
    user's working state is not one anybody runs twice.
    """
    created: list[str] = []

    def _create(pattern: str, **response: object) -> str:
        payload: dict = {"pattern": pattern}
        payload.update(response)
        body = quern.json_ok("POST", "/api/v1/proxy/mocks", json=payload, timeout=20.0)
        rule_id = body.get("rule_id")
        assert rule_id, f"POST /proxy/mocks returned no rule_id: {body}"
        created.append(rule_id)
        return rule_id

    _create.created = created  # type: ignore[attr-defined]
    try:
        yield _create
    finally:
        for rule_id in created:
            try:
                quern.delete(f"/api/v1/proxy/mocks/{rule_id}", timeout=20.0)
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


@pytest.fixture
def bypass_sandbox(quern: client_mod.QuernClient, proxy_running: dict):
    """Add bypass patterns and restore the original list afterwards.

    `clear_bypass` with no argument empties the whole list, so teardown restores
    by difference rather than by clearing: the user's own bypass entries survive
    the run.
    """
    before = set(
        quern.json_ok("GET", "/api/v1/proxy/bypass", timeout=20.0).get("patterns") or []
    )

    def _add(*patterns: str) -> list[str]:
        body = quern.json_ok(
            "POST", "/api/v1/proxy/bypass", json={"patterns": list(patterns)},
            timeout=20.0,
        )
        return body.get("patterns") or []

    try:
        yield _add
    finally:
        try:
            after = set(
                quern.json_ok("GET", "/api/v1/proxy/bypass", timeout=20.0).get(
                    "patterns"
                )
                or []
            )
            added = after - before
            if added:
                quern.delete(
                    "/api/v1/proxy/bypass",
                    params={"patterns": ",".join(sorted(added))},
                    timeout=20.0,
                )
        except Exception:  # noqa: BLE001
            pass


# -- the QuernProbe fixture app ----------------------------------------------


def _install_and_launch(
    client: client_mod.QuernClient, udid: str, artifact, contract
):
    """Install the built artifact and bring the app to the foreground."""
    client.json_ok(
        "POST", "/api/v1/device/app/install",
        json={"udid": udid, "app_path": str(artifact)}, timeout=300.0,
    )
    client.json_ok(
        "POST", "/api/v1/device/app/launch",
        json={"udid": udid, "bundle_id": probe_mod.BUNDLE_ID}, timeout=180.0,
    )
    driver = probe_mod.ProbeDriver(client, udid, contract)
    driver.wait_until_ready()
    return driver


@pytest.fixture(scope="session")
def ios_probe(quern: client_mod.QuernClient, ios_simulator: Device):
    """QuernProbe built, installed and running on an iOS simulator.

    Session-scoped: the build is the expensive part and the app is stateless
    between tests in every way this suite depends on. Tests that need a
    particular tab call `goto` themselves rather than assuming where the last
    one left it -- ordering assumptions between tests are how a suite becomes
    unable to run a single test on its own.
    """
    if not ios_simulator.booted:
        quern.json_ok(
            "POST", "/api/v1/device/boot",
            json={"udid": ios_simulator.udid}, timeout=300.0,
        )

    try:
        bundle = probe_mod.build_ios()
    except probe_mod.ProbeUnavailable as exc:
        pytest.skip(f"iOS probe app unavailable: {exc}")

    return _install_and_launch(quern, ios_simulator.udid, bundle, probe_mod.IOS)


@pytest.fixture(scope="session")
def android_probe(quern: client_mod.QuernClient, any_android: Device):
    """QuernProbe on whichever Android target this machine has.

    Takes `any_android` rather than the emulator specifically: the Android half
    of the fixture exists because of a bug that only shows on a real device
    (#78, `am start` exiting 0 for an unresolvable intent), so preferring one
    over the other would be arbitrary.
    """
    try:
        apk = probe_mod.build_android()
    except probe_mod.ProbeUnavailable as exc:
        pytest.skip(f"Android probe app unavailable: {exc}")

    return _install_and_launch(quern, any_android.udid, apk, probe_mod.ANDROID)


@pytest.fixture
def probe_id(request: pytest.FixtureRequest):
    """Resolve a logical element name against the driver in use, or skip.

    A surface that exists on one platform and not the other is a fact about the
    apps, not a failure. `Ids.SEGMENT` on Android skips with a reason; it does
    not quietly pass, and it does not fail.
    """

    def _resolve(driver, logical: str) -> str:
        identifier = driver.contract.id_for(logical)
        if identifier is None:
            pytest.skip(
                f"{logical!r} has no counterpart in the "
                f"{driver.contract.platform} probe app"
            )
        return identifier

    return _resolve


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run every `probe`-based test against each platform the machine has.

    Parametrised here rather than written as two modules. The whole point of the
    identifier contract is that one test body covers both apps; duplicating the
    module per platform would let the two copies drift, which is the thing the
    contract exists to prevent.
    """
    if "probe" in metafunc.fixturenames:
        metafunc.parametrize(
            "probe", ["ios", "android"], indirect=True, scope="session"
        )


@pytest.fixture(scope="session")
def probe(request: pytest.FixtureRequest):
    """Whichever platform's probe app this parametrisation asked for.

    Skips — rather than fails — when that platform is not present, so a
    Mac with no Android device runs the iOS half and says the other was absent.
    """
    return request.getfixturevalue(f"{request.param}_probe")


@pytest.fixture
def fresh_probe(probe):
    """A probe app restored to its launch state before the test runs.

    Function-scoped and therefore not free -- a relaunch costs a second or two
    -- so it is requested only by tests that genuinely need empty fields rather
    than applied to the whole module.
    """
    probe.relaunch()
    return probe
