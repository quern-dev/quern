"""Tests for the cert preflight on enabling the system proxy.

The failure this prevents: capture through a device that does not trust the
mitmproxy CA fails every HTTPS request, and the symptom -- a blank screen, an
app with no network -- points nowhere near the proxy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from server.models import DeviceInfo, DeviceState, DeviceType
from server.proxy.cert_preflight import refusal_detail, simulators_without_cert


def _sim(udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED):
    return DeviceInfo(
        udid=udid, name=name, state=state,
        device_type=DeviceType.SIMULATOR, os_version="iOS 18.6", runtime="",
    )


def _phone(udid="BBBB", name="iPhone 11"):
    return DeviceInfo(
        udid=udid, name=name, state=DeviceState.BOOTED,
        device_type=DeviceType.DEVICE, os_version="iOS 26.6", runtime="",
    )


class _Ctrl:
    def __init__(self, devices):
        self.list_devices = AsyncMock(return_value=devices)


class TestPreflight:
    async def test_a_booted_simulator_with_no_cert_is_reported(self):
        with patch("server.proxy.cert_state.read_cert_state", return_value={}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert [d["name"] for d in missing] == ["iPhone 16 Pro"]

    async def test_absence_and_false_mean_the_same_thing(self):
        """A device that has never had a cert installed is not in
        cert-state.json at all."""
        with patch("server.proxy.cert_state.read_cert_state",
                   return_value={"AAAA": {"cert_installed": False}}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert len(missing) == 1

    async def test_a_trusting_simulator_is_not_reported(self):
        with patch("server.proxy.cert_state.read_cert_state",
                   return_value={"AAAA": {"cert_installed": True}}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert missing == []

    async def test_physical_devices_are_out_of_scope(self):
        """The macOS system proxy is what simulators route through. A physical
        device is proxied by its own Wi-Fi config and is unaffected by this
        call, so naming it here would be noise."""
        with patch("server.proxy.cert_state.read_cert_state", return_value={}):
            missing = await simulators_without_cert(_Ctrl([_phone()]))
        assert missing == []

    async def test_shutdown_simulators_are_out_of_scope(self):
        with patch("server.proxy.cert_state.read_cert_state", return_value={}):
            missing = await simulators_without_cert(
                _Ctrl([_sim(state=DeviceState.SHUTDOWN)])
            )
        assert missing == []

    async def test_it_never_blocks_the_call_on_its_own_failure(self):
        """A preflight that fails closed would block capture over its own bug,
        which is worse than the failure it prevents."""
        ctrl = _Ctrl([])
        ctrl.list_devices = AsyncMock(side_effect=RuntimeError("simctl exploded"))
        assert await simulators_without_cert(ctrl) == []

    async def test_no_controller_is_not_an_error(self):
        assert await simulators_without_cert(None) == []


class TestRefusal:
    def test_it_offers_more_than_installing_the_cert(self):
        """A response that only offers "install the certificate" railroads
        every user into trusting a MITM root CA."""
        detail = refusal_detail([{"udid": "AAAA", "name": "iPhone 16 Pro"}])
        actions = {r["action"] for r in detail["resolutions"]}
        assert actions == {"install_proxy_cert", "set_auto_install_cert", "skip_cert_check"}

    def test_it_names_the_devices(self):
        detail = refusal_detail([{"udid": "AAAA", "name": "iPhone 16 Pro"}])
        assert detail["devices"][0]["name"] == "iPhone 16 Pro"
        assert detail["error"] == "capture_without_cert"


class TestPolicy:
    def test_the_default_is_to_ask(self, tmp_path, monkeypatch):
        """Installing a MITM root CA is a larger commitment than the proxy
        toggle that prompts it, so consent is not assumed."""
        from server import config
        monkeypatch.setattr(config, "USER_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        assert config.get_auto_install_cert() is False

    def test_only_a_real_boolean_counts_as_consent(self, tmp_path, monkeypatch):
        """A typo should read as "ask me", never as permission."""
        import json

        from server import config
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(config, "USER_CONFIG_FILE", cfg)
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        for bogus in ["true", 1, "yes", {}]:
            cfg.write_text(json.dumps({"auto_install_cert": bogus}))
            assert config.get_auto_install_cert() is False, bogus

    def test_it_round_trips(self, tmp_path, monkeypatch):
        from server import config
        monkeypatch.setattr(config, "USER_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        config.set_auto_install_cert(True)
        assert config.get_auto_install_cert() is True
        config.set_auto_install_cert(False)
        assert config.get_auto_install_cert() is False


class TestRequestModel:
    """`skip_cert_check` bypasses a safety check, so how it is parsed matters."""

    def test_the_string_false_does_not_skip_the_check(self):
        """Read off a raw dict, `bool("false")` is True -- a caller sending
        the string would have silently bypassed the preflight."""
        from server.models import ConfigureSystemProxyRequest

        assert ConfigureSystemProxyRequest(skip_cert_check="false").skip_cert_check is False

    def test_it_defaults_to_running_the_check(self):
        from server.models import ConfigureSystemProxyRequest

        assert ConfigureSystemProxyRequest().skip_cert_check is False

    def test_an_explicit_true_skips_it(self):
        from server.models import ConfigureSystemProxyRequest

        assert ConfigureSystemProxyRequest(skip_cert_check=True).skip_cert_check is True
