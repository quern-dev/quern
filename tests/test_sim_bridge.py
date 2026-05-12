"""Tests for SimBridgeBackend — mock SimBridgeManager.send."""

from __future__ import annotations

from unittest.mock import AsyncMock

from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager


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
