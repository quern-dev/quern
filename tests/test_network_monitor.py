"""Tests for the background network-change monitor."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from server.lifecycle.network_monitor import (
    DEFAULT_POLL_INTERVAL,
    NetworkState,
    _diff_reason,
    update_network_state,
)


class TestDiffReason:
    def test_no_change(self):
        assert _diff_reason("Home", "192.168.1.5", "Home", "192.168.1.5") is None

    def test_ssid_swap(self):
        # Both SSID and IP change — typical "moved networks" event
        result = _diff_reason("Work", "10.0.0.5", "Home", "192.168.1.5")
        assert result == "ssid_and_ip_changed"

    def test_ssid_only(self):
        # SSID flipped but IP coincidentally identical (unusual)
        result = _diff_reason("Work", "192.168.1.5", "Home", "192.168.1.5")
        assert result == "ssid_changed"

    def test_ip_only_same_ssid(self):
        """Same SSID but DHCP gave a different lease — common after a long
        sleep/wake cycle on the same network. Devices' stored proxy_host is
        now stale even though we never 'moved'."""
        result = _diff_reason("Home", "192.168.1.5", "Home", "192.168.1.50")
        assert result == "ip_changed_same_ssid"


class TestUpdateNetworkState:
    @pytest.fixture
    def patched_snapshot(self):
        with patch("server.lifecycle.network_monitor._snapshot") as m:
            yield m

    def test_baseline_observation_does_not_count_as_change(self, patched_snapshot):
        """First call after server start should establish baseline silently —
        we don't want a 'change' event firing every time the server boots."""
        patched_snapshot.return_value = ("Home", "192.168.1.5")
        state = NetworkState()
        changed = update_network_state(state)
        assert changed is False
        assert state.ssid == "Home"
        assert state.local_ip == "192.168.1.5"
        assert state.last_changed_at is None
        assert state.recent_changes == []

    def test_change_after_baseline(self, patched_snapshot):
        state = NetworkState()
        patched_snapshot.return_value = ("Work", "10.0.0.5")
        update_network_state(state)  # baseline

        patched_snapshot.return_value = ("Home", "192.168.1.5")
        changed = update_network_state(state)

        assert changed is True
        assert state.ssid == "Home"
        assert state.local_ip == "192.168.1.5"
        assert state.previous_ssid == "Work"
        assert state.previous_local_ip == "10.0.0.5"
        assert state.last_changed_at is not None
        assert state.last_change_reason == "ssid_and_ip_changed"
        assert len(state.recent_changes) == 1
        assert state.recent_changes[0]["from_ssid"] == "Work"
        assert state.recent_changes[0]["to_ssid"] == "Home"

    def test_no_change_after_steady_state(self, patched_snapshot):
        state = NetworkState()
        patched_snapshot.return_value = ("Home", "192.168.1.5")
        update_network_state(state)  # baseline
        update_network_state(state)  # same — should be no-op
        assert state.recent_changes == []
        assert state.last_changed_at is None

    def test_recent_changes_capped(self, patched_snapshot):
        """recent_changes should not grow unbounded — debug visibility, not
        a journal. Cap at 10 to keep the proxy_status response small."""
        state = NetworkState()
        patched_snapshot.return_value = ("S0", "1.0.0.0")
        update_network_state(state)  # baseline

        for i in range(1, 15):
            patched_snapshot.return_value = (f"S{i}", f"1.0.0.{i}")
            update_network_state(state)

        assert len(state.recent_changes) == 10
        # Ring keeps the most recent
        assert state.recent_changes[-1]["to_ssid"] == "S14"

    def test_ip_change_with_same_ssid_is_a_change(self, patched_snapshot):
        """The exact regression case: laptop wakes on the same Wi-Fi but
        DHCP gave a different IP. Devices' stored proxy_host is now wrong
        even though we never 'moved networks' in the colloquial sense."""
        state = NetworkState()
        patched_snapshot.return_value = ("Home", "192.168.1.5")
        update_network_state(state)  # baseline

        patched_snapshot.return_value = ("Home", "192.168.1.50")
        changed = update_network_state(state)

        assert changed is True
        assert state.last_change_reason == "ip_changed_same_ssid"

    def test_handles_no_wifi(self, patched_snapshot):
        """Detached / wired-only / Wi-Fi off — both fields can be None.
        Transitioning into / out of None state should still detect cleanly."""
        state = NetworkState()
        patched_snapshot.return_value = (None, None)
        # First (None, None) is the baseline; second observation is noise.
        update_network_state(state)
        update_network_state(state)
        assert state.recent_changes == []

        patched_snapshot.return_value = ("Home", "192.168.1.5")
        changed = update_network_state(state)
        assert changed is True
        assert state.previous_ssid is None
        assert state.ssid == "Home"


class TestPollInterval:
    def test_default_is_under_a_minute(self):
        """If we ever raise the interval to multi-minute territory, the
        'I just sat down with my laptop' UX gets sluggish — the staleness
        warning should be visible by the time the agent makes its first call."""
        assert DEFAULT_POLL_INTERVAL <= 60
