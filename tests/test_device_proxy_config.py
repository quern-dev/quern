"""Pointing a device at the proxy is its own operation, and on Android quern
can do it rather than describe it.

It used to happen inside `_install_cert_android`, gated on `ANDROID_EMULATOR`
and hardcoded to `10.0.2.2:9101`. Three things were wrong with that and only
one of them was the gate:

- the mechanism is universal. `settings put global http_proxy` needs no root
  and works on any Android device on any transport, measured on an unrooted
  release-keys phone;
- the dependency ran backwards. `mitm.it` is served *by* the proxy, so a
  device must be routed before it can fetch a certificate at all -- meaning a
  device that could not take the cert got no proxy either, not even for
  plaintext HTTP, which needs no certificate;
- the setting is read when the network attaches, so writing it to a connected
  device does nothing until something reattaches.

See #265.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from server.api.proxy_certs import (
    RecordDeviceProxyRequest,
    record_device_proxy_config_endpoint,
)
from server.device.controller import DeviceController
from server.models import DeviceType


@pytest.fixture(autouse=True)
def _own_cert_state(tmp_path, monkeypatch):
    """A cert-state file per test: an apply records the device's proxy from
    before quern (#645), and a record left by one test would be the "previous"
    proxy of the next."""
    from server.proxy import cert_state

    monkeypatch.setattr(cert_state, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cert_state, "CERT_STATE_FILE", tmp_path / "cert-state.json")


def _request(ctrl, port: int = 9101, listen_host: str = "0.0.0.0",
             is_running: bool = True):
    adapter = SimpleNamespace(
        listen_port=port, listen_host=listen_host, is_running=is_running,
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        device_controller=ctrl, proxy_adapter=adapter,
    )))


def _android(udid: str = "PHONE1", *, ssid="MonaLisa", ip="192.168.1.244"):
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = DeviceType.ANDROID_DEVICE
    ctrl.adb = MagicMock()
    ctrl.adb.get_lan_ip = AsyncMock(return_value=ip)
    ctrl.adb.get_wifi_ssid = AsyncMock(return_value=ssid)
    ctrl.adb.set_http_proxy = AsyncMock()
    ctrl.adb.reattach_network = AsyncMock(return_value=True)
    # Explicit rather than a truthy MagicMock: this decides whether the Wi-Fi
    # bounce is even attempted.
    ctrl.adb.is_network_transport = MagicMock(return_value=False)
    ctrl.adb.clear_http_proxy = AsyncMock()
    ctrl.adb.get_http_proxy = AsyncMock(return_value=None)
    # No copy of its own unless a test says so: the row is the answer (#652).
    ctrl.adb.get_proxy_in_use = AsyncMock(return_value=(False, None))
    ctrl.adb.is_rootable = AsyncMock(return_value=True)
    ctrl.adb._get_cert_hash = AsyncMock(return_value="c8750f0d")
    ctrl.adb.system_cert_state = AsyncMock(return_value=True)
    return ctrl


def _ios(udid: str = "IOS1"):
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = DeviceType.DEVICE
    ctrl.adb = MagicMock()
    ctrl.adb.is_network_transport = MagicMock(return_value=False)
    return ctrl


def _hosts(host="192.168.1.189", fallback="10.99.99.99"):
    """Two *different* addresses, deliberately.

    They used to be the same value, which made the headline of this change --
    that quern picks the host interface on the device's own subnet rather than
    whatever the default route happens to be -- impossible to observe: the
    subnet lookup could be deleted outright and every test still passed. A
    machine with Wi-Fi and Ethernet on different subnets is the case that
    matters, and there the fallback is the wrong answer.
    """
    return patch(
        "server.lifecycle.state.detect_host_ip_for_subnet", return_value=host,
    ), patch("server.lifecycle.state.detect_local_ip", return_value=fallback)


class TestItAppliesRatherThanOnlyRecording:
    async def test_the_setting_is_written_to_the_device(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        ctrl.adb.set_http_proxy.assert_awaited_once_with("PHONE1", "192.168.1.189", 9101)
        assert out["applied"] is True

    async def test_the_network_is_reattached(self):
        """Without this the setting is written and ignored: measured at zero
        proxied requests before the bounce and twenty after. Reporting
        `applied` without reattaching would report the request, not the
        outcome."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        ctrl.adb.reattach_network.assert_awaited_once_with("PHONE1")
        assert out["network_reattached"] is True

    async def test_a_network_that_does_not_come_back_is_reported(self):
        """Configured but not yet capturing is a real state, and `applied`
        alone would hide it."""
        ctrl = _android()
        ctrl.adb.reattach_network = AsyncMock(return_value=False)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["applied"] is True
        assert out["network_reattached"] is False

    async def test_recording_without_applying_touches_no_device(self):
        """The iOS shape, and still the default."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", ssid="w", client_ip="192.168.1.244"),
                _request(ctrl),
            )

        ctrl.adb.set_http_proxy.assert_not_awaited()
        ctrl.adb.reattach_network.assert_not_awaited()
        assert out["applied"] is False
        assert out["network_reattached"] is None


class TestItAsksTheDeviceRatherThanTheCaller:
    async def test_ssid_and_client_ip_are_detected(self):
        """A human reading these off a screen is what has kept Android flows
        unattributable (#262)."""
        ctrl = _android(ssid="MonaLisaOverdrive", ip="192.168.1.244")
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1"), _request(ctrl),
            )

        assert out["ssid"] == "MonaLisaOverdrive"
        assert out["client_ip"] == "192.168.1.244"
        assert sorted(out["detected"]) == ["client_ip", "ssid"]

    async def test_a_supplied_value_is_not_overridden(self):
        ctrl = _android(ssid="Detected", ip="10.0.0.1")
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", ssid="Given", client_ip="10.9.9.9"),
                _request(ctrl),
            )

        assert out["ssid"] == "Given"
        assert out["client_ip"] == "10.9.9.9"
        assert out["detected"] is None

    async def test_no_wifi_is_refused_rather_than_filed_under_a_guess(self):
        ctrl = _android()
        ctrl.adb.get_wifi_ssid = AsyncMock(return_value=None)
        a, b = _hosts()
        with a, b, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1"), _request(ctrl),
            )

        assert e.value.status_code == 400
        assert "ssid" in str(e.value.detail).lower()


class TestApplyIsRefusedWhereItCannotWork:
    async def test_ios_apply_is_a_400_not_a_silent_no_op(self):
        """A caller that asked for the device to be configured and got a 200
        would reasonably believe it had been."""
        ctrl = _ios()
        a, b = _hosts()
        with a, b, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="IOS1", ssid="w", client_ip="10.0.0.9", apply=True),
                _request(ctrl),
            )

        assert e.value.status_code == 400
        assert "android" in str(e.value.detail).lower()

    async def test_ios_recording_still_works(self):
        ctrl = _ios()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="IOS1", ssid="w", client_ip="10.0.0.9"),
                _request(ctrl),
            )

        assert out["applied"] is False


class TestTheAddressAndPortAreNotGuesses:
    async def test_the_port_comes_from_the_running_adapter(self):
        """`9101` was a literal. The proxy scans for a free port when the
        default is taken, so on a machine where something else holds it every
        device was pointed at a port nothing was listening on."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, port=9188),
            )

        ctrl.adb.set_http_proxy.assert_awaited_once_with("PHONE1", "192.168.1.189", 9188)
        assert out["wifi_proxy_port"] == 9188

    async def test_the_address_used_is_reported(self):
        """So a hosting misconfiguration is diagnosable. If the device cannot
        reach this, that is the thing to check -- and the caller should not
        have to infer which address quern picked."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, port=9188),
            )

        assert out["proxy_reachable_at"] == "192.168.1.189:9188"


class TestCertInstallNoLongerRoutesTheDevice:
    """The two used to be one operation, in the order that cannot work."""

    async def test_installing_a_cert_sets_no_proxy(self, tmp_path, monkeypatch):
        from server.proxy import cert_manager

        cert = tmp_path / "mitm.pem"
        cert.write_text("-----BEGIN CERTIFICATE-----\n")

        adb = MagicMock()
        adb.is_rootable = AsyncMock(return_value=True)
        adb._get_cert_hash = AsyncMock(return_value="abc123")
        adb.is_system_cert_installed = AsyncMock(return_value=False)
        adb.install_system_cert = AsyncMock()
        adb.set_http_proxy = AsyncMock()
        # `_device_type` is present and answers ANDROID_EMULATOR so that a
        # regression restoring the old gate takes its branch rather than
        # dying on a missing attribute -- the mutant has to be *able* to
        # set the proxy for this test to be evidence that it does not.
        ctrl = SimpleNamespace(
            adb=adb, _device_type=lambda u: DeviceType.ANDROID_EMULATOR,
        )

        monkeypatch.setattr(cert_manager, "get_cert_fingerprint", lambda p: "ff")
        monkeypatch.setattr(cert_manager, "update_cert_state", lambda *a, **k: None)

        installed = await cert_manager._install_cert_android(
            ctrl, "emulator-5554", cert, False, device_name="AVD",
        )

        assert installed is True
        adb.install_system_cert.assert_awaited_once()
        adb.set_http_proxy.assert_not_awaited()


