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


@pytest.fixture
def isolated(pytester, request):
    """A sub-session that loads the real conftest and nothing else of ours."""
    root = request.config.rootpath
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
])
def test_a_device_tool_spawn_fails_the_test(isolated, cmd):
    """Every tool in `_DEVICE_TOOLS`, not just the one someone remembered.

    Parametrised because the mutation that deleted `adb` and `emulator` from
    the set survived: a single-command test would not have noticed either.
    """
    isolated.makepyfile(_spawning(*cmd))

    result = isolated.runpytest_subprocess()

    # Failed *and* errored: the guard raises where the spawn happens, which
    # fails the body, and the teardown assertion reports it again so a test
    # that swallows the exception cannot hide it.
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*asked the developer's actual machine*"])


def test_the_marker_lets_a_test_through(isolated):
    """The escape hatch has to work, or it is not an escape hatch -- and the
    next person deletes the guard instead of marking their test."""
    isolated.makepyfile(_PREAMBLE + """
@pytest.mark.real_device_tools
async def test_allowed():
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/true", stdout=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
""")

    result = isolated.runpytest_subprocess()

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

    result = isolated.runpytest_subprocess()

    result.assert_outcomes(passed=1, errors=0)
