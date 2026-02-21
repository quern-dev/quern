"""Tests for children_of hierarchy query (Phase 4b-delta)."""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.device.ui_elements import find_children_of
from server.main import create_app
from server.models import UIElement


# ---------------------------------------------------------------------------
# Shared fixture: nested tree
# ---------------------------------------------------------------------------

NESTED_TREE = [
    {
        "type": "Application",
        "AXLabel": "MyApp",
        "AXUniqueId": "app-root",
        "frame": {"x": 0, "y": 0, "width": 402, "height": 874},
        "children": [
            {
                "type": "Group",
                "AXLabel": "FormGroup",
                "AXUniqueId": "form-group-1",
                "frame": {"x": 0, "y": 100, "width": 402, "height": 400},
                "children": [
                    {
                        "type": "TextField",
                        "AXLabel": "Username",
                        "AXUniqueId": "username-field",
                        "frame": {"x": 20, "y": 120, "width": 360, "height": 44},
                        "children": [],
                    },
                    {
                        "type": "TextField",
                        "AXLabel": "Password",
                        "AXUniqueId": "password-field",
                        "frame": {"x": 20, "y": 180, "width": 360, "height": 44},
                        "children": [],
                    },
                    {
                        "type": "Group",
                        "AXLabel": "Actions",
                        "AXUniqueId": "actions-group",
                        "frame": {"x": 20, "y": 240, "width": 360, "height": 60},
                        "children": [
                            {
                                "type": "Button",
                                "AXLabel": "Submit",
                                "AXUniqueId": "submit-btn",
                                "frame": {"x": 20, "y": 250, "width": 120, "height": 44},
                            },
                        ],
                    },
                ],
            },
            {
                "type": "Group",
                "AXLabel": "Tab Bar",
                "AXUniqueId": "tab-bar",
                "frame": {"x": 0, "y": 791, "width": 402, "height": 83},
                "children": [
                    {
                        "type": "Button",
                        "AXLabel": "Home",
                        "AXUniqueId": "home-tab",
                        "frame": {"x": 0, "y": 791, "width": 80, "height": 83},
                    },
                ],
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Unit tests for find_children_of
# ---------------------------------------------------------------------------


class TestFindChildrenOf:

    def test_find_by_identifier(self):
        """Find children by AXUniqueId."""
        children = find_children_of(NESTED_TREE, parent_identifier="form-group-1")
        labels = [c.get("AXLabel") for c in children]
        assert "Username" in labels
        assert "Password" in labels
        assert "Actions" in labels
        assert "Submit" in labels  # Deeply nested child

    def test_find_by_label_case_insensitive(self):
        """Find children by AXLabel, case-insensitive."""
        children = find_children_of(NESTED_TREE, parent_label="formgroup")
        labels = [c.get("AXLabel") for c in children]
        assert "Username" in labels
        assert "Password" in labels

    def test_unknown_parent_returns_empty(self):
        """Returns empty list when parent is not found."""
        children = find_children_of(NESTED_TREE, parent_identifier="nonexistent")
        assert children == []

    def test_preserves_original_tree(self):
        """find_children_of must not mutate the input tree."""
        tree_copy = copy.deepcopy(NESTED_TREE)
        find_children_of(NESTED_TREE, parent_identifier="form-group-1")
        assert NESTED_TREE == tree_copy

    def test_deeply_nested(self):
        """Find children of a 3+ levels deep parent."""
        children = find_children_of(NESTED_TREE, parent_identifier="actions-group")
        assert len(children) == 1
        assert children[0].get("AXLabel") == "Submit"

    def test_leaf_node_returns_empty(self):
        """Leaf node (no children) returns empty list."""
        children = find_children_of(NESTED_TREE, parent_identifier="submit-btn")
        assert children == []

    def test_find_by_label_with_identifier_param(self):
        """When parent_identifier doesn't match but parent_label does."""
        children = find_children_of(
            NESTED_TREE,
            parent_identifier=None,
            parent_label="Tab Bar",
        )
        assert len(children) == 1
        assert children[0].get("AXLabel") == "Home"


# ---------------------------------------------------------------------------
# API test for children_of query param
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def mock_controller_with_children(app):
    """Controller with get_ui_elements_children_of mocked."""
    ctrl = DeviceController()
    ctrl._active_udid = "AAAA-1111"
    ctrl.get_ui_elements = AsyncMock(
        return_value=(
            [UIElement(type="Button", label="Submit", identifier="submit-btn")],
            "AAAA-1111",
        )
    )
    ctrl.get_ui_elements_children_of = AsyncMock(
        return_value=(
            [
                UIElement(type="TextField", label="Username", identifier="username-field"),
                UIElement(type="TextField", label="Password", identifier="password-field"),
            ],
            "AAAA-1111",
        )
    )
    app.state.device_controller = ctrl
    return ctrl


class TestChildrenOfAPI:

    async def test_children_of_param(self, app, auth_headers, mock_controller_with_children):
        """GET /ui?children_of=FormGroup returns scoped results."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui?children_of=FormGroup",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["element_count"] == 2
        labels = [e["label"] for e in data["elements"]]
        assert "Username" in labels
        assert "Password" in labels
        mock_controller_with_children.get_ui_elements_children_of.assert_called_once_with(
            children_of="FormGroup", udid=None, snapshot_depth=None,
        )

    async def test_without_children_of_uses_normal_path(self, app, auth_headers, mock_controller_with_children):
        """GET /ui without children_of uses standard get_ui_elements."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller_with_children.get_ui_elements.assert_called_once_with(udid=None, snapshot_depth=None)
        mock_controller_with_children.get_ui_elements_children_of.assert_not_called()