class TestTheResponseReportsTheDeviceNotTheRequest:
    """`set_http_proxy` returning without raising is not evidence the setting
    took. Reading it back is."""

    async def test_the_applied_proxy_is_read_back_from_the_device(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value="192.168.1.189:9177")
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, port=9177),
            )

        # Twice: once before the write, to know what to put back (#645), and
        # once after it, to read back what the device holds.
        assert [c.args for c in ctrl.adb.get_http_proxy.await_args_list] == [
            ("PHONE1",), ("PHONE1",)]
        assert out["device_proxy"] == "192.168.1.189:9177"

    async def test_a_write_that_did_not_take_is_visible(self):
        """The failure this repo keeps producing: success and broken looking
        identical."""
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value=None)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["applied"] is True
        assert out["device_proxy"] is None

    async def test_no_read_back_when_nothing_was_applied(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", ssid="w", client_ip="1.2.3.4"),
                _request(ctrl),
            )

        assert out["device_proxy"] is None
        ctrl.adb.get_http_proxy.assert_not_awaited()


class TestClearingIsPossibleAtAll:
    """A device pointed at a proxy that is no longer listening has no working
    network. Measured: the setting survives a reboot, so without an unset it
    stays the device's configuration."""

    async def test_clear_unsets_and_reattaches(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        ctrl.adb.clear_http_proxy.assert_awaited_once_with("PHONE1")
        ctrl.adb.reattach_network.assert_awaited_once_with("PHONE1")
        ctrl.adb.set_http_proxy.assert_not_awaited()
        assert out["cleared"] is True
        assert out["device_proxy"] is None

    async def test_clear_needs_no_ssid(self):
        """Clearing must work on a device that has dropped off Wi-Fi, which is
        exactly what a bad proxy address causes -- so requiring an SSID would
        make the broken case unrecoverable."""
        ctrl = _android()
        ctrl.adb.get_wifi_ssid = AsyncMock(return_value=None)
        ctrl.adb.get_lan_ip = AsyncMock(return_value=None)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert out["cleared"] is True

    async def test_clear_forgets_the_recorded_configs(self, tmp_path, monkeypatch):
        """A stored config outliving the setting reads as current."""
        import server.api.proxy_certs as mod
        from server.proxy import cert_state

        monkeypatch.setattr(cert_state, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cert_state, "CERT_STATE_FILE", tmp_path / "certs.json")
        cert_state.record_device_proxy_config(
            "PHONE1", "MonaLisa", "192.168.1.189", 9177, client_ip="192.168.1.244",
        )
        assert (cert_state.read_cert_state_for_device("PHONE1") or {})[
            "wifi_proxy_configs"
        ]

        ctrl = _android()
        a, b = _hosts()
        with a, b, patch.object(mod, "canonical_device_id", lambda u: u):
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert out["forgot_ssids"] == ["MonaLisa"]
        assert not (cert_state.read_cert_state_for_device("PHONE1") or {}).get(
            "wifi_proxy_configs"
        )

    async def test_apply_and_clear_together_is_refused(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True, clear=True),
                _request(ctrl),
            )

        assert e.value.status_code == 400
        ctrl.adb.set_http_proxy.assert_not_awaited()
        ctrl.adb.clear_http_proxy.assert_not_awaited()

    async def test_clear_on_ios_is_refused(self):
        ctrl = _ios()
        ctrl.adb.clear_http_proxy = AsyncMock()
        a, b = _hosts()
        with a, b, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="IOS1", clear=True), _request(ctrl),
            )

        assert e.value.status_code == 400
        ctrl.adb.clear_http_proxy.assert_not_awaited()


class TestAFailedReattachSaysWhatToDo:
    """`network_reattached: false` is accurate but not actionable on its own.
    Measured on a physical Pixel 3 XL, where the bounce sometimes leaves the
    interface NO-CARRIER indefinitely and the device has no network until it
    rejoins."""

    async def test_a_failed_reattach_carries_a_hint(self):
        ctrl = _android()
        ctrl.adb.reattach_network = AsyncMock(return_value=False)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["network_reattached"] is False
        assert "clear=true" in out["hint"]

    async def test_a_successful_reattach_carries_none(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["hint"] is None

    async def test_recording_without_applying_carries_none(self):
        """reattached is None here, not False -- nothing was attempted."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", ssid="w", client_ip="1.2.3.4"),
                _request(ctrl),
            )

        assert out["hint"] is None

    async def test_the_clear_hint_does_not_advise_clearing_again(self):
        """Observed live: the generic hint told a caller that had just
        cleared to clear again, which is a loop that cannot help. The device
        needs reconnecting, and that is the only advice left."""
        ctrl = _android()
        ctrl.adb.reattach_network = AsyncMock(return_value=False)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert "clear=true" not in out["hint"]
        assert "Wi-Fi" in out["hint"]

    async def test_a_failed_clear_reattach_carries_a_hint(self):
        ctrl = _android()
        ctrl.adb.reattach_network = AsyncMock(return_value=False)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert out["hint"] is not None


class TestItWillNotStrandADeviceItCannotReach:
    """`svc wifi disable` on a device whose adb connection runs over that same
    Wi-Fi severs the control channel, and the `enable` that would undo it can
    never arrive. The farm devices this feature targets are reached over the
    network."""

    async def test_no_bounce_over_a_network_transport(self):
        ctrl = _android("192.168.1.9:5555")
        ctrl.adb.is_network_transport = MagicMock(return_value=True)
        # What the real `reattach_network` does over a network transport: it
        # declines and says so, rather than cutting its own connection.
        ctrl.adb.reattach_network = AsyncMock(return_value=False)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="192.168.1.9:5555", apply=True),
                _request(ctrl),
            )

        assert out["applied"] is True
        assert "would cut" in out["hint"]
        assert "Settings > Wi-Fi" not in out["hint"]

    def test_the_transport_shape_is_the_test(self):
        from server.device.android.adb import AdbBackend

        assert AdbBackend.is_network_transport("192.168.1.9:5555") is True
        assert AdbBackend.is_network_transport("emulator-5554") is False
        assert AdbBackend.is_network_transport("8BAY0WCL7") is False
        assert AdbBackend.is_network_transport("LGH9328170b5f6") is False
        assert AdbBackend.is_network_transport("host:notaport") is False


class TestAProxyNothingCanReachIsNotReportedAsReachable:
    async def test_a_loopback_bind_is_flagged(self):
        """`listen_host` is caller-reconfigurable. Rebound to 127.0.0.1 the
        proxy is reachable from the Mac and from no device at all, while the
        response still names an address under `proxy_reachable_at`."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, listen_host="127.0.0.1"),
            )

        assert out["proxy_bound_locally_only"] is True

    async def test_a_wildcard_bind_is_not_flagged(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, listen_host="0.0.0.0"),
            )

        assert out["proxy_bound_locally_only"] is False


class TestTheHostAddressComesFromTheDevicesSubnet:
    """On a Mac with Wi-Fi and Ethernet on different subnets, the default-route
    address is reachable from the Mac and not from the phone."""

    async def test_the_subnet_match_wins_over_the_default_route(self):
        ctrl = _android(ip="192.168.1.244")
        a, b = _hosts(host="192.168.1.189", fallback="10.99.99.99")
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["wifi_proxy_host"] == "192.168.1.189"
        ctrl.adb.set_http_proxy.assert_awaited_once_with(
            "PHONE1", "192.168.1.189", 9101,
        )

    async def test_the_device_ip_is_what_the_subnet_lookup_is_given(self):
        """Not the caller's guess and not the host's own address: the lookup
        only works if it is handed the device's address."""
        ctrl = _android(ip="192.168.1.244")
        with patch(
            "server.lifecycle.state.detect_host_ip_for_subnet",
            return_value="192.168.1.189",
        ) as subnet, patch(
            "server.lifecycle.state.detect_local_ip", return_value="10.99.99.99",
        ):
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        subnet.assert_called_once_with("192.168.1.244")

    async def test_the_fallback_is_used_when_no_interface_shares_the_subnet(self):
        ctrl = _android(ip="192.168.1.244")
        with patch(
            "server.lifecycle.state.detect_host_ip_for_subnet", return_value=None,
        ), patch(
            "server.lifecycle.state.detect_local_ip", return_value="10.99.99.99",
        ):
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["wifi_proxy_host"] == "10.99.99.99"


