"""Tests for server.lifecycle.state — state.json management."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from server.lifecycle.state import (
    is_server_healthy,
    read_active_udid,
    read_state,
    remove_state,
    update_state,
    write_active_udid,
    write_state,
)


@pytest.fixture(autouse=True)
def tmp_state_dir(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR, STATE_FILE, and ACTIVE_DEVICE_FILE to a temp dir."""
    state_file = tmp_path / "state.json"
    active_file = tmp_path / "active-device.json"
    monkeypatch.setattr("server.lifecycle.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("server.lifecycle.state.STATE_FILE", state_file)
    monkeypatch.setattr("server.lifecycle.state.ACTIVE_DEVICE_FILE", active_file)
    return tmp_path


def test_write_then_read(tmp_state_dir):
    """write_state followed by read_state should round-trip."""
    state = {
        "pid": 12345,
        "server_port": 9100,
        "proxy_port": 9101,
        "proxy_enabled": True,
        "proxy_status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "api_key": "test-key-abc123",
    }
    write_state(state)
    result = read_state()
    assert result == state


# ---------------------------------------------------------------------------
# Active device sidecar — survives stop/start, lives separately from state.json
# ---------------------------------------------------------------------------


def test_active_udid_round_trip(tmp_state_dir):
    """write_active_udid then read_active_udid should round-trip a UDID."""
    write_active_udid("ABC-123-DEF")
    assert read_active_udid() == "ABC-123-DEF"


def test_active_udid_clear(tmp_state_dir):
    """Passing None to write_active_udid clears the persisted value."""
    write_active_udid("ABC-123-DEF")
    write_active_udid(None)
    assert read_active_udid() is None


def test_active_udid_missing_file(tmp_state_dir):
    """read_active_udid returns None when the sidecar doesn't exist."""
    assert read_active_udid() is None


def test_active_udid_survives_remove_state(tmp_state_dir):
    """The bug regression test: remove_state() must NOT touch the
    active-device sidecar. Active device is user preference and survives
    `quern stop` / `quern restart` cycles."""
    write_state({"pid": 1, "api_key": "k"})
    write_active_udid("PERSISTED-UDID")
    remove_state()
    # state.json is gone, but the active device sidecar lives on
    assert read_state() is None
    assert read_active_udid() == "PERSISTED-UDID"


def test_active_udid_handles_empty_file(tmp_state_dir):
    """An empty sidecar file should read as None, not raise."""
    from server.lifecycle.state import ACTIVE_DEVICE_FILE
    ACTIVE_DEVICE_FILE.write_text("")
    assert read_active_udid() is None


def test_active_udid_handles_corrupt_file(tmp_state_dir):
    """A malformed sidecar file should read as None and log a warning."""
    from server.lifecycle.state import ACTIVE_DEVICE_FILE
    ACTIVE_DEVICE_FILE.write_text("{not json")
    assert read_active_udid() is None


def test_read_missing_file(tmp_state_dir):
    """read_state should return None when state.json doesn't exist."""
    result = read_state()
    assert result is None


def test_read_corrupt_json(tmp_state_dir):
    """read_state should return None for invalid JSON."""
    state_file = tmp_state_dir / "state.json"
    state_file.write_text("not valid json {{{")
    result = read_state()
    assert result is None


def test_read_empty_file(tmp_state_dir):
    """read_state should return None for empty file."""
    state_file = tmp_state_dir / "state.json"
    state_file.write_text("")
    result = read_state()
    assert result is None


def test_remove_state(tmp_state_dir):
    """remove_state should delete state.json."""
    write_state({"pid": 1, "server_port": 9100})
    state_file = tmp_state_dir / "state.json"
    assert state_file.exists()
    remove_state()
    assert not state_file.exists()


def test_remove_state_missing_file(tmp_state_dir):
    """remove_state should not raise when file doesn't exist."""
    remove_state()  # Should not raise


def test_update_state_merges(tmp_state_dir):
    """update_state should merge new keys into existing state."""
    write_state({"pid": 1, "server_port": 9100, "proxy_status": "running"})
    update_state(proxy_status="stopped")
    result = read_state()
    assert result["pid"] == 1
    assert result["server_port"] == 9100
    assert result["proxy_status"] == "stopped"


def test_update_state_adds_keys(tmp_state_dir):
    """update_state should add new keys to existing state."""
    write_state({"pid": 1, "server_port": 9100})
    update_state(proxy_status="running", proxy_port=9101)
    result = read_state()
    assert result["proxy_status"] == "running"
    assert result["proxy_port"] == 9101


def test_update_state_noop_when_no_file(tmp_state_dir):
    """update_state should be a no-op when state.json doesn't exist."""
    update_state(proxy_status="stopped")  # Should not raise
    assert read_state() is None


def test_is_server_healthy_success():
    """is_server_healthy should return True when health endpoint responds 200."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("server.lifecycle.state.urllib.request.urlopen", return_value=mock_resp):
        assert is_server_healthy(9100) is True


def test_is_server_healthy_failure():
    """is_server_healthy should return False when connection fails."""
    with patch(
        "server.lifecycle.state.urllib.request.urlopen",
        side_effect=ConnectionRefusedError,
    ):
        assert is_server_healthy(9100) is False


def test_is_server_healthy_timeout():
    """is_server_healthy should return False on timeout."""
    import urllib.error

    with patch(
        "server.lifecycle.state.urllib.request.urlopen",
        side_effect=urllib.error.URLError("timeout"),
    ):
        assert is_server_healthy(9100) is False


def test_write_creates_config_dir(tmp_path):
    """write_state should create CONFIG_DIR if it doesn't exist."""
    nested = tmp_path / "sub" / "dir"
    state_file = nested / "state.json"

    with (
        patch("server.lifecycle.state.CONFIG_DIR", nested),
        patch("server.lifecycle.state.STATE_FILE", state_file),
    ):
        write_state({"pid": 1, "server_port": 9100})

    assert nested.exists()
    assert state_file.exists()


# ---------------------------------------------------------------------------
# Multi-interface enumeration (proxy_status / setup_guide disambiguation)
# ---------------------------------------------------------------------------


_IFCONFIG_DUAL_INTERFACE = """\
lo0: flags=8049<UP,LOOPBACK> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500
\tinet 192.168.31.243 netmask 0xffffff00 broadcast 192.168.31.255
en10: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500
\tinet 192.168.200.98 netmask 0xfffffe00 broadcast 192.168.201.255
en14: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500
\tinet 169.254.128.252 netmask 0xffff0000 broadcast 169.254.255.255
utun0: flags=8051<UP,POINTOPOINT,RUNNING> mtu 1500
"""


def _mock_subprocess_run(ifconfig_stdout, ssid_responses=None):
    """Return a subprocess.run double that maps ifconfig and networksetup."""
    ssid_responses = ssid_responses or {}

    def fake_run(cmd, *args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if cmd[0] == "ifconfig":
            result.stdout = ifconfig_stdout
        elif cmd[0] == "networksetup" and cmd[1] == "-getairportnetwork":
            iface = cmd[2]
            ssid = ssid_responses.get(iface)
            result.stdout = (
                f"Current Wi-Fi Network: {ssid}\n" if ssid
                else "You are not associated with an AirPort network.\n"
            )
        else:
            result.stdout = ""
        return result
    return fake_run


def test_enumerate_local_interfaces_parses_ifconfig_and_excludes_loopback():
    from server.lifecycle import state as state_mod

    with (
        patch.object(
            state_mod.subprocess if hasattr(state_mod, "subprocess") else __import__("subprocess"),
            "run",
            side_effect=_mock_subprocess_run(_IFCONFIG_DUAL_INTERFACE),
        ),
        patch(
            "server.proxy.system_proxy.get_default_route_device",
            return_value="en10",
        ),
    ):
        entries = state_mod.enumerate_local_interfaces()

    # Loopback (127.x) and link-local (169.254.x) are excluded; en0 and en10 remain.
    ips = {e["ip"] for e in entries}
    assert ips == {"192.168.31.243", "192.168.200.98"}


def test_enumerate_local_interfaces_marks_default_route():
    from server.lifecycle import state as state_mod

    with (
        patch(
            "subprocess.run",
            side_effect=_mock_subprocess_run(_IFCONFIG_DUAL_INTERFACE),
        ),
        patch(
            "server.proxy.system_proxy.get_default_route_device",
            return_value="en10",
        ),
    ):
        entries = state_mod.enumerate_local_interfaces()

    default = [e for e in entries if e["is_default_route"]]
    assert len(default) == 1
    assert default[0]["interface"] == "en10"
    assert default[0]["ip"] == "192.168.200.98"


def test_enumerate_local_interfaces_attaches_ssid_for_wifi():
    from server.lifecycle import state as state_mod

    with (
        patch(
            "subprocess.run",
            side_effect=_mock_subprocess_run(
                _IFCONFIG_DUAL_INTERFACE,
                ssid_responses={"en0": "Lilypad"},
            ),
        ),
        patch(
            "server.proxy.system_proxy.get_default_route_device",
            return_value="en10",
        ),
    ):
        entries = state_mod.enumerate_local_interfaces()

    by_iface = {e["interface"]: e for e in entries}
    assert by_iface["en0"]["ssid"] == "Lilypad"
    # en10 is Ethernet — no SSID
    assert by_iface["en10"]["ssid"] is None


def test_enumerate_local_interfaces_computes_subnet():
    from server.lifecycle import state as state_mod

    with (
        patch(
            "subprocess.run",
            side_effect=_mock_subprocess_run(_IFCONFIG_DUAL_INTERFACE),
        ),
        patch(
            "server.proxy.system_proxy.get_default_route_device",
            return_value=None,
        ),
    ):
        entries = state_mod.enumerate_local_interfaces()

    by_ip = {e["ip"]: e for e in entries}
    assert by_ip["192.168.31.243"]["subnet"] == "192.168.31.0/24"
    assert by_ip["192.168.200.98"]["subnet"] == "192.168.200.0/24"


def test_enumerate_local_interfaces_handles_ifconfig_failure():
    """If ifconfig itself blows up, return an empty list rather than raising."""
    from server.lifecycle import state as state_mod

    with patch("subprocess.run", side_effect=OSError("no ifconfig")):
        entries = state_mod.enumerate_local_interfaces()

    assert entries == []
