"""Tests for the cert preflight on enabling the system proxy.

The failure this prevents: capture through a device that does not trust the
mitmproxy CA fails every HTTPS request, and the symptom -- a blank screen, an
app with no network -- points nowhere near the proxy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType
from server.proxy.cert_preflight import (
    refusal_detail,
    simulators_without_cert,
    trust_is_stale,
)


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

    def _is_android(self, udid):
        """Real controllers have this; `is_cert_installed` calls it first.

        Only the tests that run the real `is_cert_installed` reach it, and
        without it the preflight's fail-open `except Exception` swallowed an
        AttributeError and reported nothing missing -- a green test for a gate
        that never ran.
        """
        return False


def _trust(answers):
    """Patch what the cert manager reports, keyed by udid.

    This is the seam the preflight actually consults. The tests used to patch
    `read_cert_state`, which the preflight read directly -- and that is the bug:
    the file says what quern last recorded, while `is_cert_installed` expires
    that after an hour and re-checks the TrustStore. A test pinned to the file
    could not tell the two apart, which is why an erased simulator passed.
    """
    async def fake(_controller, udid, verify=False, *, device_name=None):
        return answers.get(udid, False)

    return patch("server.proxy.cert_manager.is_cert_installed", side_effect=fake)


class TestPreflight:
    async def test_a_booted_simulator_with_no_cert_is_reported(self):
        with _trust({}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert [d["name"] for d in missing] == ["iPhone 16 Pro"]

    async def test_absence_and_false_mean_the_same_thing(self):
        """A device that has never had a cert installed is not in
        cert-state.json at all."""
        with _trust({"AAAA": False}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert len(missing) == 1

    async def test_a_trusting_simulator_is_not_reported(self):
        with _trust({"AAAA": True}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert missing == []

    async def test_physical_devices_are_out_of_scope(self):
        """The macOS system proxy is what simulators route through. A physical
        device is proxied by its own Wi-Fi config and is unaffected by this
        call, so naming it here would be noise."""
        with _trust({}):
            missing = await simulators_without_cert(_Ctrl([_phone()]))
        assert missing == []

    async def test_shutdown_simulators_are_out_of_scope(self):
        with _trust({}):
            missing = await simulators_without_cert(
                _Ctrl([_sim(state=DeviceState.SHUTDOWN)])
            )
        assert missing == []

    async def test_an_erased_simulator_is_reported(self):
        """The reported failure, and the one the old wiring let through.

        Erasing a simulator recreates its TrustStore empty while quern's record
        still says the cert is installed. Reading that record straight through
        reported nothing missing, so enabling capture was allowed against a
        device that could not complete a single HTTPS request -- which is the
        exact case this preflight exists to refuse. The field report had a
        record 10.5 hours older than the erase that invalidated it.

        Expressed through the cert manager because that is where the decision
        lives: it expires the record after an hour and re-queries the
        TrustStore, which is what turns a stale `true` into a live `false`.
        """
        with _trust({"AAAA": False}):
            missing = await simulators_without_cert(_Ctrl([_sim()]))
        assert [d["name"] for d in missing] == ["iPhone 16 Pro"]

    async def test_every_booted_simulator_is_checked_not_just_the_first(self):
        # A mixed fleet is the normal case, and stopping at the first answer
        # would report a trusted device as the only one that mattered.
        with _trust({"AAAA": True, "CCCC": False}):
            missing = await simulators_without_cert(
                _Ctrl([_sim(), _sim(udid="CCCC", name="iPad Air")])
            )
        assert [d["name"] for d in missing] == ["iPad Air"]

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


class TestStaleTrustIsFlaggedPerDevice:
    """`proxy_status` reported a stored `cert_installed: true` as fact.

    The field report read exactly that and moved on, which is the natural thing
    to do, then spent the next hour concluding that staging authentication was
    down — because every HTTPS request from that simulator was failing with
    nothing pointing at the proxy. The record was 10.5 hours older than the
    erase that invalidated it.

    A warning elsewhere in the response is not enough on its own: someone
    looking at one device's entry should not have to correlate it against a
    list somewhere else. The flag is computed at read time, following the
    `wifi_proxy_stale` idiom already in this model.
    """

    def _entry(self, *, recorded: bool, untrusted: bool) -> DeviceCertState:
        """What proxy_status builds for one device.

        Goes through `trust_is_stale` rather than computing the answer here.
        An earlier version passed the expected value in directly, which meant
        mutating the real computation changed nothing and every test still
        passed — a test rig re-implementing the decision it is checking.
        """
        return DeviceCertState(
            name="iPhone 16 Pro",
            cert_installed=recorded,
            cert_trust_stale=trust_is_stale(
                "AAAA",
                {"cert_installed": recorded},
                {"AAAA"} if untrusted else set(),
            ),
        )

    def test_a_recorded_cert_that_is_gone_is_flagged(self):
        entry = self._entry(recorded=True, untrusted=True)
        assert entry.cert_trust_stale is True
        # The stored field is left alone deliberately: it says what quern
        # recorded, and the flag says that record is contradicted. Flipping it
        # would make the API disagree with the file for no stated reason.
        assert entry.cert_installed is True

    def test_a_device_that_never_had_one_is_not_flagged_stale(self):
        # Nothing to be stale about, and calling it stale would send someone
        # looking for an erase that never happened.
        assert self._entry(recorded=False, untrusted=True).cert_trust_stale is False

    def test_a_trusting_device_is_not_flagged(self):
        assert self._entry(recorded=True, untrusted=False).cert_trust_stale is False

    def test_the_flag_defaults_to_false(self):
        # Absence means "not contradicted", not "verified" — a shutdown
        # simulator is never checked, so it must not acquire the flag.
        assert DeviceCertState(name="iPad").cert_trust_stale is False



class TestAFreshEraseIsCaught:
    """The record is never believed, however recently it was written.

    These are the only tests here that run the real `is_cert_installed`. The
    rest patch it, which means they cannot see the cache at all -- and the
    cache is the whole defect: before this, the preflight asked with
    `verify=False`, so a record written minutes ago was returned unchecked and
    an erased simulator sailed through the gate that exists to stop it.

    Measured on a real simulator: `simctl erase` at 17:50, TrustStore empty,
    preflight reporting nothing missing, `POST /proxy/local-capture` answering
    200. The record was 3 minutes old, well inside the hour the cache holds.

    The seam is `verify_cert_in_truststore` -- the ground-truth oracle, the
    lowest point above the SQLite file itself. Patching anything higher would
    stub out the decision under test.
    """

    @pytest.fixture(autouse=True)
    def _a_ca_must_exist(self, tmp_path, monkeypatch):
        """`is_cert_installed` returns False before it reaches the TrustStore
        if no CA file exists, so without this the tests below never run the
        code they are about.

        Found by CI, not locally: this machine has `~/.mitmproxy` and the
        runner does not. One test failed honestly. The other two **passed for
        the wrong reason** -- the device was reported missing because the CA
        was absent, not because the TrustStore was consulted, which is their
        entire claim. They would have passed with the fix reverted.

        Neither of these is the decision under test. `verify_cert_in_truststore`
        stays the only oracle.
        """
        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("contents unread: the fingerprint is stubbed below")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        monkeypatch.setattr(
            "server.proxy.cert_manager.get_cert_fingerprint", lambda _p: "a" * 64,
        )

    def _record(self, udid, *, installed, age_seconds=0):
        """Write a real cert-state.json entry, as install_cert would."""
        from datetime import UTC, datetime, timedelta

        from server.proxy.cert_manager import update_cert_state

        when = datetime.now(UTC) - timedelta(seconds=age_seconds)
        update_cert_state(udid, DeviceCertState(
            name="iPhone 17e",
            cert_installed=installed,
            verified_at=when.isoformat(),
        ).model_dump())

    def _truststore(self, answer):
        return patch(
            "server.proxy.cert_manager.verify_cert_in_truststore",
            return_value=answer,
        )

    async def test_a_minutes_old_record_does_not_shield_an_erased_simulator(self):
        self._record("AAAA", installed=True, age_seconds=180)
        with self._truststore(False) as truststore:
            missing = await simulators_without_cert(_Ctrl([_sim(udid="AAAA")]))
        assert [d["udid"] for d in missing] == ["AAAA"]
        # Not decoration. Without it this passes whenever `is_cert_installed`
        # returns False for any reason at all -- which is exactly how it passed
        # on a runner with no CA file while proving nothing.
        assert truststore.called

    async def test_a_seconds_old_record_does_not_either(self):
        # The narrowest version: nothing short of a zero TTL saves this, which
        # is the point -- the fix is to stop consulting the cache, not to
        # shorten it.
        self._record("AAAA", installed=True, age_seconds=0)
        with self._truststore(False) as truststore:
            missing = await simulators_without_cert(_Ctrl([_sim(udid="AAAA")]))
        assert [d["udid"] for d in missing] == ["AAAA"]
        assert truststore.called

    async def test_the_truststore_is_still_what_decides_trust(self):
        # The converse, so the tests above cannot be satisfied by a function
        # that reports every device missing.
        self._record("AAAA", installed=False, age_seconds=0)
        with self._truststore(True) as truststore:
            missing = await simulators_without_cert(_Ctrl([_sim(udid="AAAA")]))
        assert missing == []
        assert truststore.called
