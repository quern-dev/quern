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


def _request(ctrl, port: int = 9101):
    adapter = SimpleNamespace(listen_port=port)
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
    return ctrl


def _ios(udid: str = "IOS1"):
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = DeviceType.DEVICE
    ctrl.adb = MagicMock()
    return ctrl


def _hosts(host="192.168.1.189"):
    return patch(
        "server.lifecycle.state.detect_host_ip_for_subnet", return_value=host,
    ), patch("server.lifecycle.state.detect_local_ip", return_value=host)


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