class TestBothBranchesReturnTheSameShape:
    """Divergent keys between two branches of one endpoint are a KeyError
    waiting for whichever branch the caller did not test."""

    async def test_clear_and_apply_agree_on_their_keys(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            applied = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )
        ctrl2 = _android()
        c, d = _hosts()
        with c, d:
            cleared = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl2),
            )

        assert set(applied) - set(cleared) == set()
        assert cleared["applied"] is False
        assert applied["cleared"] is False


class TestQuernMakesTheComparisonItself:
    """Reporting `device_proxy` beside `proxy_reachable_at` and leaving the
    comparison to the caller is how a write that did not take goes unnoticed:
    the caller who would check is the one who already suspects a problem."""

    async def test_a_matching_read_back_verifies(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value="192.168.1.189:9177")
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, port=9177),
            )

        assert out["proxy_verified"] is True

    async def test_the_device_holding_something_else_fails_verification(self):
        """A stale value from a previous configuration is the realistic case,
        and it is indistinguishable from success without this."""
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value="10.0.2.2:9101")
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, port=9177),
            )

        assert out["applied"] is True
        assert out["proxy_verified"] is False

    async def test_a_failed_read_back_verifies_nothing_either_way(self):
        ctrl = _android()
        # The read before the write answers; the read-back after it does not.
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=[None, RuntimeError("offline")])
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["proxy_verified"] is None

    async def test_nothing_applied_verifies_nothing(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", ssid="w", client_ip="1.2.3.4"),
                _request(ctrl),
            )

        assert out["proxy_verified"] is None

    async def test_clearing_is_verified_by_asking_the_device(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value=None)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert out["proxy_verified"] is True

    async def test_a_clear_that_did_not_take_fails_verification(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value="192.168.1.189:9177")
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert out["cleared"] is True
        assert out["proxy_verified"] is False

    async def test_a_clear_whose_read_back_failed_claims_nothing(self):
        """None here is the whole point of splitting the read into a value and
        a did-it-happen: without it, a device that could not be asked reports
        the same `device_proxy: None` as one confirmed clear."""
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=RuntimeError("offline"))
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl),
            )

        assert out["device_proxy"] is None
        assert out["proxy_verified"] is None


class TestAMissingSsidDoesNotBlockTheDeviceWrite:
    """`settings put global http_proxy` is one setting for the whole device and
    is not tied to a network -- which is why `clear` has never needed an SSID.
    Requiring one to *apply* refused to configure a phone on Ethernet, a phone
    that had dropped off Wi-Fi, or any device whose `dumpsys wifi` quern could
    not read."""

    async def test_apply_without_an_ssid_still_configures_the_device(self):
        ctrl = _android()
        ctrl.adb.get_wifi_ssid = AsyncMock(return_value=None)
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        ctrl.adb.set_http_proxy.assert_awaited_once_with(
            "PHONE1", "192.168.1.189", 9101,
        )
        assert out["applied"] is True
        assert out["recorded"] is False

    async def test_recording_still_needs_one(self):
        """Without an SSID there is no key to file it under, and inventing one
        would make the record unfindable."""
        ctrl = _android()
        ctrl.adb.get_wifi_ssid = AsyncMock(return_value=None)
        a, b = _hosts()
        with a, b, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1"), _request(ctrl),
            )

        assert e.value.status_code == 400

    async def test_an_ssid_present_is_still_recorded(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl),
            )

        assert out["recorded"] is True


