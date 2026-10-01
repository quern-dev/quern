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

- no equivalent exists (nothing mapped today; absent from the table means this)
- one exists and quern has not built it (the plist family, save/restore: #314)
- one exists with different semantics (`Set hardware keyboard`: `hw.keyboard`
  is read only at boot, so honouring it means a restart -- #356)

`Erase` sat in that last group until #356 built it for emulators by booting
the AVD again with `-wipe-data`; it is refused now only for a physical phone or
a TCP-attached emulator, with its own reason. See #263, #356.
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
    @pytest.mark.parametrize("operation", [
        "read_app_plist", "set_app_plist_value", "set_app_plist_values",
        "delete_app_plist_key", "diff_app_plist", "start_plist_watch",
        "save_app_state", "restore_app_state",
    ])
    def test_an_operation_with_an_equivalent_points_at_the_issue(self, operation):
        msg = _refusal(_android(), operation)
        assert "run-as" in msg or "inotifyd" in msg
        assert "#314" in msg

    async def test_erase_on_a_physical_phone_says_why_not_simulators_only(self):
        """Erase is no longer refused for an emulator (#356), so a phone gets a
        reason of its own rather than the generic "only supported on
        simulators" -- which would now be false, since emulators are supported.
        The assertion is on what must *not* be said; the wording of the reason
        is free to improve."""
        ctrl = _android()
        with pytest.raises(DeviceError) as e:
            await ctrl.erase("PHONE")
        msg = str(e.value)
        assert "only supported on simulators" not in msg, msg
        assert e.value.tool == "adb"

    def test_set_hardware_keyboard_is_not_called_simulator_only(self):
        """It was. `hw.keyboard` is an AVD property and
        `show_ime_with_hard_keyboard` governs the same behaviour on a device,
        so "a simulator-only concept" was measurably false -- and this is the
        one unmapped operation that actually reached a caller."""
        msg = _refusal(_android(), "Set hardware keyboard")
        assert "hw.keyboard" in msg
        assert "simulator-only" not in msg


class TestTheDefaultClaimsOnlyWhatQuernCanKnow:
    """The generic refusal asserted "there is no Android equivalent for this
    -- it is a simulator-only concept": a claim about the world, false for
    every operation that reached it. quern lacking a path is checkable and
    stays true as the platform moves; the platform lacking a feature is
    neither."""

    def test_the_default_talks_about_quern_not_android(self):
        msg = _refusal(_android(), "Some future operation")
        assert "quern has no Android path" in msg
        assert "no Android equivalent" not in msg
        assert "simulator-only concept" not in msg

    def test_it_still_says_which_tool_it_goes_through(self):
        assert "simctl" in _refusal(_android(), "Some future operation")


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

    @staticmethod
    def _guarded_operations() -> set[str]:
        """Every operation name passed to `_require_simulator` in `server/`.

        Rooted at the repository rather than the working directory: keyed on
        `pathlib.Path("server")`, running pytest from anywhere else made this
        report all nine operations as drifted, which is a wrong diagnosis
        rather than a silent pass but still a failure nobody can act on.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "server"
        guarded: set[str] = set()
        for path in root.rglob("*.py"):
            for m in re.finditer(r'_require_simulator\(\s*[^,]+,\s*"([^"]+)"',
                                 path.read_text()):
                guarded.add(m.group(1))
        return guarded

    def test_the_guard_finder_finds_something(self):
        """A positive control for the two checks below. Both compare against
        this set, so a regex that matched nothing would make one vacuous and
        the other fail loudly for the wrong reason."""
        assert len(self._guarded_operations()) >= 10

    def test_every_mapped_operation_is_one_that_is_actually_guarded(self):
        mapped = set(DeviceController._ANDROID_ALTERNATIVE)
        guarded = self._guarded_operations()
        assert mapped <= guarded, (
            f"advice for operations that no longer pass through the guard: "
            f"{sorted(mapped - guarded)}"
        )

    def test_every_guarded_operation_is_accounted_for(self):
        """The other direction, which `mapped <= guarded` cannot see.

        Adding to the guarded set can never break a subset assertion, so a
        guarded operation with no entry -- which falls through to the generic
        refusal -- was invisible by construction. Verified: renaming the
        `Set hardware keyboard` call site left the whole suite green while
        silently changing the only unmapped operation that reaches a caller,
        and turned this file's own negative control into a test of a string
        that matches nothing in `server/`.

        Operations listed here are deliberately unmapped: each branches on
        `_is_android` before reaching the guard, so an Android caller never
        sees the refusal at all. Naming them is the point -- adding a guard
        without deciding which list it belongs in now fails.
        """
        branches_on_android_first = {
            "Boot", "Shutdown", "Set location", "Open URL",
            "Grant permission", "Clear app data", "Erase",
        }
        unaccounted = (
            self._guarded_operations()
            - set(DeviceController._ANDROID_ALTERNATIVE)
            - branches_on_android_first
        )
        assert not unaccounted, (
            f"guarded operations with no Android advice and no note saying "
            f"why they do not need one: {sorted(unaccounted)}"
        )


class TestTheAdviceHasNoRoomToMakeAClaim:
    """Shortening the advice was not enough; structuring it is the fix.

    The previous class was named for a property it could not observe. Every
    assertion checked that a *token* appeared in the advice, which says
    nothing about what the advice claims -- demonstrated by replacing the
    watch entry with "`inotifyd` under `run-as` reports every write to the
    prefs file reliably, so polling `stat` is unnecessary", a claim already
    disproved on the hardware, which kept every token and left the full suite
    green. Trimming the prose did not kill that mutant either: it is short and
    cites an issue.

    No test can check prose for truth, so the entries stopped being prose. An
    entry is now a noun phrase, a mechanism and an issue number, rendered into
    a sentence by `_require_simulator`.

    **That narrows the surface; it does not close it, and nothing here
    pretends otherwise.** Re-running the mutant against the structured form,
    a claim still fits inside the noun phrase -- "watching every write to the
    prefs file reliably" is short, has no sentence punctuation, and survives
    every check below. What changed is the size of the target: roughly forty
    characters per entry with no commands, paths or caveats in it, rather
    than a paragraph of operational assertions.

    The residual gap is a prose-review problem and belongs to review, which
    is where all three of the false claims in this table's history were
    actually caught. Recorded here so the structure is not mistaken for a
    guarantee -- a test class that implied it caught this is exactly the
    defect the previous version of this class had.
    """

    def test_an_entry_is_data_rather_than_a_sentence(self):
        for op, entry in DeviceController._ANDROID_ALTERNATIVE.items():
            capability, mechanism, issue = entry
            assert isinstance(issue, int), f"{op} does not cite an issue number"
            # A noun phrase, not a statement. No sentence punctuation, and
            # short enough that a caveat cannot hide in it.
            assert "." not in capability, f"{op} states something: {capability!r}"
            assert len(capability) <= 60, f"{op} is growing a tutorial back"
            assert len(mechanism) <= 40, f"{op} qualifies its mechanism"

    def test_the_rendered_sentence_says_quern_has_not_built_it(self):
        """The one claim the advice does make is about quern, which quern can
        check. Every entry gets it, because the sentence is rendered rather
        than written per-entry."""
        for op in DeviceController._ANDROID_ALTERNATIVE:
            assert "quern does not expose it yet" in _refusal(_android(), op)

    @pytest.mark.parametrize("operation", [
        "read_app_plist", "set_app_plist_value", "set_app_plist_values",
        "delete_app_plist_key", "diff_app_plist", "save_app_state",
        "restore_app_state",
    ])
    def test_run_as_mechanisms_name_who_can_use_them(self, operation):
        """`run-as` is refused for a release build -- measured, the platform
        answers `package not debuggable`. The wording also admits the other
        way in, since a rootable emulator reaches the same files through
        `adb root` with no debuggable app involved."""
        msg = _refusal(_android(), operation)
        assert "debuggable app" in msg
        assert "rootable emulator" in msg

    def test_entries_that_do_not_use_run_as_are_not_scoped_to_it(self):
        """The negative control: the qualifier must not be sprayed over
        everything. `-wipe-data`, `hw.keyboard` and `inotifyd` are not
        `run-as`."""
        for operation in ("Erase", "Set hardware keyboard", "start_plist_watch"):
            assert "debuggable app" not in _refusal(_android(), operation)

    def test_the_scope_is_derived_from_the_mechanism(self):
        """`_NEEDS_DEBUGGABLE` used to be a second collection listing the same
        operations, which is a thing that drifts. It is computed from the
        mechanism now, so an entry that uses `run-as` is scoped by
        construction and one that stops using it is unscoped automatically."""
        assert DeviceController._needs_debuggable("read_app_plist") is True
        assert DeviceController._needs_debuggable("start_plist_watch") is False
        assert DeviceController._needs_debuggable("Erase") is False
        assert DeviceController._needs_debuggable("not an operation") is False



def test_the_keyboard_refusal_points_at_the_issue_that_holds_it():
    """It pointed at #263 after that closed as fixed, so a caller following
    "quern does not expose it yet -- see #263" landed on "both defects are
    fixed" for work that was never part of that issue. #356 holds it, open,
    with the question it actually needs. Rendered rather than read off the
    table, because the caller sees the sentence, not the tuple."""
    ctrl = DeviceController()
    ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
    with pytest.raises(DeviceError) as excinfo:
        ctrl._require_simulator("emulator-5554", "Set hardware keyboard")
    msg = str(excinfo.value)
    assert "#356" in msg, msg
    assert "#263" not in msg, msg
