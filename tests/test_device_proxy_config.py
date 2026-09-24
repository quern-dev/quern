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


def _request(ctrl, port: int = 9101, listen_host: str = "0.0.0.0"):
    adapter = SimpleNamespace(listen_port=port, listen_host=listen_host)
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

        ctrl.adb.get_http_proxy.assert_awaited_once_with("PHONE1")
        assert out["device_proxy"] == "192.168.1.189:9177"

    async def test_a_write_that_did_not_take_is_visible(self):
        """The failure this repo keeps producing: success and broken looking
        identical. If the device reports something other than what quern set,
        the response says so rather than echoing the request."""
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
        from server.device.adb import AdbBackend

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