class TestApplyingToAProxyThatIsNotThere:
    """`listen_port` survives the adapter stopping, so nothing in the response
    would have contradicted itself: the write succeeds, the read-back matches,
    and `proxy_verified` is true -- for a device that just lost its network."""

    async def test_apply_is_refused_when_the_proxy_is_stopped(self):
        ctrl = _android()
        a, b = _hosts()
        with a, b, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True),
                _request(ctrl, is_running=False),
            )

        assert e.value.status_code == 409
        ctrl.adb.set_http_proxy.assert_not_awaited()

    async def test_recording_is_still_allowed(self):
        """Recording what someone else configured does not touch the device."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", ssid="w", client_ip="1.2.3.4"),
                _request(ctrl, is_running=False),
            )

        assert out["applied"] is False

    async def test_clearing_is_still_allowed(self):
        """Especially then: a stopped proxy is exactly when a device needs
        rescuing from pointing at it."""
        ctrl = _android()
        a, b = _hosts()
        with a, b:
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", clear=True),
                _request(ctrl, is_running=False),
            )

        assert out["cleared"] is True
        ctrl.adb.clear_http_proxy.assert_awaited_once()


class TestAProxyEndsWithTheHoldThatAppliedIt:
    """#582: a client applied a proxy to an emulator, released it, and the
    setting stayed (measured on romulus) -- the next holder inherited a
    proxied device that loses its network when the proxy stops."""

    def _holding(self, request, session_id="client:a/s1", udid="PHONE1"):
        from datetime import UTC, datetime

        opened = datetime(2026, 11, 1, tzinfo=UTC)
        session = SimpleNamespace(id=session_id, opened_at=opened)
        request.app.state.sessions = SimpleNamespace(
            holder_of=lambda u: SimpleNamespace(session=session) if u == udid else None)
        return opened

    async def _apply(self, ctrl, request, caller_session, **body):
        a, b = _hosts()
        caller = SimpleNamespace(session=caller_session, principal="client:a",
                                 is_owner=False)
        with a, b, patch("server.api.proxy_certs.visibility.current_caller",
                         return_value=caller):
            return await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", **body), request)

    async def test_the_holding_session_that_applies_it_is_recorded(self):
        ctrl, request = _android(), None
        request = _request(ctrl)
        opened = self._holding(request)
        await self._apply(ctrl, request, "client:a/s1", apply=True)
        assert request.app.state.device_proxy_holds == {"PHONE1": ("client:a/s1", opened, 0)}

    async def test_an_apply_without_a_hold_is_not_recorded(self):
        ctrl = _android()
        request = _request(ctrl)
        self._holding(request, session_id="client:b/other")
        await self._apply(ctrl, request, "client:a/s1", apply=True)
        assert request.app.state.device_proxy_holds == {}

    async def test_a_clear_ends_the_record(self):
        ctrl = _android()
        request = _request(ctrl)
        self._holding(request)
        await self._apply(ctrl, request, "client:a/s1", apply=True)
        await self._apply(ctrl, request, "client:a/s1", clear=True)
        assert request.app.state.device_proxy_holds == {}

    async def test_release_clears_it_and_says_so(self):
        from server.api import sessions as sessions_api

        ctrl = _android()
        opened = object()
        app = SimpleNamespace(state=SimpleNamespace(
            device_controller=ctrl, session_proxies=None, proxy_adapter=None,
            recordings=None, mock_owners={},
            device_proxy_holds={"PHONE1": ("ci", opened), "PHONE2": ("other", None)}))
        registry = SimpleNamespace(holder_of=lambda udid: None)
        session = SimpleNamespace(id="ci", principal="owner", opened_at=opened,
                                  reservations=["PHONE1", "PHONE2"])

        actions = await sessions_api._end_client_work(app, registry, session)
        ctrl.adb.clear_http_proxy.assert_awaited_once_with("PHONE1")
        assert {"udid": "PHONE1", "action": "device_proxy_cleared", "ok": True,
                "proxy_verified": True, "restored_proxy": None,
                "network_reattached": True} in actions
        assert app.state.device_proxy_holds == {"PHONE2": ("other", None)}

    async def test_a_later_opening_of_the_session_does_not_clear_an_earlier_ones(self):
        """The same id, opened again, is not the hold that applied it."""
        from server.api import sessions as sessions_api

        ctrl = _android()
        app = SimpleNamespace(state=SimpleNamespace(
            device_controller=ctrl, session_proxies=None, proxy_adapter=None,
            recordings=None, mock_owners={},
            device_proxy_holds={"PHONE1": ("ci", "first opening")}))
        session = SimpleNamespace(id="ci", principal="owner", opened_at="second opening",
                                  reservations=["PHONE1"])
        await sessions_api._end_client_work(
            app, SimpleNamespace(holder_of=lambda udid: None), session)
        ctrl.adb.clear_http_proxy.assert_not_awaited()

    async def test_a_failed_clear_is_reported_and_kept_for_a_retry(self):
        from server.api import sessions as sessions_api

        ctrl = _android()
        ctrl.adb.clear_http_proxy = AsyncMock(side_effect=RuntimeError("device offline"))
        app = SimpleNamespace(state=SimpleNamespace(
            device_controller=ctrl, session_proxies=None, proxy_adapter=None,
            recordings=None, mock_owners={}, device_proxy_holds={"PHONE1": ("ci", None)}))
        session = SimpleNamespace(id="ci", principal="owner", opened_at=None,
                                  reservations=["PHONE1"])
        actions = await sessions_api._end_client_work(
            app, SimpleNamespace(holder_of=lambda udid: None), session)
        assert {"udid": "PHONE1", "action": "device_proxy_cleared", "ok": False,
                "error": "device offline"} in actions
        assert "PHONE1" in app.state.device_proxy_holds


class TestApplyingSaysWhetherHttpsWillWork:
    """#582: routed but not trusting the CA, every HTTPS request fails, and
    nothing said so until requests failed. Warned, not refused."""

    async def _apply(self, ctrl):
        a, b = _hosts()
        with a, b:
            return await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl))

    async def test_a_rootable_device_that_trusts_the_ca_says_nothing_more(self):
        out = await self._apply(_android())
        assert out["applied"] is True
        assert out["https"] == {"ca_install": "automatic", "ca_trusted": True, "note": None}

    async def test_a_rootable_device_without_the_ca_is_told_to_install_it(self):
        ctrl = _android()
        ctrl.adb.system_cert_state = AsyncMock(return_value=False)
        out = await self._apply(ctrl)
        assert out["applied"] is True
        assert out["https"]["ca_trusted"] is False
        assert "install_proxy_cert" in out["https"]["note"]
        assert "reboot" in out["https"]["note"]

    async def test_an_unrootable_device_gets_the_manual_path_and_no_false_answer(self):
        ctrl = _android()
        ctrl.adb.is_rootable = AsyncMock(return_value=False)
        out = await self._apply(ctrl)
        https = out["https"]
        assert https["ca_install"] == "manual"
        assert https["ca_trusted"] is None, "a user CA is where adb cannot read it"
        assert "user certificate" in https["note"] and "network security config" in https["note"]
        ctrl.adb.system_cert_state.assert_not_awaited()

    @pytest.mark.parametrize("failure", ["unreadable props", "adb raises"])
    async def test_a_check_that_cannot_run_is_unknown_not_a_failed_apply(self, failure):
        ctrl = _android()
        ctrl.adb.is_rootable = (AsyncMock(return_value=None) if failure == "unreadable props"
                                else AsyncMock(side_effect=RuntimeError("offline")))
        out = await self._apply(ctrl)
        assert out["applied"] is True
        assert out["https"]["ca_install"] == "unknown" and out["https"]["ca_trusted"] is None

    async def test_a_failed_ca_lookup_is_could_not_check(self):
        ctrl = _android()
        ctrl.adb.system_cert_state = AsyncMock(return_value=None)
        out = await self._apply(ctrl)
        assert out["https"]["ca_trusted"] is None
        assert out["https"]["note"].startswith("Could not check")


class TestTheReleaseClearReviewFindings:
    """#582 review: the record is the session's, not the key's; a device that
    is gone is not retried for ever; a clear that did not take is no ok."""

    def _app(self, ctrl, holds):
        return SimpleNamespace(state=SimpleNamespace(
            device_controller=ctrl, session_proxies=None, proxy_adapter=None,
            recordings=None, mock_owners={}, device_proxy_holds=holds))

    async def _end(self, app, session_id="client:a/s1", holder=None):
        from server.api import sessions as sessions_api

        session = SimpleNamespace(id=session_id, principal="client:a", opened_at="o1",
                                  reservations=["PHONE1"])
        return await sessions_api._end_client_work(
            app, SimpleNamespace(holder_of=lambda udid: holder), session)

    async def test_another_session_of_the_key_holding_it_takes_the_record(self):
        ctrl = _android()
        app = self._app(ctrl, {"PHONE1": ("client:a/s1", "o1", 0)})
        s2 = SimpleNamespace(session=SimpleNamespace(id="client:a/s2", principal="client:a",
                                                     opened_at="o2"))
        actions = await self._end(app, holder=s2)
        ctrl.adb.clear_http_proxy.assert_not_awaited()
        assert app.state.device_proxy_holds == {"PHONE1": ("client:a/s2", "o2", 0)}
        assert {"udid": "PHONE1", "action": "device_proxy_handed_over", "ok": True,
                "to": "client:a/s2"} in actions

    async def test_the_same_name_opened_again_takes_the_record(self):
        """Same id, a new opening: compared by id alone, the old opening's
        release cleared the proxy the new one was using."""
        ctrl = _android()
        app = self._app(ctrl, {"PHONE1": ("client:a/s1", "o1", 0)})
        again = SimpleNamespace(session=SimpleNamespace(id="client:a/s1", principal="client:a",
                                                        opened_at="o2"))
        actions = await self._end(app, holder=again)
        ctrl.adb.clear_http_proxy.assert_not_awaited()
        assert app.state.device_proxy_holds == {"PHONE1": ("client:a/s1", "o2", 0)}
        assert any(a["action"] == "device_proxy_handed_over" for a in actions)

    async def test_another_keys_device_is_left_alone(self):
        ctrl = _android()
        app = self._app(ctrl, {"PHONE1": ("client:a/s1", "o1", 0)})
        bob = SimpleNamespace(session=SimpleNamespace(id="client:b/s1", principal="client:b",
                                                      opened_at="x"))
        actions = await self._end(app, holder=bob)
        ctrl.adb.clear_http_proxy.assert_not_awaited()
        assert app.state.device_proxy_holds == {}
        assert any(a["action"] == "device_proxy_left" for a in actions)

    async def test_a_gone_device_is_given_up_on_after_three_attempts(self):
        ctrl = _android()
        ctrl.adb.clear_http_proxy = AsyncMock(side_effect=RuntimeError("device not found"))
        app = self._app(ctrl, {"PHONE1": ("client:a/s1", "o1", 0)})
        for attempt in (1, 2):
            actions = await self._end(app)
            assert app.state.device_proxy_holds["PHONE1"][2] == attempt
            assert not any(a.get("gave_up") for a in actions)
        actions = await self._end(app)
        assert app.state.device_proxy_holds == {}
        [last] = [a for a in actions if a["action"] == "device_proxy_cleared"]
        assert last["ok"] is False and last["gave_up"] is True and "clear=true" in last["note"]
        await self._end(app)
        assert ctrl.adb.clear_http_proxy.await_count == 3, "not tried again after giving up"

    async def test_a_clear_whose_read_back_still_shows_a_proxy_is_not_ok(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value="10.0.0.1:9101")
        app = self._app(ctrl, {"PHONE1": ("client:a/s1", "o1", 0)})
        actions = await self._end(app)
        [act] = [a for a in actions if a["action"] == "device_proxy_cleared"]
        assert act["ok"] is False and "10.0.0.1:9101" in act["error"]
        assert "PHONE1" in app.state.device_proxy_holds

    async def test_an_apply_by_anyone_else_ends_the_record(self):
        ctrl = _android()
        request = _request(ctrl)
        request.app.state.device_proxy_holds = {"PHONE1": ("client:a/s1", "o1", 0)}
        request.app.state.sessions = SimpleNamespace(holder_of=lambda u: None)
        a, b = _hosts()
        owner = SimpleNamespace(session=None, principal="owner", is_owner=True)
        with a, b, patch("server.api.proxy_certs.visibility.current_caller",
                         return_value=owner):
            out = await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), request)
        assert request.app.state.device_proxy_holds == {}
        assert out["ends_with_hold"] is False


class TestSystemCertState:
    """True, False, or None when the device could not be asked: the check
    `is_system_cert_installed` makes reads a failed adb call as False."""

    async def test_present_absent_and_could_not_ask(self):
        from server.device.android.adb import AdbBackend
        from server.models import DeviceError

        backend = AdbBackend()
        backend._run_adb_for_device = AsyncMock(return_value=("quern-present\n", ""))
        assert await backend.system_cert_state("S", "c.0") is True
        backend._run_adb_for_device = AsyncMock(return_value=("quern-absent\n", ""))
        assert await backend.system_cert_state("S", "c.0") is False
        backend._run_adb_for_device = AsyncMock(side_effect=DeviceError("device offline", "adb"))
        assert await backend.system_cert_state("S", "c.0") is None
        backend._run_adb_for_device = AsyncMock(return_value=("", ""))
        assert await backend.system_cert_state("S", "c.0") is None


def test_the_reservation_answer_says_an_android_devices_ca_install():
    from server.api.sessions import _what
    from server.models import DeviceType

    emu = SimpleNamespace(name="Pixel_9", model="Pixel 9", os_version="16",
                          device_family="Android", device_type=DeviceType.ANDROID_EMULATOR,
                          ca_install="manual")
    sim = SimpleNamespace(name="iPhone", model="", os_version="iOS 18.6",
                          device_family="iPhone", device_type=DeviceType.SIMULATOR, ca_install="")
    assert _what(emu)["ca_install"] == "manual"
    assert "ca_install" not in _what(sim)


async def test_a_pool_entry_takes_the_ca_install_its_device_reports_now(tmp_path):
    """Listed as unknown while booting, then readable: the pool's entry must
    follow, or pool-backed answers stay 'unknown' (review)."""
    from server.device.pool import DevicePool
    from server.models import DeviceInfo, DeviceState

    def row(ca):
        return DeviceInfo(udid="emulator-5554", name="Pixel_9", state=DeviceState.BOOTED,
                          device_type=DeviceType.ANDROID_EMULATOR, os_version="16",
                          ca_install=ca)

    ctrl = MagicMock()
    ctrl.list_devices = AsyncMock(return_value=[row("unknown")])
    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    await pool.refresh(force=True)
    ctrl.list_devices = AsyncMock(return_value=[row("automatic")])
    await pool.refresh(force=True)
    assert pool._read_state().devices["emulator-5554"].ca_install == "automatic"


@pytest.mark.parametrize("read_back", [("10.0.0.1:9101", True), (None, False)],
                         ids=["still-set", "unreadable"])
async def test_a_clear_that_is_not_confirmed_keeps_the_record(read_back):
    """Ended only once the device says it holds nothing: otherwise the
    release, which retries, would find no record (CodeRabbit)."""
    ctrl = _android()
    value, readable = read_back
    ctrl.adb.get_http_proxy = (AsyncMock(return_value=value) if readable
                               else AsyncMock(side_effect=RuntimeError("offline")))
    request = _request(ctrl)
    request.app.state.device_proxy_holds = {"PHONE1": ("client:a/s1", "o1", 0)}
    out = await record_device_proxy_config_endpoint(
        RecordDeviceProxyRequest(udid="PHONE1", clear=True), request)
    assert out["proxy_verified"] is not True
    assert request.app.state.device_proxy_holds == {"PHONE1": ("client:a/s1", "o1", 0)}


async def test_a_hold_that_ends_while_the_proxy_is_applied_gets_it_cleared():
    """The release ran while the write and the network bounce did, and found
    no record yet: the proxy stayed on a device nobody held (CodeRabbit)."""
    ctrl = _android()
    request = _request(ctrl)
    session = SimpleNamespace(id="client:a/s1", opened_at="o1")
    holding = {"now": True}
    request.app.state.sessions = SimpleNamespace(
        holder_of=lambda u: SimpleNamespace(session=session) if holding["now"] else None)

    async def bounce(udid):
        holding["now"] = False                # the session is released meanwhile
        return True

    ctrl.adb.reattach_network = AsyncMock(side_effect=bounce)
    a, b = _hosts()
    caller = SimpleNamespace(session="client:a/s1", principal="client:a", is_owner=False)
    with a, b, patch("server.api.proxy_certs.visibility.current_caller", return_value=caller):
        out = await record_device_proxy_config_endpoint(
            RecordDeviceProxyRequest(udid="PHONE1", apply=True), request)
    ctrl.adb.clear_http_proxy.assert_awaited_once_with("PHONE1")
    assert out["hold_ended"] is True and out["ends_with_hold"] is False
    assert request.app.state.device_proxy_holds == {}


async def test_a_failed_write_leaves_no_record():
    ctrl = _android()
    ctrl.adb.set_http_proxy = AsyncMock(side_effect=RuntimeError("adb offline"))
    request = _request(ctrl)
    session = SimpleNamespace(id="client:a/s1", opened_at="o1")
    request.app.state.sessions = SimpleNamespace(
        holder_of=lambda u: SimpleNamespace(session=session))
    a, b = _hosts()
    caller = SimpleNamespace(session="client:a/s1", principal="client:a", is_owner=False)
    with a, b, patch("server.api.proxy_certs.visibility.current_caller", return_value=caller), \
         pytest.raises(HTTPException):
        await record_device_proxy_config_endpoint(
            RecordDeviceProxyRequest(udid="PHONE1", apply=True), request)
    assert request.app.state.device_proxy_holds == {}


async def test_a_stalled_clear_at_release_is_bounded(monkeypatch):
    """It runs in the sweep every session's expiry waits on (CodeRabbit)."""
    import asyncio

    from server.api import sessions as sessions_api

    ctrl = _android()

    async def hangs(udid):
        await asyncio.sleep(10)

    ctrl.adb.clear_http_proxy = AsyncMock(side_effect=hangs)
    monkeypatch.setattr(sessions_api, "_PROXY_CLEAR_TIMEOUT_S", 0.05)
    app = SimpleNamespace(state=SimpleNamespace(
        device_controller=ctrl, session_proxies=None, proxy_adapter=None, recordings=None,
        mock_owners={}, device_proxy_holds={"PHONE1": ("ci", None, 0)}))
    session = SimpleNamespace(id="ci", principal="owner", opened_at=None,
                              reservations=["PHONE1"])
    actions = await asyncio.wait_for(sessions_api._end_client_work(
        app, SimpleNamespace(holder_of=lambda udid: None), session), 2)
    [act] = [x for x in actions if x["action"] == "device_proxy_cleared"]
    assert act["ok"] is False and "did not answer" in act["error"]


