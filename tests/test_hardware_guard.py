"""The guard that stops tests reading the developer's machine, tested.

`_no_real_subprocess_spawns` in conftest is a safety net, and the stub beside
it means nothing normally touches the net -- so deleting the guard entirely
left the whole suite green. Measured: two mutations, "stop blocking device
tools" and "forget adb and emulator", both survived 188 tests.

That is the failure this repo keeps finding, aimed at a guard instead of a
feature: a check nothing exercises is decoration. These run pytest inside
pytest so the teardown assertion can be observed failing rather than failing
the observer.

See #272.
"""

from __future__ import annotations

import os

import pytest

pytest_plugins = ["pytester"]

#: Enough of a test session to reach our conftest, which is what is under test.
_PREAMBLE = """
import asyncio
import pytest

pytest_plugins = []
"""


def _spawning(program: str, *args: str) -> str:
    call = ", ".join(repr(a) for a in (program, *args))
    return _PREAMBLE + f"""
async def test_reaches_for_hardware():
    await asyncio.create_subprocess_exec({call})
"""


#: Every tool these tests name. A sub-session runs with fakes for all of them
#: ahead of the real ones on PATH.
_FAKED = (
    "xcrun", "adb", "emulator", "idb", "ideviceinstaller",
    "ios-webkit-debug-proxy", "ios_webkit_debug_proxy",
    "xcode-select", "xcodebuild", "swiftc", "simctl", "pymobiledevice3",
)


@pytest.fixture
def isolated(pytester, request, monkeypatch):
    """A sub-session that loads the real conftest and nothing else of ours.

    **With fake device tools ahead of the real ones on PATH**, because these
    tests are the one place that deliberately tries to spawn them. While the
    guard works the attempt never reaches `exec`; the moment it stops
    recognising a command -- which is exactly the mutation these tests exist
    to catch -- the bare name resolves against the developer's machine and
    the test that proves the guard is broken is the one that reads the
    hardware. It has happened here: mutating `_DEVICE_TOOLS` to drop six
    tools ran the real `xcode-select`, `xcodebuild` and `swiftc`.

    `ios-webkit-debug-proxy` is the worst of them -- it starts a server and
    does not exit -- so the sub-sessions are bounded by a timeout as well.
    The fakes exit 0 immediately and print nothing.
    """
    root = request.config.rootpath
    fake_bin = pytester.path / "fake-bin"
    fake_bin.mkdir()
    for tool in _FAKED:
        stub = fake_bin / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    pytester.makefile(".ini", pytest=(
        "[pytest]\nasyncio_mode = auto\n"
        "markers =\n"
        "    real_device_tools: exempt\n"
        "    device_discovery: exempt\n"
    ))
    (pytester.path / "conftest.py").write_text(
        (root / "tests" / "conftest.py").read_text(),
    )
    return pytester


@pytest.mark.parametrize("cmd", [
    ("xcrun", "simctl", "list", "devices"),
    ("adb", "devices", "-l"),
    ("emulator", "-list-avds"),
    ("idb", "list-targets"),
    # Added with the tools themselves: deleting all six from `_DEVICE_TOOLS`
    # left the whole suite green, so the set had six entries nothing pinned.
    ("ideviceinstaller", "-l"),
    ("ios-webkit-debug-proxy", "-c", "null:9221"),
    ("ios_webkit_debug_proxy", "-c", "null:9221"),
    ("xcode-select", "-p"),
    ("xcodebuild", "-version"),
    ("swiftc", "--version"),
])
def test_a_device_tool_spawn_fails_the_test(isolated, cmd):
    """Every tool in `_DEVICE_TOOLS`, not just the one someone remembered.

    Parametrised because the mutation that deleted `adb` and `emulator` from
    the set survived: a single-command test would not have noticed either.
    """
    isolated.makepyfile(_spawning(*cmd))

    result = isolated.runpytest_subprocess(timeout=60)

    # Failed *and* errored: the guard raises where the spawn happens, which
    # fails the body, and the teardown assertion reports it again so a test
    # that swallows the exception cannot hide it.
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*asked the developer's actual machine*"])


