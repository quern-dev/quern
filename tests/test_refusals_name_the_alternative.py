"""A refusal that does not say what to do instead is barely better than a crash.

#299 turned eleven operations from silently mis-dispatching to `simctl` with
an adb serial into honest refusals. Honest is not the same as useful: the
first version said "quern has no Android equivalent for this operation" about
every one of them, and that was false for eight. `run-as <pkg> cat
shared_prefs/<name>.xml` reads an app's preferences on an unrooted phone
today -- measured on a release-keys Pixel 3 XL -- so what those operations
lack is an implementation, not a mechanism.

Three situations, and conflating them costs the caller the only thing the
refusal was for:

- no equivalent exists (`Set hardware keyboard`)
- one exists and quern has not built it (the plist family, save/restore: #314)
- one exists with different semantics (`Erase` and `-wipe-data`)

See #263.
"""

from __future__ import annotations

import pytest

from server.device.controller import DeviceController
from server.models import DeviceError, DeviceType


def _android(udid="PHONE"):
    c = DeviceController()
    c._device_type_cache[udid] = DeviceType.ANDROID_DEVICE
    return c


def _refusal(ctrl, operation, udid="PHONE"):
    with pytest.raises(DeviceError) as e:
        ctrl._require_simulator(udid, operation)
    return str(e.value)


class TestTheRefusalNamesTheAlternative:
    @pytest.mark.parametrize("operation,expected", [
        ("read_app_plist", "shared_prefs"),
        ("set_app_plist_value", "SharedPreferences"),
        ("set_app_plist_values", "SharedPreferences"),
        ("delete_app_plist_key", "SharedPreferences"),
        ("diff_app_plist", "SharedPreferences"),
        ("start_plist_watch", "stat"),
        ("save_app_state", "tar cf -"),
        ("restore_app_state", "tar xf -"),
    ])
    def test_an_operation_with_an_equivalent_points_at_it(self, operation, expected):
        msg = _refusal(_android(), operation)
        assert expected in msg
        assert "#314" in msg

    def test_erase_names_its_different_semantics(self):
        """Not "no equivalent" and not "coming soon": `-wipe-data` exists and
        restarts the AVD rather than wiping in place, which is a difference a
        caller needs to know before asking for it."""
        msg = _refusal(_android(), "Erase")
        assert "-wipe-data" in msg
        assert "restarts it" in msg

    def test_an_operation_with_no_equivalent_says_so_plainly(self):
        """The negative control. Without it, a map that claimed an
        alternative for everything would satisfy every test above."""
        msg = _refusal(_android(), "Set hardware keyboard")
        assert "no Android equivalent" in msg
        assert "#314" not in msg

    def test_an_unmapped_operation_does_not_invent_one(self):
        msg = _refusal(_android(), "Some future operation")
        assert "no Android equivalent" in msg


class TestTheRefusalStillCarriesWhatTheApiDependsOn:
    @pytest.mark.parametrize("operation", [
        "read_app_plist", "Erase", "Set hardware keyboard",
    ])
    def test_the_phrase_that_maps_to_400_survives(self, operation):
        """`_handle_device_error` matches this literal string to return 400.
        Appending detail is safe; rewording the leading clause would turn
        every refusal into a 500 with nothing to indicate it had happened."""
        assert "only supported on simulators" in _refusal(_android(), operation)

    @pytest.mark.parametrize("module", ["server.api.device", "server.api.app_state"])
    def test_every_copy_of_the_mapper_agrees_on_the_status(self, module):
        """Asserting the phrase is in the string tests the wrong thing: it
        says nothing about what the API returns, and both are needed for a
        400 to reach the caller.

        There are two `_handle_device_error` functions. `server/api/app_state`
        carries its own and never gained this rule, so the identical refusal
        was a 400 from one route and a **500** from another -- a client error
        reported as a server fault, decided by which file the route happened
        to live in. A live call against an attached Pixel 3 XL returned 500
        while this file's earlier string assertion passed.
        """
        import importlib

        from server.models import DeviceError

        handler = importlib.import_module(module)._handle_device_error
        err = DeviceError(
            "read_app_plist is only supported on simulators. X is Android.",
            tool="simctl",
        )
        assert handler(err).status_code == 400

    def test_non_android_devices_are_unaffected(self):
        c = DeviceController()
        c._device_type_cache["IOS"] = DeviceType.DEVICE
        msg = _refusal(c, "read_app_plist", udid="IOS")
        assert "physical iOS device" in msg
        assert "#314" not in msg

    def test_an_unknown_device_still_says_it_is_unknown(self):
        assert "does not recognise" in _refusal(DeviceController(), "Erase", udid="?")


class TestTheMapDoesNotDriftFromTheCode:
    """A table of advice keyed by string goes stale silently: rename the
    operation and the entry simply stops matching, with the refusal quietly
    falling back to "no equivalent" -- which is the false claim this replaced."""

    def test_every_mapped_operation_is_one_that_is_actually_guarded(self):
        import pathlib
        import re

        guarded = set()
        for path in pathlib.Path("server").rglob("*.py"):
            for m in re.finditer(r'_require_simulator\(\s*[^,]+,\s*"([^"]+)"',
                                 path.read_text()):
                guarded.add(m.group(1))

        mapped = set(DeviceController._ANDROID_ALTERNATIVE)
        assert mapped <= guarded, (
            f"advice for operations that no longer pass through the guard: "
            f"{sorted(mapped - guarded)}"
        )