async def test_a_failed_clear_after_the_hold_ended_is_reported_and_kept_for_a_retry():
    """The clear the route makes itself when the hold ended mid-apply: one
    that fails, or does not read back clear, is said and its record kept for
    the release's retry, not dropped (CodeRabbit)."""
    ctrl = _android()
    ctrl.adb.clear_http_proxy = AsyncMock(side_effect=RuntimeError("device offline"))
    request = _request(ctrl)
    session = SimpleNamespace(id="client:a/s1", opened_at="o1")
    holding = {"now": True}
    request.app.state.sessions = SimpleNamespace(
        holder_of=lambda u: SimpleNamespace(session=session) if holding["now"] else None)

    async def bounce(udid):
        holding["now"] = False
        return True

    ctrl.adb.reattach_network = AsyncMock(side_effect=bounce)
    a, b = _hosts()
    caller = SimpleNamespace(session="client:a/s1", principal="client:a", is_owner=False)
    with a, b, patch("server.api.proxy_certs.visibility.current_caller", return_value=caller):
        out = await record_device_proxy_config_endpoint(
            RecordDeviceProxyRequest(udid="PHONE1", apply=True), request)
    assert out["hold_ended"] is True and "device offline" in out["hold_ended_clear_error"]
    assert "PHONE1" in request.app.state.device_proxy_holds


