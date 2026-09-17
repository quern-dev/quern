"""Tests for SimBridgeBackend — mock SimBridgeManager.send."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.sim_bridge import (
    SIMULATOR_KIT_RELATIVE_PATHS,
    SimBridgeBackend,
    SimBridgeManager,
    find_simulator_kit,
)


def _backend_with_send(send_impl):
    """Build a SimBridgeBackend whose underlying manager has a mocked send."""
    mgr = SimBridgeManager()
    mgr.send = AsyncMock(side_effect=send_impl)  # type: ignore[method-assign]
    return SimBridgeBackend(mgr), mgr


# ---------------------------------------------------------------------------
# describe_point
# ---------------------------------------------------------------------------


class TestDescribePoint:
    async def test_returns_hit_element(self):
        async def send(cmd):
            assert cmd["cmd"] == "probe-point"
            assert cmd["x"] == 100.0
            assert cmd["y"] == 200.0
            return {
                "ok": True,
                "tree": [{"type": "Button", "AXLabel": "Tap"}],
            }

        backend, _ = _backend_with_send(send)
        element = await backend.describe_point("X", 100, 200)
        assert element == {"type": "Button", "AXLabel": "Tap"}

    async def test_miss_returns_none(self):
        async def send(cmd):
            return {"ok": False, "error": "probe-point returned nil"}

        backend, _ = _backend_with_send(send)
        element = await backend.describe_point("X", 100, 200)
        assert element is None

    async def test_dict_tree_unwrapped(self):
        async def send(cmd):
            return {"ok": True, "tree": {"type": "Button", "AXLabel": "Tap"}}

        backend, _ = _backend_with_send(send)
        element = await backend.describe_point("X", 100, 200)
        assert element == {"type": "Button", "AXLabel": "Tap"}


# ---------------------------------------------------------------------------
# describe_all probing integration
# ---------------------------------------------------------------------------


class TestDescribeAllWithProbing:
    async def test_probes_empty_tab_bar(self):
        """describe_all should probe a childless tab bar and merge its hits."""
        tab_button = {
            "type": "RadioButton",
            "AXLabel": "Timelines",
            "frame": {"x": 0, "y": 770, "width": 80, "height": 48},
        }

        async def send(cmd):
            if cmd["cmd"] == "describe-ui":
                # Nested tree with an empty tab bar group
                return {
                    "ok": True,
                    "tree": [
                        {
                            "type": "Application",
                            "AXLabel": "App",
                            "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
                            "children": [
                                {
                                    "type": "Group",
                                    "AXLabel": "Tab Bar",
                                    "role_description": "group",
                                    "frame": {"x": 0, "y": 769, "width": 393, "height": 83},
                                    "children": [],
                                }
                            ],
                        }
                    ],
                }
            if cmd["cmd"] == "probe-point":
                # Every grid hit returns the single tab button
                return {"ok": True, "tree": [tab_button]}
            raise AssertionError(f"unexpected cmd: {cmd}")

        backend, _ = _backend_with_send(send)
        result = await backend.describe_all("X")

        labels = [item.get("AXLabel") for item in result]
        assert "App" in labels
        assert "Tab Bar" in labels
        assert "Timelines" in labels
        # Tab Bar's children key was popped during flatten
        for item in result:
            assert "children" not in item

    async def test_no_probing_when_all_full(self):
        """No probe-point calls when every container has enumerated children."""

        async def send(cmd):
            assert cmd["cmd"] != "probe-point", "should not probe — no empty containers"
            return {
                "ok": True,
                "tree": [
                    {
                        "type": "Application",
                        "AXLabel": "App",
                        "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
                        "children": [
                            {
                                "type": "Button",
                                "AXLabel": "Hello",
                                "frame": {"x": 0, "y": 0, "width": 50, "height": 50},
                            }
                        ],
                    }
                ],
            }

        backend, _ = _backend_with_send(send)
        result = await backend.describe_all("X")
        assert len(result) == 2

    async def test_dedup_against_existing(self):
        """Probed elements with same frame as something already in the flat list are skipped."""
        existing_button = {
            "type": "Button",
            "AXLabel": "Existing",
            "frame": {"x": 50, "y": 770, "width": 80, "height": 48},
        }
        # Same frame as existing_button → should be deduped out.
        duplicate = {
            "type": "RadioButton",
            "AXLabel": "Duplicate",
            "frame": {"x": 50, "y": 770, "width": 80, "height": 48},
        }

        async def send(cmd):
            if cmd["cmd"] == "describe-ui":
                return {
                    "ok": True,
                    "tree": [
                        {
                            "type": "Application",
                            "AXLabel": "App",
                            "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
                            "children": [
                                {
                                    "type": "Group",
                                    "AXLabel": "Tab Bar",
                                    "frame": {"x": 0, "y": 769, "width": 393, "height": 83},
                                    "children": [],
                                },
                                existing_button,
                            ],
                        }
                    ],
                }
            if cmd["cmd"] == "probe-point":
                return {"ok": True, "tree": [duplicate]}
            raise AssertionError(f"unexpected cmd: {cmd}")

        backend, _ = _backend_with_send(send)
        result = await backend.describe_all("X")
        labels = [item.get("AXLabel") for item in result]
        assert labels.count("Existing") == 1
        assert "Duplicate" not in labels


# ---------------------------------------------------------------------------
# describe_all_nested — no probing
# ---------------------------------------------------------------------------


class TestDescribeAllNested:
    async def test_returns_nested_without_probing(self):
        async def send(cmd):
            assert cmd["cmd"] == "describe-ui"
            assert cmd["nested"] is True
            return {
                "ok": True,
                "tree": {
                    "type": "Application",
                    "AXLabel": "App",
                    "children": [{"type": "Button", "AXLabel": "Hi"}],
                },
            }

        backend, mgr = _backend_with_send(send)
        result = await backend.describe_all_nested("X")
        # Single dict tree gets wrapped in a list
        assert len(result) == 1
        assert result[0]["AXLabel"] == "App"
        assert result[0]["children"][0]["AXLabel"] == "Hi"
        # Only one call — no probing path
        assert mgr.send.await_count == 1


class TestSimulatorKitDiscovery:
    """Xcode 27 moved SimulatorKit out of the developer directory.

    Before this, `is_available()` looked only under
    `<dev>/Library/PrivateFrameworks`, so on Xcode 27 it reported the backend
    unavailable while the framework sat one level up in `Contents/SharedFrameworks`.
    Every simulator HID call — tap, type, swipe, press — failed as a result.
    """

    def _make_framework(self, root: Path, relative: str) -> Path:
        """Create a stand-in framework directory and return it."""
        path = Path(os.path.normpath(root / relative))
        path.mkdir(parents=True)
        return path

    def test_finds_the_pre_27_layout(self, tmp_path):
        dev = tmp_path / "Xcode.app" / "Contents" / "Developer"
        dev.mkdir(parents=True)
        expected = self._make_framework(
            dev, "Library/PrivateFrameworks/SimulatorKit.framework"
        )
        assert find_simulator_kit(dev) == expected

    def test_finds_the_xcode_27_layout(self, tmp_path):
        """The regression case: a sibling of Developer, not a child."""
        dev = tmp_path / "Xcode.app" / "Contents" / "Developer"
        dev.mkdir(parents=True)
        expected = self._make_framework(
            dev, "../SharedFrameworks/SimulatorKit.framework"
        )
        assert find_simulator_kit(dev) == expected
        # Spelled out, because the whole bug is that this is *outside* dev.
        assert "SharedFrameworks" in str(expected)
        assert "Developer" not in expected.name

    def test_returns_none_when_neither_layout_is_present(self, tmp_path):
        """Distinguishable from "found it": None, not a path that may not exist.

        `is_available()` turns this into the decision to offer the backend at
        all, so a truthy answer here advertises a backend that cannot load.
        """
        dev = tmp_path / "Xcode.app" / "Contents" / "Developer"
        dev.mkdir(parents=True)
        assert find_simulator_kit(dev) is None

    def test_a_missing_developer_directory_is_not_an_error(self, tmp_path):
        """A machine with no Xcode must answer None rather than raise.

        `is_available()` is called from `check_tools()`, which backs `/tools`
        and `quern doctor`; an exception here takes the whole health report
        down over an absence that is entirely normal.
        """
        assert find_simulator_kit(tmp_path / "nonexistent") is None

    def test_prefers_the_legacy_layout_when_both_exist(self, tmp_path):
        """Deterministic on a machine carrying both.

        Not a configuration anyone plans, but beta Xcodes have shipped
        transitional layouts before, and "whichever the filesystem lists first"
        is not an answer that reproduces.
        """
        dev = tmp_path / "Xcode.app" / "Contents" / "Developer"
        dev.mkdir(parents=True)
        legacy = self._make_framework(
            dev, "Library/PrivateFrameworks/SimulatorKit.framework"
        )
        self._make_framework(dev, "../SharedFrameworks/SimulatorKit.framework")
        assert find_simulator_kit(dev) == legacy

    def test_the_swift_side_checks_the_same_two_layouts(self):
        """The Python and Swift halves must not drift apart.

        Python decides whether to *offer* the backend; Swift decides what to
        `dlopen`. If one learns about a new layout and the other does not, the
        server either advertises a backend that cannot load or refuses one that
        works — and both failures look like this bug did.
        """
        source = Path(__file__).resolve().parents[1] / "tools" / "sim-bridge.swift"
        assert source.exists(), (
            f"{source} is missing, so this guard cannot check anything -- fail "
            "rather than error, so the reason is the drift message"
        )

        # Comments stripped first. The paths appear in prose there as well as in
        # the constant, and matching either meant the guard passed on a doc
        # comment: reducing the Swift constant to the new path only -- dropping
        # Xcode 26 support in the half that actually dlopens -- left the suite
        # green, because the legacy path was still mentioned two lines above in
        # a comment.
        code = "\n".join(
            line for line in source.read_text().splitlines()
            if not line.lstrip().startswith("//")
        )

        for relative in SIMULATOR_KIT_RELATIVE_PATHS:
            # The Swift constant names the binary inside the bundle; the Python
            # one names the bundle. Compare the part they share.
            assert relative.replace(".framework", "") in code, (
                f"tools/sim-bridge.swift does not look for {relative!r}; the "
                "Swift and Python halves of SimulatorKit discovery have drifted"
            )


class TestIsAvailableResolvesTheSamePath:
    """The helper is not the bug site.

    Every other test here exercises `find_simulator_kit`, which is pure. The
    function that actually reported `sim_bridge: false` on Xcode 27 is
    `SimBridgeManager.is_available()`, and reverting *its* call site to the
    hardcoded legacy path left the whole suite green — the fix was pinned
    everywhere except where it was made.

    Driven through `DEVELOPER_DIR`, which `xcode-select -p` honours, so the
    real Xcode layout on the machine running this is irrelevant: CI without
    Xcode and a developer box with either layout all behave the same.
    """

    def _fake_xcode(self, tmp_path, layout: str) -> pathlib.Path:
        """Build a throwaway Xcode.app of one shape and return its dev dir."""
        contents = tmp_path / "Xcode.app" / "Contents"
        dev = contents / "Developer"
        dev.mkdir(parents=True)
        if layout == "legacy":
            target = dev / "Library" / "PrivateFrameworks" / "SimulatorKit.framework"
        elif layout == "xcode27":
            target = contents / "SharedFrameworks" / "SimulatorKit.framework"
        else:
            return dev
        target.mkdir(parents=True)
        return dev

    async def _available(self, monkeypatch, dev_dir) -> bool:
        """Answer only on the framework layout, with the host factored out.

        `is_available` returns False for three different reasons, and two of
        them are properties of whatever machine the suite runs on: no `swiftc`
        on PATH, and whatever the real `xcode-select -p` prints. Left real,
        `test_neither_layout_is_not_available` passes on any machine without
        Swift installed -- returning False at the `which` guard, never reaching
        the layout check it exists to exercise -- and the two positive tests
        fail there outright.
        """
        from server.device import sim_bridge as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/swiftc")
        monkeypatch.setattr(
            sb, "probe_stdout", AsyncMock(return_value=str(dev_dir)),
        )
        return await sb.SimBridgeManager().is_available()

    async def test_the_xcode_27_layout_is_available(self, monkeypatch, tmp_path):
        """The regression. Against the pre-fix call site this returns False."""
        dev = self._fake_xcode(tmp_path, "xcode27")
        assert await self._available(monkeypatch, dev) is True

    async def test_the_legacy_layout_is_still_available(self, monkeypatch, tmp_path):
        dev = self._fake_xcode(tmp_path, "legacy")
        assert await self._available(monkeypatch, dev) is True

    async def test_neither_layout_is_not_available(self, monkeypatch, tmp_path):
        """And the negative, so the two above cannot be satisfied by a stub
        that always answers True."""
        dev = self._fake_xcode(tmp_path, "none")
        assert await self._available(monkeypatch, dev) is False


class TestTheBinaryCacheIsKeyedOnContent:
    """An mtime check is defeated by the normal upgrade path.

    Release tarballs come from `git archive`, which stamps files with the
    *commit* time, and both `tar -xzf` and `shutil.move` preserve it — so an
    extracted source is routinely older than a binary compiled last week, and
    nothing in the update path deletes the cached binary. The modal case: a user
    on Xcode 26 with a working bridge upgrades to Xcode 27, taps break, they run
    `quern update`, and the fix never gets compiled.

    Worse than a missed rebuild, because a pre-fix binary still completes the
    readiness handshake and logs its dlopen failure only to stderr. So
    `_sim_bridge_ok` goes True and every gesture is routed to a bridge that
    cannot resolve HID — where before the fix the user got an honest
    `sim_bridge: false` and fell back to idb.
    """

    def _manager(self, monkeypatch, tmp_path, source_text: str):
        from server.device import sim_bridge as sb

        src = tmp_path / "sim-bridge.swift"
        src.write_text(source_text)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        monkeypatch.setattr(sb, "QUERN_BIN_DIR", bin_dir)
        monkeypatch.setattr(sb, "_find_source", lambda: src)
        mgr = sb.SimBridgeManager()
        mgr._binary_path = bin_dir / sb.BINARY_NAME
        mgr._stamp_path = bin_dir / f"{sb.BINARY_NAME}.sha256"
        return mgr, src

    async def test_a_source_change_rebuilds_even_when_the_binary_is_newer(
        self, monkeypatch, tmp_path
    ):
        """The exact tarball shape: binary mtime *ahead* of the source."""
        import os
        import time

        mgr, src = self._manager(monkeypatch, tmp_path, "// original")
        compiled = []

        async def fake_compile(*a, **k):
            compiled.append(True)
            mgr._binary_path.write_text("binary")

            class P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return P()

        monkeypatch.setattr(
            "server.device.sim_bridge.asyncio.create_subprocess_exec", fake_compile,
        )
        monkeypatch.setattr("server.device.sim_bridge.shutil.which", lambda _n: "/usr/bin/swiftc")

        await mgr.ensure_binary()
        assert compiled == [True], "the first build did not happen"

        # The source changes, but its mtime goes *backwards* — what `git
        # archive` produces.
        src.write_text("// the Xcode 27 fix")
        old = time.time() - 86400
        os.utime(src, (old, old))

        await mgr.ensure_binary()
        assert len(compiled) == 2, (
            "an older-but-different source was treated as up to date, so the "
            "fix would never reach a tarball user"
        )

    async def test_an_unchanged_source_does_not_rebuild(self, monkeypatch, tmp_path):
        """The cache still has to be a cache."""
        mgr, _src = self._manager(monkeypatch, tmp_path, "// same")
        compiled = []

        async def fake_compile(*a, **k):
            compiled.append(True)
            mgr._binary_path.write_text("binary")

            class P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return P()

        monkeypatch.setattr(
            "server.device.sim_bridge.asyncio.create_subprocess_exec", fake_compile,
        )
        monkeypatch.setattr("server.device.sim_bridge.shutil.which", lambda _n: "/usr/bin/swiftc")

        await mgr.ensure_binary()
        await mgr.ensure_binary()
        assert len(compiled) == 1, "it recompiled an unchanged source"

    async def test_a_binary_with_no_stamp_is_rebuilt(self, monkeypatch, tmp_path):
        """The upgrade path, and the one case with no stamp to compare against.

        Every user who compiled before stamps existed has exactly this on
        disk: a binary, and no `sim-bridge.sha256` beside it. If a missing
        stamp were treated as a hit rather than a miss, those users would keep
        the pre-fix binary indefinitely -- the dlopen failure goes to stderr,
        the readiness handshake still completes, so `_sim_bridge_ok` goes True
        and every gesture routes to a bridge that cannot resolve HID.

        The four sibling tests all leave a stamp behind, so a regression that
        reused an unstamped binary while still honouring stamped misses would
        pass all of them.
        """
        mgr, _src = self._manager(monkeypatch, tmp_path, "// pre-fix")
        compiled = []

        async def fake_compile(*a, **k):
            compiled.append(True)
            mgr._binary_path.write_text("rebuilt")

            class P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return P()

        monkeypatch.setattr(
            "server.device.sim_bridge.asyncio.create_subprocess_exec", fake_compile,
        )
        monkeypatch.setattr("server.device.sim_bridge.shutil.which", lambda _n: "/usr/bin/swiftc")

        # What the upgrade leaves: a binary, and nothing recording what built it.
        mgr._binary_path.write_text("the stale pre-fix binary")
        assert not mgr._stamp_path.exists()

        await mgr.ensure_binary()

        assert compiled == [True], (
            "an unstamped binary was reused, so every pre-stamp install keeps "
            "the binary that cannot find SimulatorKit under Xcode 27"
        )
        assert mgr._binary_path.read_text() == "rebuilt"
        assert mgr._stamp_path.exists(), "the rebuild did not record a stamp"

    async def test_a_failed_compile_does_not_record_a_stamp(self, monkeypatch, tmp_path):
        """A stamp written before the compile would mark a failed build current
        and skip the retry."""
        mgr, _src = self._manager(monkeypatch, tmp_path, "// broken")

        async def failing(*a, **k):
            class P:
                returncode = 1

                async def communicate(self):
                    return b"", b"boom"

            return P()

        monkeypatch.setattr(
            "server.device.sim_bridge.asyncio.create_subprocess_exec", failing,
        )
        monkeypatch.setattr("server.device.sim_bridge.shutil.which", lambda _n: "/usr/bin/swiftc")

        with pytest.raises(RuntimeError, match="Failed to compile"):
            await mgr.ensure_binary()
        assert not mgr._stamp_path.exists()


class TestTheTwoHalvesAgreeOnSymlinkedDeveloperDirs:
    """Python resolved `..` lexically while the Swift half follows symlinks.

    `xcode-select -s` pointing at a symlink to `Contents/Developer` is enough:
    a lexical `..` climbs out of the *link's* parent rather than the real
    `Contents/`, finds nothing, and reports sim-bridge unavailable — for an
    Xcode whose SimulatorKit the Swift binary loads without complaint. That is
    the inverse of the failure this fix exists to prevent.
    """

    def test_a_symlinked_developer_dir_still_resolves(self, tmp_path):
        from server.device.sim_bridge import find_simulator_kit

        contents = tmp_path / "Xcode.app" / "Contents"
        real_dev = contents / "Developer"
        real_dev.mkdir(parents=True)
        (contents / "SharedFrameworks" / "SimulatorKit.framework").mkdir(parents=True)

        link = tmp_path / "devlink"
        link.symlink_to(real_dev)

        found = find_simulator_kit(str(link))
        assert found is not None, (
            "a symlinked developer dir resolved to nothing, so sim-bridge would "
            "report unavailable for an Xcode that works"
        )
        assert found.name == "SimulatorKit.framework"


class TestDescribeAllProbeSkip:
    """`probe=False` is a performance escape hatch with a correctness cost.

    The probe finds hidden children of containers the static walk reports as
    childless. Skipping it is right only when the caller knows its target is in
    the static tree, so the flag has to actually reach the probing decision --
    and it has to survive the poisoned-tree retry, or a caller that opted out
    silently pays again on recovery.
    """

    def _backend(self, nested, probe_calls):
        mgr = SimBridgeManager()
        backend = SimBridgeBackend(mgr)
        backend._fetch_nested = AsyncMock(return_value=nested)  # type: ignore[method-assign]

        async def _describe_point(udid, x, y):
            probe_calls.append((x, y))
            return None

        backend.describe_point = _describe_point  # type: ignore[method-assign]
        return backend

    #: A container the static walk reports as childless, so it gets probed.
    #: Shaped to match `is_probeable_container`: a Group whose label names it a
    #: tab bar, with no enumerated children. This is the real tab-bar case, not
    #: an invented one -- an invented shape would make the default-probing test
    #: pass or fail for reasons unrelated to the flag.
    PROBEABLE = [{
        "type": "Group", "AXLabel": "Tab Bar", "children": [],
        "frame": {"x": 0, "y": 800, "width": 400, "height": 80},
    }]

    @pytest.mark.asyncio
    async def test_probing_happens_by_default(self):
        calls: list = []
        backend = self._backend(self.PROBEABLE, calls)
        await backend.describe_all("udid")
        assert calls, "the default call did not probe an empty container"

    @pytest.mark.asyncio
    async def test_probe_false_skips_the_hit_tests(self):
        calls: list = []
        backend = self._backend(self.PROBEABLE, calls)
        await backend.describe_all("udid", probe=False)
        assert calls == [], (
            f"probe=False still issued {len(calls)} describe_point call(s); "
            "this is 92% of the cost the flag exists to avoid"
        )

    @pytest.mark.asyncio
    async def test_the_static_tree_is_still_returned_when_probing_is_skipped(self):
        """Skipping the probe must not skip the answer."""
        calls: list = []
        backend = self._backend(self.PROBEABLE, calls)
        result = await backend.describe_all("udid", probe=False)
        assert len(result) == 1
        assert result[0]["type"] == "Group"


class TestCancellationIsNotSwallowed:
    """A cancelled command stays cancelled; a crashed bridge stays a failure.

    `_send_locked` sees `CancelledError` from two different sources, and they
    need different exits:

    * **this task was cancelled** — `_run_until_client_leaves` does that when a
      client disconnects. The cancellation must propagate, or the caller that
      asked for it never finds out.
    * **the future was cancelled under us** — `_stdout_reader` calls
      `_cleanup_state()` when the subprocess exits, which cancels
      `_pending_response`. Nobody cancelled anything; the bridge crashed. That
      is a failed command and must be reported as one.

    Both are driven for real here: `_send_locked` awaits a genuine future, and
    the test either cancels the *task* or cancels the *future*. The first
    version of these tests patched `asyncio.wait_for` to raise, which looks the
    same in both cases — so it passed against an implementation that turned
    every crash into a cancellation.
    """

    def _manager(self):
        mgr = SimBridgeManager()
        mgr._process = MagicMock()
        mgr._process.stdin = MagicMock()
        mgr._process.stdin.drain = AsyncMock()
        mgr._ensure_process = AsyncMock()  # type: ignore[method-assign]
        mgr._kill_process = AsyncMock()  # type: ignore[method-assign]
        return mgr

    async def _until_waiting(self, mgr):
        """Yield until `_send_locked` has created its future and is awaiting it."""
        for _ in range(100):
            if mgr._pending_response is not None:
                return
            await asyncio.sleep(0)
        raise AssertionError("_send_locked never reached its wait")

    @pytest.mark.asyncio
    async def test_cancelling_the_task_propagates_the_cancellation(self):
        mgr = self._manager()
        task = asyncio.ensure_future(mgr._send_locked({"cmd": "tap"}))
        await self._until_waiting(mgr)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert mgr._kill_process.await_count == 1, (
            "the subprocess was not killed on cancellation; a late response can "
            "still be handed to the next command, which the protocol cannot detect"
        )

    @pytest.mark.asyncio
    async def test_a_bridge_crash_is_a_failure_not_a_cancellation(self):
        """The regression the review caught.

        Cancelling the *future* is what `_cleanup_state()` does when the
        subprocess dies. The waiting task was not cancelled, so it must not
        come out looking cancelled — that would skip every `except Exception`
        fallback between here and the endpoint.
        """
        mgr = self._manager()
        task = asyncio.ensure_future(mgr._send_locked({"cmd": "tap"}))
        await self._until_waiting(mgr)

        mgr._pending_response.cancel()  # what the reader's cleanup does

        with pytest.raises(RuntimeError, match="exited"):
            await task
        assert not task.cancelled(), (
            "a crashed bridge surfaced as a cancelled task; nothing cancelled it"
        )
        assert mgr._kill_process.await_count == 1

    @pytest.mark.asyncio
    async def test_a_timeout_still_becomes_a_runtime_error(self):
        """The control: the timeout path is unchanged."""
        mgr = self._manager()

        async def _times_out(*_a, **_kw):
            raise TimeoutError

        with patch("asyncio.wait_for", _times_out):
            with pytest.raises(RuntimeError, match="timed out"):
                await mgr._send_locked({"cmd": "tap"})
        assert mgr._kill_process.await_count == 1


class TestProbeFlagIsHonouredOnEveryPath:
    """`probe=False` must mean no hit-tests, on the retry and on idb too.

    Two paths the review found unguarded. Both take the flag and could drop it
    without any test noticing, and dropping it silently restores the ~3.5s cost
    the flag exists to skip.
    """

    #: A tab bar the static walk reports as childless — what probing targets.
    TAB_BAR = [{
        "type": "Group", "AXLabel": "Tab Bar", "children": [],
        "frame": {"x": 0, "y": 800, "width": 400, "height": 80},
    }]

    @pytest.mark.asyncio
    async def test_the_poisoned_tree_retry_keeps_probe_false(self):
        """M5.

        A poisoned accessibility bridge (#66) is healed by one reset and a
        recursive retry. The retry must carry the caller's `probe` choice, or a
        caller that opted out pays for probing anyway — exactly when the bridge
        is already struggling.
        """
        backend = SimBridgeBackend(SimBridgeManager())
        backend._fetch_nested = AsyncMock(side_effect=lambda *_a, **_k: [dict(self.TAB_BAR[0])])  # type: ignore[method-assign]
        hits: list = []

        async def _describe_point(*_a, **_k):
            hits.append(_a)
            return None

        backend.describe_point = _describe_point  # type: ignore[method-assign]

        poisoned = iter([True, False])
        with (
            patch("server.device.sim_bridge.ax_recovery.looks_poisoned",
                  lambda _flat: next(poisoned, False)),
            patch("server.device.sim_bridge.ax_recovery.reset_bridge",
                  AsyncMock(return_value=True)),
        ):
            await backend.describe_all("udid", probe=False)

        assert backend._fetch_nested.await_count == 2, "the retry did not run"
        assert hits == [], (
            f"probe=False, yet {len(hits)} hit-test(s) ran — the retry after a "
            "bridge reset dropped the caller's choice"
        )

    @pytest.mark.asyncio
    async def test_idb_honours_probe_false(self):
        """M9.

        idb probes the same way sim-bridge does, and is the backend in use
        whenever sim-bridge is unavailable. Checking only that it *accepts* the
        keyword let an implementation that ignored it pass.
        """
        from server.device.idb import IdbBackend

        backend = IdbBackend()
        backend._run = AsyncMock(  # type: ignore[method-assign]
            return_value=(json.dumps(self.TAB_BAR), ""),
        )
        backend.describe_point = AsyncMock(return_value=None)  # type: ignore[method-assign]

        await backend.describe_all("udid", probe=False)
        assert backend.describe_point.await_count == 0, (
            "idb ran hit-tests with probe=False"
        )

        await backend.describe_all("udid", probe=True)
        assert backend.describe_point.await_count > 0, (
            "idb did not probe with probe=True — the control for the check above"
        )
