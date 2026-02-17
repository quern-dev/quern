"""Tests for IdbBackend — mock asyncio.create_subprocess_exec."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.device.idb import IdbBackend, _PROBEABLE_ROLES, _PROBE_STEP
from server.models import DeviceError

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Create a mock async subprocess."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value="/usr/local/bin/idb"):
            assert await backend.is_available() is True

    async def test_not_available(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value=None):
            assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# _resolve_binary
# ---------------------------------------------------------------------------


class TestResolveBinary:
    def test_found(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value="/usr/local/bin/idb"):
            path = backend._resolve_binary()
        assert path == "/usr/local/bin/idb"

    def test_cached(self):
        backend = IdbBackend()
        backend._binary = "/cached/idb"
        path = backend._resolve_binary()
        assert path == "/cached/idb"

    def test_not_found_raises(self):
        backend = IdbBackend()
        with patch.object(IdbBackend, "_find_idb", return_value=None):
            with pytest.raises(DeviceError, match="idb not found"):
                backend._resolve_binary()


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


class TestRun:
    async def test_success(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"ok\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            stdout, stderr = await backend._run("ui", "describe-all", "--udid", "X")
            assert stdout == "ok\n"
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "describe-all", "--udid", "X",
                stdout=-1, stderr=-1,
            )

    async def test_nonzero_exit_raises(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stderr=b"connection refused", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="connection refused"):
                await backend._run("ui", "describe-all")

    async def test_error_tool_is_idb(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stderr=b"fail", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError) as exc_info:
                await backend._run("ui", "tap", "100", "200")
            assert exc_info.value.tool == "idb"


# ---------------------------------------------------------------------------
# describe_all
# ---------------------------------------------------------------------------


class TestDescribeAll:
    async def test_parse_fixture(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        fixture_data = (FIXTURES / "idb_describe_all_output.json").read_bytes()
        proc = _mock_proc(stdout=fixture_data)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_all("AAAA-1111")

        assert isinstance(result, list)
        assert len(result) == 14
        assert result[0]["type"] == "Application"
        assert result[1]["AXLabel"] == "Maps"

    async def test_invalid_json_raises(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"not json")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="Failed to parse"):
                await backend.describe_all("AAAA-1111")

    async def test_non_array_raises(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b'{"not": "an array"}')
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="Expected JSON array"):
                await backend.describe_all("AAAA-1111")

    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"[]")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.describe_all("UDID-123")
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "describe-all", "--udid", "UDID-123", "--nested",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# tap
# ---------------------------------------------------------------------------


class TestTap:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.tap("AAAA-1111", 100.5, 200.3)
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "tap", "100", "200",
                "--duration", "0.05",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# swipe
# ---------------------------------------------------------------------------


class TestSwipe:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.swipe("AAAA-1111", 100.5, 200.7, 300.2, 400.9, duration=1.0)
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "swipe",
                "100", "201", "300", "401",
                "--udid", "AAAA-1111",
                "--duration", "1.0",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# type_text
# ---------------------------------------------------------------------------


class TestTypeText:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.type_text("AAAA-1111", "Hello World")
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "text", "Hello World",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# press_button
# ---------------------------------------------------------------------------


class TestPressButton:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.press_button("AAAA-1111", "HOME")
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "button", "HOME",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )


# ---------------------------------------------------------------------------
# describe_point
# ---------------------------------------------------------------------------


class TestDescribePoint:
    async def test_command_construction(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        element = {"type": "Button", "AXLabel": "Settings", "frame": {"x": 351, "y": 56, "width": 43, "height": 44}}
        proc = _mock_proc(stdout=json.dumps(element).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await backend.describe_point("AAAA-1111", 372.5, 78.0)
            mock_exec.assert_called_once_with(
                "/usr/local/bin/idb", "ui", "describe-point", "372", "78",
                "--udid", "AAAA-1111",
                stdout=-1, stderr=-1,
            )
        assert result == element

    async def test_returns_dict(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        element = {"type": "Button", "AXLabel": "Gear"}
        proc = _mock_proc(stdout=json.dumps(element).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_point("X", 100, 200)
        assert result == element

    async def test_returns_first_from_array(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        elements = [{"type": "Button", "AXLabel": "First"}, {"type": "Button", "AXLabel": "Second"}]
        proc = _mock_proc(stdout=json.dumps(elements).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_point("X", 100, 200)
        assert result["AXLabel"] == "First"

    async def test_returns_none_on_empty_array(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"[]")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_point("X", 100, 200)
        assert result is None

    async def test_returns_none_on_error(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stderr=b"no element", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_point("X", 100, 200)
        assert result is None

    async def test_returns_none_on_invalid_json(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"
        proc = _mock_proc(stdout=b"not json")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await backend.describe_point("X", 100, 200)
        assert result is None


# ---------------------------------------------------------------------------
# _is_probeable_container
# ---------------------------------------------------------------------------


class TestIsProbeableContainer:
    def test_nav_bar_empty(self):
        item = {"type": "Group", "role_description": "Nav bar", "children": []}
        assert IdbBackend._is_probeable_container(item) is True

    def test_tab_bar_by_label(self):
        item = {"type": "Group", "AXLabel": "Tab Bar", "role_description": "group", "children": []}
        assert IdbBackend._is_probeable_container(item) is True

    def test_toolbar_empty(self):
        item = {"type": "Group", "role_description": "Toolbar", "children": []}
        assert IdbBackend._is_probeable_container(item) is True

    def test_container_with_children_not_probeable(self):
        item = {
            "type": "Group", "role_description": "Nav bar",
            "children": [{"type": "Button", "AXLabel": "Back"}],
        }
        assert IdbBackend._is_probeable_container(item) is False

    def test_regular_group_not_probeable(self):
        item = {"type": "Group", "AXLabel": "Content", "role_description": "group", "children": []}
        assert IdbBackend._is_probeable_container(item) is False

    def test_button_not_probeable(self):
        item = {"type": "Button", "AXLabel": "Submit", "children": []}
        assert IdbBackend._is_probeable_container(item) is False

    def test_no_children_key_is_probeable(self):
        """Container with no children key at all (missing, not empty list)."""
        item = {"type": "Group", "role_description": "Nav bar"}
        assert IdbBackend._is_probeable_container(item) is True


# ---------------------------------------------------------------------------
# _find_empty_containers
# ---------------------------------------------------------------------------


class TestFindEmptyContainers:
    def test_finds_containers_in_nested_tree(self):
        data = json.loads((FIXTURES / "idb_describe_all_nested_output.json").read_text())
        containers = IdbBackend._find_empty_containers(data)
        assert len(containers) == 2
        # Nav bar
        assert containers[0]["role_description"] == "Nav bar"
        # Tab bar (detected by label)
        assert containers[1].get("AXLabel") == "Tab Bar"

    def test_no_containers_in_flat_list(self):
        data = json.loads((FIXTURES / "idb_describe_all_output.json").read_text())
        containers = IdbBackend._find_empty_containers(data)
        assert len(containers) == 0

    def test_deeply_nested_container(self):
        data = [
            {
                "type": "Group", "role_description": "application",
                "children": [
                    {
                        "type": "Group", "role_description": "group",
                        "children": [
                            {
                                "type": "Group", "role_description": "Toolbar",
                                "frame": {"x": 0, "y": 700, "width": 400, "height": 44},
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ]
        containers = IdbBackend._find_empty_containers(data)
        assert len(containers) == 1
        assert containers[0]["role_description"] == "Toolbar"


# ---------------------------------------------------------------------------
# _probe_container
# ---------------------------------------------------------------------------


class TestProbeContainer:
    async def test_probes_across_width(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        container = {
            "type": "Group", "role_description": "Nav bar",
            "frame": {"x": 0, "y": 56, "width": 100, "height": 44},
        }

        # 100pt wide / 20pt step = 5 probes at x=10, 30, 50, 70, 90
        call_xs = []
        async def mock_describe_point(udid, x, y):
            call_xs.append(x)
            return None

        with patch.object(backend, "describe_point", side_effect=mock_describe_point):
            await backend._probe_container("AAAA-1111", container)

        assert len(call_xs) == 5
        assert call_xs == [10.0, 30.0, 50.0, 70.0, 90.0]

    async def test_deduplicates_by_frame(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        container = {
            "type": "Group", "role_description": "Tab bar",
            "frame": {"x": 0, "y": 791, "width": 100, "height": 83},
        }

        # Two probes return the same element (same frame)
        element = {"type": "RadioButton", "AXLabel": "Home", "frame": {"x": 0, "y": 791, "width": 76, "height": 83}}

        with patch.object(backend, "describe_point", return_value=element):
            discovered = await backend._probe_container("X", container)

        # Should be deduplicated to 1 element even though 5 probes hit it
        assert len(discovered) == 1
        assert discovered[0]["AXLabel"] == "Home"

    async def test_filters_out_container_itself(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        container = {
            "type": "Group", "role_description": "Nav bar",
            "frame": {"x": 0, "y": 56, "width": 100, "height": 44},
        }

        # Probe returns the container itself (same frame)
        container_hit = {"type": "Group", "AXLabel": "Nav bar", "frame": {"x": 0, "y": 56, "width": 100, "height": 44}}

        with patch.object(backend, "describe_point", return_value=container_hit):
            discovered = await backend._probe_container("X", container)

        assert len(discovered) == 0

    async def test_discovers_multiple_elements(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        container = {
            "type": "Group", "role_description": "Nav bar",
            "frame": {"x": 0, "y": 56, "width": 402, "height": 44},
        }

        # Different elements at different X positions
        back_btn = {"type": "Button", "AXLabel": "Back", "frame": {"x": 8, "y": 56, "width": 60, "height": 44}}
        gear_btn = {"type": "Button", "AXLabel": "Settings", "frame": {"x": 351, "y": 56, "width": 43, "height": 44}}

        async def mock_describe_point(udid, x, y):
            if x < 100:
                return back_btn
            elif x > 340:
                return gear_btn
            return {"type": "Group", "frame": {"x": 0, "y": 56, "width": 402, "height": 44}}  # container itself

        with patch.object(backend, "describe_point", side_effect=mock_describe_point):
            discovered = await backend._probe_container("X", container)

        assert len(discovered) == 2
        labels = {d["AXLabel"] for d in discovered}
        assert labels == {"Back", "Settings"}

    async def test_handles_none_results(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        container = {
            "type": "Group", "role_description": "Nav bar",
            "frame": {"x": 0, "y": 56, "width": 60, "height": 44},
        }

        with patch.object(backend, "describe_point", return_value=None):
            discovered = await backend._probe_container("X", container)

        assert len(discovered) == 0

    async def test_no_frame_returns_empty(self):
        backend = IdbBackend()
        container = {"type": "Group", "role_description": "Nav bar"}
        discovered = await backend._probe_container("X", container)
        assert discovered == []

    async def test_probes_at_vertical_center(self):
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        container = {
            "type": "Group", "role_description": "Nav bar",
            "frame": {"x": 0, "y": 56, "width": 40, "height": 44},
        }

        call_ys = []
        async def mock_describe_point(udid, x, y):
            call_ys.append(y)
            return None

        with patch.object(backend, "describe_point", side_effect=mock_describe_point):
            await backend._probe_container("X", container)

        # y_center = 56 + 44/2 = 78.0
        assert all(y == 78.0 for y in call_ys)


# ---------------------------------------------------------------------------
# describe_all with probing
# ---------------------------------------------------------------------------


class TestDescribeAllWithProbing:
    async def test_probes_empty_containers(self):
        """describe_all should discover hidden elements in empty containers."""
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        fixture_data = (FIXTURES / "idb_describe_all_nested_output.json").read_bytes()
        proc = _mock_proc(stdout=fixture_data)

        # Hidden elements that probing discovers
        gear_btn = {"type": "Button", "AXLabel": "Settings", "AXUniqueId": "_Settings button",
                     "frame": {"x": 351, "y": 56, "width": 43, "height": 44}}
        home_tab = {"type": "RadioButton", "AXLabel": "Home", "AXUniqueId": "_Home tab",
                     "frame": {"x": 0, "y": 791, "width": 80, "height": 83}}
        profile_tab = {"type": "RadioButton", "AXLabel": "Profile", "AXUniqueId": "_Profile tab",
                        "frame": {"x": 160, "y": 791, "width": 80, "height": 83}}

        async def mock_probe(udid, container):
            role_desc = container.get("role_description", "")
            if role_desc == "Nav bar":
                return [gear_btn]
            else:  # Tab Bar
                return [home_tab, profile_tab]

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(backend, "_probe_container", side_effect=mock_probe):
                result = await backend.describe_all("AAAA-1111")

        # Fixture has: Application, Nav bar, StaticText, Button(Submit), Tab Bar = 5 elements
        # Plus 3 probed elements = 8 total
        assert len(result) == 8
        labels = [item.get("AXLabel") for item in result]
        assert "Settings" in labels
        assert "Home" in labels
        assert "Profile" in labels

    async def test_no_probing_for_flat_tree(self):
        """describe_all should not probe when there are no empty containers."""
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        fixture_data = (FIXTURES / "idb_describe_all_output.json").read_bytes()
        proc = _mock_proc(stdout=fixture_data)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(backend, "_probe_container") as mock_probe:
                result = await backend.describe_all("AAAA-1111")

        # No containers to probe
        mock_probe.assert_not_called()
        assert len(result) == 14

    async def test_deduplicates_probed_against_existing(self):
        """Probed elements with same frame as existing elements are skipped."""
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        # Tree with a nav bar that also has a button already in the flat list
        tree = json.dumps([
            {
                "type": "Application", "AXLabel": "App",
                "frame": {"x": 0, "y": 0, "width": 402, "height": 874},
                "children": [
                    {
                        "type": "Group", "role_description": "Nav bar",
                        "frame": {"x": 0, "y": 56, "width": 402, "height": 44},
                        "children": [],
                    },
                    {
                        "type": "Button", "AXLabel": "Existing",
                        "frame": {"x": 351, "y": 56, "width": 43, "height": 44},
                    },
                ],
            }
        ]).encode()

        proc = _mock_proc(stdout=tree)

        # Probe returns the same element that's already in the flat list
        duplicate = {"type": "Button", "AXLabel": "Existing",
                     "frame": {"x": 351, "y": 56, "width": 43, "height": 44}}

        async def mock_probe(udid, container):
            return [duplicate]

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with patch.object(backend, "_probe_container", side_effect=mock_probe):
                result = await backend.describe_all("X")

        # Application + Nav bar + Existing button = 3 (no duplicate)
        assert len(result) == 3
        existing_count = sum(1 for item in result if item.get("AXLabel") == "Existing")
        assert existing_count == 1


# ---------------------------------------------------------------------------
# select_all_and_delete
# ---------------------------------------------------------------------------


class TestSelectAllAndDelete:
    async def test_triple_tap_and_delete(self):
        """Triple-taps at coordinates then presses Backspace."""
        backend = IdbBackend()
        backend._binary = "/usr/local/bin/idb"

        calls = []

        async def mock_run(*args):
            calls.append(args)
            return ("", "")

        with patch.object(backend, "_run", side_effect=mock_run):
            await backend.select_all_and_delete("AAAA-1111", x=180.5, y=220.7)

        # 4 calls: 3 taps + 1 backspace
        assert len(calls) == 4
        # Three taps at rounded coordinates
        for i in range(3):
            assert calls[i] == ("ui", "tap", "180", "221", "--udid", "AAAA-1111")
        # Backspace
        assert calls[3] == ("ui", "key", "42", "--udid", "AAAA-1111")