class TestAProxyThatWasThereBeforeIsPutBack:
    """#645: apply overwrote a phone's own global proxy (a corporate or lab
    one) and clear deleted it, leaving the phone off the proxy it needs."""

    async def _apply(self, ctrl):
        a, b = _hosts()
        with a, b:
            return await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), _request(ctrl))

    async def _clear(self, ctrl):
        return await record_device_proxy_config_endpoint(
            RecordDeviceProxyRequest(udid="PHONE1", clear=True), _request(ctrl))

    async def test_apply_over_an_existing_proxy_then_clear_puts_it_back(self):
        ctrl = _android()
        ctrl.adb.put_http_proxy = AsyncMock()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=[
            "10.0.0.5:3128", "192.168.1.189:9101", "10.0.0.5:3128"])
        out = await self._apply(ctrl)
        assert out["previous_proxy"] == "10.0.0.5:3128"
        assert ctrl.adb.get_http_proxy.await_args_list[0].kwargs == {"strict": True}

        out = await self._clear(ctrl)
        ctrl.adb.put_http_proxy.assert_awaited_once_with("PHONE1", "10.0.0.5:3128")
        ctrl.adb.clear_http_proxy.assert_not_awaited()
        assert out["restored_proxy"] == "10.0.0.5:3128"
        assert out["proxy_verified"] is True

    async def test_apply_over_none_then_clear_deletes_it(self):
        ctrl = _android()
        ctrl.adb.put_http_proxy = AsyncMock()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=[None, "192.168.1.189:9101", None])
        assert (await self._apply(ctrl))["previous_proxy"] is None
        out = await self._clear(ctrl)
        ctrl.adb.clear_http_proxy.assert_awaited_once_with("PHONE1")
        ctrl.adb.put_http_proxy.assert_not_awaited()
        assert out["restored_proxy"] is None and out["proxy_verified"] is True

    async def test_a_read_that_fails_before_apply_refuses_it(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=RuntimeError("device offline"))
        with pytest.raises(HTTPException) as e:
            await self._apply(ctrl)
        assert e.value.status_code == 502 and "Nothing was changed" in e.value.detail
        ctrl.adb.set_http_proxy.assert_not_awaited()

    async def test_a_second_apply_keeps_the_first_record(self):
        """Read again, the device holds quern's own proxy: filing that would
        put quern's proxy back on clear."""
        ctrl = _android()
        ctrl.adb.put_http_proxy = AsyncMock()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=[
            "10.0.0.5:3128", "192.168.1.189:9101",
            "192.168.1.189:9101", "10.0.0.5:3128"])
        await self._apply(ctrl)
        assert (await self._apply(ctrl))["previous_proxy"] == "10.0.0.5:3128"
        await self._clear(ctrl)
        ctrl.adb.put_http_proxy.assert_awaited_once_with("PHONE1", "10.0.0.5:3128")

    async def test_quern_s_own_leftover_is_not_filed_as_the_previous_one(self):
        ctrl = _android()
        ctrl.adb.get_http_proxy = AsyncMock(return_value="192.168.1.189:9101")
        assert (await self._apply(ctrl))["previous_proxy"] is None

    async def test_a_restore_that_did_not_take_keeps_the_record_for_a_retry(self):
        from server.proxy.cert_state import proxy_before_quern

        ctrl = _android()
        ctrl.adb.put_http_proxy = AsyncMock()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=[
            "10.0.0.5:3128", "192.168.1.189:9101", "192.168.1.189:9101"])
        await self._apply(ctrl)
        out = await self._clear(ctrl)
        assert out["proxy_verified"] is False
        assert proxy_before_quern("PHONE1") == (True, "10.0.0.5:3128")

    async def test_a_hold_release_restores_it_too(self):
        from server.api import sessions as sessions_api

        ctrl = _android()
        ctrl.adb.put_http_proxy = AsyncMock()
        ctrl.adb.get_http_proxy = AsyncMock(side_effect=["10.0.0.5:3128", "10.0.0.5:3128"])
        from server.api.proxy_certs import _note_proxy_before_quern

        await _note_proxy_before_quern(ctrl, "PHONE1", "192.168.1.189:9101")
        opened = 1.0
        app = SimpleNamespace(state=SimpleNamespace(
            device_controller=ctrl, device_proxy_holds={"PHONE1": ("ci", opened, 0)}))
        registry = SimpleNamespace(holder_of=lambda udid: None)
        session = SimpleNamespace(id="ci", principal="owner", opened_at=opened,
                                  reservations=["PHONE1"])
        actions = await sessions_api._end_client_work(app, registry, session)
        ctrl.adb.put_http_proxy.assert_awaited_once_with("PHONE1", "10.0.0.5:3128")
        cleared = [a for a in actions if a["action"] == "device_proxy_cleared"]
        assert cleared and cleared[0]["ok"] and cleared[0]["restored_proxy"] == "10.0.0.5:3128"


class TestTheRecordOfTheProxyBeforeQuern:
    def test_a_second_record_replaces_the_first(self):
        """Which value is the device's own is the apply's decision, made from
        what the device holds now."""
        from server.proxy import cert_state

        cert_state.record_proxy_before_quern("PHONE1", "10.0.0.5:3128")
        cert_state.record_proxy_before_quern("PHONE1", "10.9.9.9:8080")
        assert cert_state.proxy_before_quern("PHONE1") == (True, "10.9.9.9:8080")

    def test_a_record_survives_a_spelling_that_has_none(self):
        from server.proxy import cert_state

        with patch("server.device.ios.devicectl.canonical_device_id",
                   side_effect=lambda u: "CANON" if u in ("RAW", "CANON") else u):
            cert_state.CERT_STATE_FILE.write_text(json.dumps({
                "RAW": {"proxy_before_quern": {"value": "10.0.0.5:3128"}},
                "CANON": {"proxy_before_quern": None}}))
            assert cert_state.proxy_before_quern("CANON") == (True, "10.0.0.5:3128")

    def test_forgetting_reaches_every_spelling(self):
        from server.proxy import cert_state

        with patch("server.device.ios.devicectl.canonical_device_id",
                   side_effect=lambda u: "CANON" if u in ("RAW", "CANON") else u):
            cert_state.CERT_STATE_FILE.write_text(json.dumps({
                "RAW": {"proxy_before_quern": {"value": "10.0.0.5:3128"}},
                "CANON": {"cert_installed": True}}))
            cert_state.forget_proxy_before_quern("CANON")
            assert cert_state.proxy_before_quern("RAW") == (False, None)

    def test_forgetting_a_device_with_no_record_writes_nothing(self):
        """A null written for a device with no other state would list it in
        cert_setup as one whose CA is not installed."""
        from server.proxy import cert_state

        cert_state.forget_proxy_before_quern("PHONE1")
        assert not cert_state.CERT_STATE_FILE.exists() \
            or json.loads(cert_state.CERT_STATE_FILE.read_text() or "{}") == {}

    def test_forgetting_ends_it(self):
        from server.proxy import cert_state

        cert_state.record_proxy_before_quern("PHONE1", None)
        assert cert_state.proxy_before_quern("PHONE1") == (True, None)
        cert_state.forget_proxy_before_quern("PHONE1")
        assert cert_state.proxy_before_quern("PHONE1") == (False, None)


class _FakeDevice:
    """A device whose proxy setting the adb mocks actually change, so a test
    reads the outcome rather than which calls were made (#645 review)."""

    def __init__(self, ctrl, value):
        self.value, self.fail_reads = value, 0

        async def get(udid, strict=False):
            if self.fail_reads:
                self.fail_reads -= 1
                raise RuntimeError("adb: device offline")
            return None if self.value == ":0" else self.value

        async def put(udid, v):
            self.value = v

        async def set_(udid, host, port):
            self.value = f"{host}:{port}"

        async def delete(udid):
            self.value = None

        async def in_use(udid):
            return (True, self.copy) if self.keeps_copy else (False, None)

        ctrl.adb.get_http_proxy = AsyncMock(side_effect=get)
        ctrl.adb.get_proxy_in_use = AsyncMock(side_effect=in_use)
        ctrl.adb.put_http_proxy = AsyncMock(side_effect=put)
        ctrl.adb.set_http_proxy = AsyncMock(side_effect=set_)
        ctrl.adb.clear_http_proxy = AsyncMock(side_effect=delete)

    # Android's own copy of the proxy (#652), off unless a test turns it on:
    # a write sets it, `:0` empties it, and a plain delete leaves it.
    keeps_copy = False
    _copy = None

    @property
    def copy(self):
        return self._copy

    def __setattr__(self, name, v):
        object.__setattr__(self, name, v)
        if name == "value" and getattr(self, "keeps_copy", False) and v is not None:
            object.__setattr__(self, "_copy", None if v == ":0" else v)


_CORP = "10.0.0.5:3128"


def _held_by_a_session(request):
    session = SimpleNamespace(id="client:a/s1", opened_at="o1", principal="client:a",
                              reservations=["PHONE1"])
    holding = {"now": True}
    request.app.state.sessions = SimpleNamespace(
        holder_of=lambda u: SimpleNamespace(session=session) if holding["now"] else None)
    caller = SimpleNamespace(session=session.id, principal="client:a", is_owner=False)
    return session, holding, patch("server.api.proxy_certs.visibility.current_caller",
                                   return_value=caller)


