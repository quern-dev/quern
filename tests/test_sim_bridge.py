"""Tests for SimBridgeBackend — mock SimBridgeManager.send."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

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
        text = source.read_text()
        for relative in SIMULATOR_KIT_RELATIVE_PATHS:
            # The Swift constant names the binary inside the bundle; the Python
            # one names the bundle. Compare the part they share.
            assert relative.replace(".framework", "") in text, (
                f"tools/sim-bridge.swift does not look for {relative!r}; the "
                "Swift and Python halves of SimulatorKit discovery have drifted"
            )