def test_the_marker_lets_a_test_through(isolated):
    """The escape hatch has to work, or it is not an escape hatch -- and the
    next person deletes the guard instead of marking their test.

    It spawns `adb`, a tool the guard recognises. With `/usr/bin/true` this
    passed whether or not the marker did anything, since the guard never had
    an opinion about it -- a test that could not fail, checking the one
    feature whose absence is silent. Safe now only because `isolated` puts a
    fake `adb` first on PATH."""
    isolated.makepyfile(_PREAMBLE + """
@pytest.mark.real_device_tools
async def test_allowed():
    proc = await asyncio.create_subprocess_exec(
        "adb", "devices", stdout=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=1, errors=0)


def test_an_unrelated_command_is_not_blocked(isolated):
    """The guard names device tools. Blocking everything would be a different
    and much more annoying bug."""
    isolated.makepyfile(_PREAMBLE + """
async def test_ordinary_subprocess():
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/true", stdout=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=1, errors=0)


# ---------------------------------------------------------------------------
# The other half: `_no_hardware_attached`, which stubs enumeration so the
# spawn guard above has nothing to catch. Neutering it left this file's six
# tests passing -- the full suite caught it, but the cheap signal did not, and
# the cheap signal is the one anyone runs while editing the fixture.
# ---------------------------------------------------------------------------


def test_enumeration_answers_empty_without_asking_the_machine(isolated):
    """The stub half. Against a neutered `_no_hardware_attached` this reaches
    a real `xcrun`, and the spawn guard beside it turns that into a failure --
    which is the point: the two fixtures test each other."""
    isolated.makepyfile(_PREAMBLE + """
from server.device.simctl import SimctlBackend
from server.device.adb import AdbBackend

async def test_backends_find_nothing():
    assert await SimctlBackend().list_devices() == []
    assert await AdbBackend().list_devices() == []
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=1, errors=0)


def test_the_availability_stub_answers_for_the_object_the_controller_asks(isolated):
    """`SimBridgeManager`, not `SimBridgeBackend`.

    The backend has no `is_available` at all; the controller builds both and
    asks the manager. Patching the backend created the attribute on a class
    nobody consults and left six real `xcode-select` spawns per run -- a no-op
    inside the fixture written to prevent no-ops.

    Asserting the return value would pass for the wrong reason on a machine
    that has Xcode, since the real probe answers True there too. So this
    asserts nothing was spawned, which is the property actually wanted.
    """
    isolated.makepyfile(_PREAMBLE + """
import asyncio
from server.device.sim_bridge import SimBridgeManager

async def test_availability_costs_no_subprocess():
    spawned = []
    real = asyncio.create_subprocess_exec

    async def _record(program, *args, **kwargs):
        spawned.append(str(program))
        return await real(program, *args, **kwargs)

    asyncio.create_subprocess_exec = _record
    try:
        await SimBridgeManager().is_available()
    finally:
        asyncio.create_subprocess_exec = real
    assert spawned == []
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=1, errors=0)


# ---------------------------------------------------------------------------
# Seams the first version of this guard did not cover. Each was found by
# review, and each is a way the check reported clean on a command that ran.
# ---------------------------------------------------------------------------


def test_a_synchronous_spawn_is_blocked_too(isolated):
    """`server/main.py` reaches for `subprocess.run(["xcrun", "simctl",
    "help"])` and `setup.py` for `xcodebuild -version`. Guarding only the
    async seam covered the device backends and claimed the rest."""
    isolated.makepyfile(_PREAMBLE + """
import subprocess

def test_sync():
    subprocess.run(["xcrun", "--find", "simctl"], capture_output=True)
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=0, failed=1, errors=1)


def test_a_tool_behind_sh_c_is_blocked(isolated):
    """`SimctlBackend._run_shell` spawns `sh -c "xcrun simctl listapps | plutil"`.
    argv[0] is `sh`, so the name check saw nothing -- the one seam that voids
    the guard entirely, in the class it is named after."""
    isolated.makepyfile(_PREAMBLE + """
async def test_shell():
    await asyncio.create_subprocess_exec("sh", "-c", "xcrun simctl list | cat")
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=0, failed=1, errors=1)


def test_a_binary_the_test_built_is_not_the_machine(isolated):
    """The exemption, and it has to exist: `test_tool_probe.py` writes a fake
    `idb_companion` into `tmp_path` and execs it to check the probe reports a
    broken binary. That matches on basename while being the opposite of
    reading the developer's desk -- the binary is fabricated on purpose."""
    isolated.makepyfile(_PREAMBLE + """
import subprocess

def test_fixture_binary(tmp_path):
    fake = tmp_path / "idb_companion"
    fake.write_text("#!/bin/sh\\nexit 0\\n")
    fake.chmod(0o755)
    assert subprocess.run([str(fake)], capture_output=True).returncode == 0
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=1, errors=0)


def test_a_shell_string_command_is_blocked(isolated):
    """`subprocess.run("adb devices", shell=True)` is a script, not a program
    name. Testing the whole string as argv[0] never matches -- the same seam
    the `sh -c` fix closed on the async side, and it read the real attached
    phones with the guard reporting clean."""
    isolated.makepyfile(_PREAMBLE + """
import subprocess

def test_shell_true():
    subprocess.run("adb devices", shell=True, capture_output=True)
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=0, failed=1, errors=1)
    result.stdout.fnmatch_lines(["*asked the developer's actual machine*"])


def test_the_exemption_follows_pytests_own_basetemp(isolated, monkeypatch):
    """`--basetemp` moves `tmp_path` out of `tempfile.gettempdir()`. Deriving
    the exemption from the temp root alone silently disabled it there, with a
    failure signature identical to deleting the exemption and landing in
    `test_tool_probe.py`, nowhere near the guard.

    The two roots have to be genuinely disjoint for this to test anything.
    The first version pointed `--basetemp` inside pytester's own directory,
    which is itself under the temp root -- so the temp-root branch covered it
    and the test passed with the basetemp branch deleted. It could not fail.
    Pointing `TMPDIR` somewhere else entirely is what separates them.
    """
    elsewhere = isolated.path / "elsewhere"
    basetemp = isolated.path / "relocated"
    elsewhere.mkdir()
    monkeypatch.setenv("TMPDIR", str(elsewhere))
    isolated.makepyfile(_PREAMBLE + """
import os
import subprocess
import tempfile

def test_fixture_binary_under_basetemp(tmp_path):
    # The premise: this run's temp root does not contain its basetemp.
    assert not str(tmp_path).startswith(os.path.realpath(tempfile.gettempdir()))
    fake = tmp_path / "adb"
    fake.write_text("#!/bin/sh\\nexit 0\\n")
    fake.chmod(0o755)
    assert subprocess.run([str(fake)], capture_output=True).returncode == 0
""")

    result = isolated.runpytest_subprocess(f"--basetemp={basetemp}", timeout=60)

    result.assert_outcomes(passed=1, errors=0)


def test_the_sub_sessions_device_tools_are_fakes(isolated):
    """The safety property the fixture above claims, asserted rather than
    assumed. If the fakes are not first on PATH then every test in this file
    is one guard regression away from running the real thing, and nothing
    would say so -- the tests would still pass, because a working guard means
    the name is never resolved at all."""
    isolated.makepyfile(_PREAMBLE + """
import shutil

@pytest.mark.real_device_tools
def test_which_resolves_to_the_fake():
    for tool in ("adb", "xcrun", "xcode-select", "ios-webkit-debug-proxy"):
        found = shutil.which(tool)
        assert found is not None, tool
        assert "fake-bin" in found, (tool, found)
""")

    result = isolated.runpytest_subprocess(timeout=60)

    result.assert_outcomes(passed=1, errors=0)