class TestTheRestoreSurvivesItsFailurePaths:
    """The adversarial review's repros: each ended with the device's own proxy
    deleted, which is #645 again."""

    async def _call(self, ctrl, request=None, **flags):
        a, b = _hosts()
        with a, b:
            return await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", **flags), request or _request(ctrl))

    async def test_a_restore_that_could_not_be_read_back_is_not_undone_by_the_release(self):
        from server.api import sessions as sessions_api

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        session, holding, who = _held_by_a_session(request)
        with who:
            out = await self._call(ctrl, request, apply=True)
            assert out["ends_with_hold"] is True
            dev.fail_reads = 1
            out = await self._call(ctrl, request, clear=True)
        assert dev.value == _CORP and out["proxy_verified"] is None
        holding["now"] = False
        await sessions_api._end_device_proxy(
            SimpleNamespace(state=request.app.state), request.app.state.sessions,
            session, "PHONE1", [])
        assert dev.value == _CORP

    async def test_a_release_during_the_apply_leaves_the_devices_own_proxy(self):
        from server.api.proxy_certs import clear_device_proxy

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        _, holding, who = _held_by_a_session(request)

        async def bounce(udid):
            # The session's release runs while the network bounces.
            if holding["now"]:
                holding["now"] = False
                await clear_device_proxy(ctrl, "PHONE1")
                request.app.state.device_proxy_holds.pop("PHONE1", None)
            return True

        ctrl.adb.reattach_network = AsyncMock(side_effect=bounce)
        with who:
            out = await self._call(ctrl, request, apply=True)
        assert out.get("hold_ended") is True
        assert dev.value == _CORP

    async def test_a_proxy_set_after_an_uncleared_apply_is_the_one_kept(self):
        ctrl = _android()
        dev = _FakeDevice(ctrl, None)
        await self._call(ctrl, apply=True)
        dev.value = "10.9.9.9:8080"     # an admin sets a lab proxy afterwards
        out = await self._call(ctrl, apply=True)
        assert out["previous_proxy"] == "10.9.9.9:8080"
        await self._call(ctrl, clear=True)
        assert dev.value == "10.9.9.9:8080"

    async def test_an_older_quern_address_is_not_filed_as_the_devices_own(self):
        from server.proxy.cert_state import record_device_proxy_config

        ctrl = _android()
        dev = _FakeDevice(ctrl, "192.168.1.50:9101")    # quern, from another Mac address
        record_device_proxy_config("PHONE1", "MonaLisa", "192.168.1.50", 9101)
        out = await self._call(ctrl, apply=True)
        assert out["previous_proxy"] is None
        await self._call(ctrl, clear=True)
        assert dev.value is None

    async def test_a_clear_on_a_device_quern_never_recorded_leaves_no_entry(self):
        from server.proxy import cert_state

        ctrl = _android()
        _FakeDevice(ctrl, None)
        await self._call(ctrl, clear=True)
        assert not cert_state.CERT_STATE_FILE.exists() \
            or json.loads(cert_state.CERT_STATE_FILE.read_text() or "{}") == {}

    async def test_a_failed_first_write_leaves_no_record(self):
        from server.proxy.cert_state import proxy_before_quern

        ctrl = _android()
        _FakeDevice(ctrl, _CORP)
        ctrl.adb.set_http_proxy = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(HTTPException):
            await self._call(ctrl, apply=True)
        assert proxy_before_quern("PHONE1") == (False, None)

    async def test_a_failed_second_write_keeps_the_first_record(self):
        from server.proxy.cert_state import proxy_before_quern

        ctrl = _android()
        _FakeDevice(ctrl, _CORP)
        await self._call(ctrl, apply=True)
        ctrl.adb.set_http_proxy = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(HTTPException):
            await self._call(ctrl, apply=True)
        assert proxy_before_quern("PHONE1") == (True, _CORP)

    async def test_a_record_that_cannot_be_written_refuses_the_apply(self):
        ctrl = _android()
        _FakeDevice(ctrl, _CORP)
        with patch("server.proxy.cert_state.update_cert_state",
                   side_effect=PermissionError("read-only")), pytest.raises(HTTPException) as e:
            await self._call(ctrl, apply=True)
        assert e.value.status_code == 500 and "Could not record" in e.value.detail
        ctrl.adb.set_http_proxy.assert_not_awaited()

    async def test_querns_earlier_address_is_known_without_an_ssid(self):
        """A device off Wi-Fi gets no per-network config, so only the record
        can say quern's last address was quern's (CodeRabbit)."""
        ctrl = _android(ssid=None)
        dev = _FakeDevice(ctrl, _CORP)
        await self._call(ctrl, _request(ctrl, port=9101), apply=True)
        out = await self._call(ctrl, _request(ctrl, port=9102), apply=True)
        assert out["previous_proxy"] == _CORP
        await self._call(ctrl, clear=True)
        assert dev.value == _CORP

    async def test_a_write_reported_failed_that_landed_keeps_the_record_and_hold(self):
        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        _, _, who = _held_by_a_session(request)

        async def landed_then_failed(udid, host, port):
            dev.value = f"{host}:{port}"
            raise RuntimeError("adb: connection reset")

        ctrl.adb.set_http_proxy = AsyncMock(side_effect=landed_then_failed)
        with who, pytest.raises(HTTPException) as e:
            await self._call(ctrl, request, apply=True)
        assert "may have reached the device" in e.value.detail
        assert "PHONE1" in request.app.state.device_proxy_holds
        await self._call(ctrl, request, clear=True)
        assert dev.value == _CORP

    async def test_a_second_clear_leaves_the_restored_proxy_alone(self):
        """With the record gone after a restore, a second clear deleted the
        proxy it had just put back (CodeRabbit)."""
        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        await self._call(ctrl, apply=True)
        await self._call(ctrl, clear=True)
        writes, bounces = ctrl.adb.put_http_proxy.await_count, ctrl.adb.reattach_network.await_count
        out = await self._call(ctrl, clear=True)
        assert dev.value == _CORP and out["restored_proxy"] == _CORP
        assert out["proxy_verified"] is True
        # Nothing to undo, so no write and no Wi-Fi bounce (#652).
        assert ctrl.adb.put_http_proxy.await_count == writes
        assert ctrl.adb.reattach_network.await_count == bounces

    async def test_a_late_write_after_the_release_is_undone(self):
        """The release restores the device while the write is in flight; the
        write then lands and adb reports it failed (CodeRabbit)."""
        from server.api.proxy_certs import clear_device_proxy

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        _, holding, who = _held_by_a_session(request)

        async def released_then_landed(udid, host, port):
            holding["now"] = False
            await clear_device_proxy(ctrl, "PHONE1")
            request.app.state.device_proxy_holds.pop("PHONE1", None)
            dev.value = f"{host}:{port}"
            raise RuntimeError("adb: connection reset")

        ctrl.adb.set_http_proxy = AsyncMock(side_effect=released_then_landed)
        with who, pytest.raises(HTTPException):
            await self._call(ctrl, request, apply=True)
        assert dev.value == _CORP

    async def test_an_untouched_repeat_apply_keeps_the_hold(self):
        """The device is still on the first apply's proxy, so the release must
        still put it back (CodeRabbit)."""
        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        _, _, who = _held_by_a_session(request)
        with who:
            await self._call(ctrl, request, apply=True)
            ctrl.adb.set_http_proxy = AsyncMock(side_effect=RuntimeError("boom"))
            with pytest.raises(HTTPException):
                await self._call(ctrl, request, apply=True)
        assert "PHONE1" in request.app.state.device_proxy_holds
        await self._call(ctrl, request, clear=True)
        assert dev.value == _CORP

    async def test_a_release_that_cannot_confirm_the_restore_retries(self):
        """A read-back that failed is not a confirmation (CodeRabbit)."""
        from server.api import sessions as sessions_api

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        session, holding, who = _held_by_a_session(request)
        with who:
            await self._call(ctrl, request, apply=True)
        holding["now"] = False
        app = SimpleNamespace(state=request.app.state)
        dev.fail_reads = 1
        actions: list = []
        await sessions_api._end_device_proxy(app, request.app.state.sessions, session,
                                             "PHONE1", actions)
        assert actions[-1]["ok"] is False and "PHONE1" in request.app.state.device_proxy_holds
        await sessions_api._end_device_proxy(app, request.app.state.sessions, session,
                                             "PHONE1", actions)
        assert actions[-1]["ok"] is True and actions[-1]["proxy_verified"] is True
        assert dev.value == _CORP and "PHONE1" not in request.app.state.device_proxy_holds


class TestTheProxyInUseIsWhatCounts:
    """#652: Android keeps its own copy of the proxy, and a plain delete
    leaves it, so a device can read no proxy while it still uses quern's."""

    async def _call(self, ctrl, **flags):
        a, b = _hosts()
        with a, b:
            return await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", **flags), _request(ctrl))

    def _device(self, value=None):
        ctrl = _android()
        dev = _FakeDevice(ctrl, None)
        dev.keeps_copy = True
        dev.value = value
        return ctrl, dev

    async def test_a_delete_that_leaves_the_copy_is_not_verified(self):
        ctrl, dev = self._device()
        await self._call(ctrl, apply=True)

        async def delete_only(udid):        # the clear as it was before #652
            object.__setattr__(dev, "value", None)

        ctrl.adb.clear_http_proxy = AsyncMock(side_effect=delete_only)
        out = await self._call(ctrl, clear=True)
        assert out["proxy_verified"] is False
        assert out["device_proxy"] == "192.168.1.189:9101"

    async def test_the_real_clear_empties_the_copy_and_is_verified(self):
        """quern's own clear_http_proxy against a device whose delete leaves
        the copy, as the phone's did (review: a stand-in clear proved nothing)."""
        from server.device.android.adb import AdbBackend

        ctrl, dev = self._device()
        await self._call(ctrl, apply=True)

        async def run(serial, *args, timeout=None):
            assert args[:4] == ("shell", "settings", "delete", "global")
            object.__setattr__(dev, "value", None)      # the row only
            return "", ""

        adb = SimpleNamespace(put_http_proxy=ctrl.adb.put_http_proxy,
                              get_proxy_in_use=ctrl.adb.get_proxy_in_use,
                              _run_adb_for_device=run)
        ctrl.adb.clear_http_proxy = lambda udid: AdbBackend.clear_http_proxy(adb, udid)
        out = await self._call(ctrl, clear=True)
        assert dev.copy is None and dev.value is None
        assert out["proxy_verified"] is True and out["device_proxy"] is None

    async def test_a_failed_write_that_changed_only_the_row_is_not_untouched(self):
        """The copy follows the row a moment later, so a copy that still reads
        as before is no proof the write did not land (review)."""
        ctrl, dev = self._device("10.0.0.5:3128")
        request = _request(ctrl)
        _, _, who = _held_by_a_session(request)

        async def row_landed_then_failed(udid, host, port):
            object.__setattr__(dev, "value", f"{host}:{port}")   # copy not yet
            raise RuntimeError("adb: connection reset")

        ctrl.adb.set_http_proxy = AsyncMock(side_effect=row_landed_then_failed)
        a, b = _hosts()
        with a, b, who, pytest.raises(HTTPException) as e:
            await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", apply=True), request)
        assert "may have reached the device" in e.value.detail
        assert "PHONE1" in request.app.state.device_proxy_holds

    async def test_a_policy_proxy_in_the_copy_alone_is_put_back(self):
        """Device policy sets the copy, not the row (#645 meets #652)."""
        ctrl, dev = self._device()
        object.__setattr__(dev, "_copy", "10.0.0.5:3128")
        out = await self._call(ctrl, apply=True)
        assert out["previous_proxy"] == "10.0.0.5:3128"
        out = await self._call(ctrl, clear=True)
        ctrl.adb.put_http_proxy.assert_awaited_with("PHONE1", "10.0.0.5:3128")
        assert dev.copy == "10.0.0.5:3128" and out["proxy_verified"] is True

    async def test_an_unconfirmed_clear_does_not_bounce_and_is_refused(self):
        """A clear that fails before the delete is a 502, with no Wi-Fi bounce
        to send requests through the old proxy (CodeRabbit on #653)."""
        from server.models import DeviceError

        ctrl, _ = self._device()
        await self._call(ctrl, apply=True)
        bounces = ctrl.adb.reattach_network.await_count
        ctrl.adb.clear_http_proxy = AsyncMock(
            side_effect=DeviceError("not seen to empty", tool="adb"))
        with pytest.raises(HTTPException) as e:
            await self._call(ctrl, clear=True)
        assert e.value.status_code == 502
        assert ctrl.adb.reattach_network.await_count == bounces


class TestAResetToNoProxy:
    """clear=true, restore=false (#654): no proxy, whatever quern recorded --
    for a shared or remote device whose history quern cannot vouch for."""

    async def _call(self, ctrl, request=None, **flags):
        a, b = _hosts()
        with a, b:
            return await record_device_proxy_config_endpoint(
                RecordDeviceProxyRequest(udid="PHONE1", **flags), request or _request(ctrl))

    async def test_it_removes_the_proxy_instead_of_restoring_it(self):
        from server.proxy.cert_state import proxy_before_quern

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        await self._call(ctrl, apply=True)
        out = await self._call(ctrl, clear=True, restore=False)
        assert dev.value is None and out["proxy_verified"] is True
        assert out["discarded_proxy"] == _CORP and out["restored_proxy"] is None
        assert proxy_before_quern("PHONE1") == (False, None)

    async def test_a_plain_clear_still_restores(self):
        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        await self._call(ctrl, apply=True)
        out = await self._call(ctrl, clear=True)
        assert dev.value == _CORP and out["discarded_proxy"] is None

    async def test_restore_false_without_clear_is_refused(self):
        ctrl = _android()
        with pytest.raises(HTTPException) as e:
            await self._call(ctrl, restore=False)
        assert e.value.status_code == 400 and "clear=true" in e.value.detail

    async def test_a_reset_that_could_not_be_read_back_is_not_undone_by_the_release(self):
        """The record goes once the removal is written: kept, the hold's
        release would have put the old proxy back after a reset."""
        from server.api import sessions as sessions_api

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        session, holding, who = _held_by_a_session(request)
        with who:
            await self._call(ctrl, request, apply=True)
            dev.fail_reads = 1
            out = await self._call(ctrl, request, clear=True, restore=False)
        assert out["proxy_verified"] is None and dev.value is None
        assert "PHONE1" in request.app.state.device_proxy_holds    # the release retries
        holding["now"] = False
        clears = ctrl.adb.clear_http_proxy.await_count
        await sessions_api._end_device_proxy(
            SimpleNamespace(state=request.app.state), request.app.state.sessions,
            session, "PHONE1", [])
        assert dev.value is None
        # It did retry, and by removing, not restoring (CodeRabbit).
        assert ctrl.adb.clear_http_proxy.await_count == clears + 1
        ctrl.adb.put_http_proxy.assert_not_awaited()


    async def test_a_reset_whose_write_raises_is_not_undone_by_the_release(self):
        """clear_http_proxy writes :0 and then raises when Android is not seen
        to empty its copy: the 502 left the record, and the release restored
        the proxy the reset threw away (review)."""
        from server.api import sessions as sessions_api

        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        request = _request(ctrl)
        session, holding, who = _held_by_a_session(request)

        async def zero_then_raise(udid):
            dev.value = None
            raise RuntimeError("Android was not seen to empty its copy of the proxy")

        with who:
            await self._call(ctrl, request, apply=True)
            ctrl.adb.clear_http_proxy = AsyncMock(side_effect=zero_then_raise)
            with pytest.raises(HTTPException) as e:
                await self._call(ctrl, request, clear=True, restore=False)
        assert e.value.status_code == 502
        ctrl.adb.clear_http_proxy = AsyncMock(side_effect=lambda udid: None)
        holding["now"] = False
        await sessions_api._end_device_proxy(
            SimpleNamespace(state=request.app.state), request.app.state.sessions,
            session, "PHONE1", [])
        assert dev.value is None
        ctrl.adb.put_http_proxy.assert_not_awaited()


    async def test_a_reset_blanks_the_record_under_every_spelling(self):
        """The merged read takes a later spelling's record over the canonical
        one's: blanking only the canonical key left the old value readable
        (CodeRabbit)."""
        from server.proxy import cert_state

        with patch("server.device.ios.devicectl.canonical_device_id",
                   side_effect=lambda u: "CANON" if u in ("RAW", "CANON") else u):
            cert_state.CERT_STATE_FILE.write_text(json.dumps({
                "CANON": {"proxy_before_quern": {"value": _CORP, "quern_addresses": ["q:1"]}},
                "RAW": {"proxy_before_quern": {"value": _CORP, "quern_addresses": ["q:1"]}}}))
            cert_state.blank_proxy_before_quern("CANON")
            assert cert_state.proxy_before_quern("CANON") == (True, None)
            assert cert_state.quern_addresses_applied("CANON") == ["q:1"]

    async def test_a_clear_and_a_reset_of_one_device_take_turns(self):
        """A clear that read the old proxy and paused while writing it back
        landed after a reset had removed it (CodeRabbit)."""
        ctrl = _android()
        dev = _FakeDevice(ctrl, _CORP)
        await self._call(ctrl, apply=True)
        started, release = asyncio.Event(), asyncio.Event()
        real_put = ctrl.adb.put_http_proxy.side_effect

        async def slow_put(udid, value):
            started.set()
            await release.wait()
            await real_put(udid, value)

        ctrl.adb.put_http_proxy = AsyncMock(side_effect=slow_put)
        restoring = asyncio.ensure_future(self._call(ctrl, clear=True))
        await asyncio.wait_for(started.wait(), 2)
        resetting = asyncio.ensure_future(self._call(ctrl, clear=True, restore=False))
        await asyncio.sleep(0.05)
        assert not resetting.done()                  # waits for the clear in flight
        release.set()
        await asyncio.wait_for(asyncio.gather(restoring, resetting), 5)
        assert dev.value is None                     # the reset is what lasts
