"""Tests for the cert preflight on enabling the system proxy.

The failure this prevents: capture through a device that does not trust the
mitmproxy CA fails every HTTPS request, and the symptom -- a blank screen, an
app with no network -- points nowhere near the proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType
from server.proxy.cert_preflight import (
    refusal_detail,
    simulators_without_cert,
    trust_is_stale,
    warn_if_capture_lacks_trust,
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
    async def fake(_controller, udid, *, device_name=None):
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


class TestOneUncheckableDeviceDoesNotUnRefuseTheRest:
    """A failing check must lose only that device, never the confirmed ones.

    The handler used to sit around the whole loop, so one device raising
    returned `[]` -- discarding every device already *confirmed* untrusted.
    `_ensure_ca_is_trusted` then saw nothing missing and allowed capture, which
    is the gate opening on precisely the state it exists to catch. Failing open
    is right for a device we could not check and never for one we could.
    """

    @pytest.fixture(autouse=True)
    def _a_ca_must_exist(self, tmp_path, monkeypatch):
        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("contents unread: the fingerprint is stubbed below")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        monkeypatch.setattr(
            "server.proxy.cert_manager.get_cert_fingerprint", lambda _p: "a" * 64,
        )

    def _trust_raising_on(self, exploding_udid, answers):
        async def fake(_controller, udid, *, device_name=None):
            if udid == exploding_udid:
                raise OSError("TrustStore unreadable")
            return answers.get(udid, False)

        return patch("server.proxy.cert_manager.is_cert_installed", side_effect=fake)

    async def test_a_confirmed_untrusted_device_survives_a_later_failure(self):
        devices = [_sim(udid="GOOD", name="trusts it"),
                   _sim(udid="BAD", name="does not"),
                   _sim(udid="BOOM", name="unreadable")]
        with self._trust_raising_on("BOOM", {"GOOD": True, "BAD": False}):
            missing = await simulators_without_cert(_Ctrl(devices))
        assert [d["udid"] for d in missing] == ["BAD"], (
            "a failing check discarded a device already confirmed untrusted"
        )

    async def test_the_order_does_not_matter(self):
        # The failure first, so the fix cannot be "collect before raising".
        devices = [_sim(udid="BOOM", name="unreadable"),
                   _sim(udid="BAD", name="does not")]
        with self._trust_raising_on("BOOM", {"BAD": False}):
            missing = await simulators_without_cert(_Ctrl(devices))
        assert [d["udid"] for d in missing] == ["BAD"]

    async def test_the_uncheckable_device_is_not_itself_reported_missing(self):
        # Failing open for it specifically: we do not know, so we do not claim.
        with self._trust_raising_on("BOOM", {}):
            missing = await simulators_without_cert(_Ctrl([_sim(udid="BOOM")]))
        assert missing == []


class TestTheStartupPathSaysSomething:
    """`quern enable-local-capture` writes config.json and the lifespan starts
    the adapter from it, so nothing on that route passes the capture gate.

    It cannot be gated the way the endpoints are -- there is no request to
    refuse, and refusing to boot the server over one device's certificate is a
    worse outcome than the failure it would prevent. So the requirement is only
    that the state is no longer silent: before this, the server started
    cleanly, printed `Local capture: MyApp`, captured nothing decryptable, and
    said so only in `proxy_status`, which the person who ran the CLI never sees.
    """

    async def test_it_warns_when_capture_is_on_and_the_ca_is_not_trusted(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        ctrl = _Ctrl([_sim()])
        with _trust({"AAAA": False}):
            with caplog.at_level("WARNING"):
                missing = await warn_if_capture_lacks_trust(ctrl, ["MyApp"])
        assert [d["udid"] for d in missing] == ["AAAA"]
        assert "do(es) not trust" in caplog.text
        assert "iPhone 16 Pro" in caplog.text, "the warning has to name the device"
        assert "MyApp" in caplog.text, "and what is being captured"

    async def test_it_is_quiet_when_the_ca_is_trusted(self, monkeypatch, caplog):
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        ctrl = _Ctrl([_sim()])
        with _trust({"AAAA": True}):
            with caplog.at_level("WARNING"):
                missing = await warn_if_capture_lacks_trust(ctrl, ["MyApp"])
        assert missing == []
        assert caplog.text == "", "a warning on the healthy path is noise"

    async def test_it_does_not_ask_when_capture_is_off(self, caplog):
        """The check costs a TrustStore query per booted simulator. With no
        capture configured there is nothing to warn about, and warning anyway
        would fire on every server start on every machine."""
        ctrl = _Ctrl([_sim()])
        with _trust({"AAAA": False}):
            missing = await warn_if_capture_lacks_trust(ctrl, [])
        assert missing == []
        ctrl.list_devices.assert_not_awaited()

    def test_the_lifespan_actually_calls_it(self):
        """A correct function nobody calls is not a fix (CONTRIBUTING 2.4).

        Static, because the lifespan cannot be run here -- it builds a real
        DeviceController and every adapter, and the existing suite notes in
        three places that it does not run under test. So deleting the call site
        left all of this file green while the startup path went back to being
        silent, which is the mutation this test exists for.

        Also pins the `await`. `warn_if_capture_lacks_trust` is async, so
        dropping it leaves a coroutine that is never run: no warning, no error,
        and a RuntimeWarning in a stream nobody reads.
        """
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parents[1] / "server" / "main.py").read_text())

        awaited = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            awaited.add(getattr(func, "id", None) or getattr(func, "attr", None))

        assert "warn_if_capture_lacks_trust" in awaited, (
            "server/main.py does not await warn_if_capture_lacks_trust, so a "
            "server started with local capture against an untrusting simulator "
            "reports nothing anywhere the CLI user can see it"
        )

        # One frame out, and the same bug. The check lives inside
        # `_warmup_devices`, which is only ever reached because something
        # schedules it -- replacing `create_task(_warmup_devices())` with `None`
        # left the whole startup path dead and every test green.
        scheduled = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (getattr(func, "attr", None) or getattr(func, "id", None)) != "create_task":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Call):
                    scheduled.add(
                        getattr(arg.func, "id", None) or getattr(arg.func, "attr", None)
                    )

        assert "_warmup_devices" in scheduled, (
            "nothing schedules _warmup_devices, so the startup cert check never "
            "runs no matter what it contains"
        )


class TestTheCliCommandIsGatedToo:
    """`quern enable-local-capture` is the fourth routing boundary.

    It was left ungated on the reasoning that it is the human's path, which was
    wrong twice over: an agent has a shell and the agent guide names this
    command, and the config it writes is what the lifespan starts routing from
    in the next process. The gate runs in-process -- a DeviceController does no
    I/O to construct -- so the CLI does not need an HTTP client to ask the same
    question the endpoints ask.
    """

    @pytest.fixture(autouse=True)
    def _machine_state(self, monkeypatch, tmp_path):
        """Pin what the CLI gate reads off the machine.

        Two dependencies, both introduced by this branch and both invisible
        locally. The CA-existence guard means these tests would pass on a
        developer's Mac (which has ~/.mitmproxy) and fail on CI (which does
        not), reporting the opposite of the behaviour under test. And the gate
        now runs after the no-op early return, so a sibling test that persists
        a capture list into the sandboxed config.json makes this one skip the
        gate entirely -- which is how it passed alone and failed in the suite.
        """
        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\n")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        from server import main

        monkeypatch.setattr(main, "get_local_capture_processes", lambda: [])
        monkeypatch.setattr(main, "read_state", lambda: None)

    def _refuses(self, monkeypatch, missing):
        async def _missing(_controller):
            return list(missing)

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", _missing
        )
        monkeypatch.setattr(
            "server.device.controller.DeviceController", lambda: object()
        )

    def test_it_refuses_and_writes_nothing(self, monkeypatch, capsys):
        from server import main

        self._refuses(monkeypatch, [{"udid": "AAAA1111", "name": "iPhone 16 Pro"}])
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        wrote = []
        monkeypatch.setattr(main, "set_local_capture_processes", wrote.append)

        with pytest.raises(SystemExit) as exc:
            main._cmd_enable_local_capture(["MyApp"])

        assert exc.value.code == 1, "a script ignoring the text must still see failure"
        assert wrote == [], "config.json was written by a refused command"
        out = capsys.readouterr().out
        assert "iPhone 16 Pro" in out, "the refusal has to name the device"
        assert "--skip-cert-check" in out, "and the way past it"

    def test_the_skip_flag_lets_it_through(self, monkeypatch):
        from server import main

        self._refuses(monkeypatch, [{"udid": "AAAA1111", "name": "iPhone 16 Pro"}])
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: False)
        wrote = []
        monkeypatch.setattr(main, "set_local_capture_processes", wrote.append)
        monkeypatch.setattr(main, "get_local_capture_processes", lambda: [])
        monkeypatch.setattr(main, "read_state", lambda: None)

        main._cmd_enable_local_capture(["MyApp"], skip_cert_check=True)
        # The subject is that the command got past the gate and wrote
        # something, not what the list contains. Asserting the exact list
        # pinned the old replace-the-defaults behaviour, so changing that
        # broke a test about certificates.
        assert wrote and "MyApp" in wrote[0]

    def test_a_trusting_machine_is_not_slowed_into_refusing(self, monkeypatch):
        from server import main

        self._refuses(monkeypatch, [])
        wrote = []
        monkeypatch.setattr(main, "set_local_capture_processes", wrote.append)
        monkeypatch.setattr(main, "get_local_capture_processes", lambda: [])
        monkeypatch.setattr(main, "read_state", lambda: None)

        main._cmd_enable_local_capture(["MyApp"])
        # The subject is that the command got past the gate and wrote
        # something, not what the list contains. Asserting the exact list
        # pinned the old replace-the-defaults behaviour, so changing that
        # broke a test about certificates.
        assert wrote and "MyApp" in wrote[0]

    def test_disabling_is_never_refused(self, monkeypatch):
        """Clearing the list stops capture. Refusing it would trap someone in
        the state they are trying to leave -- the same rule the endpoint has."""
        from server import main

        called = []

        async def _missing(_controller):
            called.append(True)
            return [{"udid": "AAAA1111", "name": "iPhone 16 Pro"}]

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", _missing
        )
        main._local_capture_cert_gate([], skip_cert_check=False)
        assert called == [], "an empty list must not even ask"

    def test_a_broken_check_does_not_block_capture(self, monkeypatch, capsys):
        """Fails open, like the preflight it calls. A gate that refuses over
        its own bug is worse than the state it prevents."""
        from server import main

        async def _boom(_controller):
            raise RuntimeError("simctl exploded")

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", _boom
        )
        monkeypatch.setattr(
            "server.device.controller.DeviceController", lambda: object()
        )
        main._local_capture_cert_gate(["MyApp"], skip_cert_check=False)
        assert "Could not check" in capsys.readouterr().out


class TestAutoInstallCertClearsEveryGate:
    """`auto_install_cert` has to mean the same thing everywhere: you are never
    asked again, and nothing refuses you for a certificate.

    It is the answer to a question four gates and one startup check each ask
    independently, and a setting honoured in four of five places is worse than
    one honoured nowhere -- it works until the day it does not, on whichever
    path the user happens to take. The CLI and the startup check are here; the
    three HTTP gates are pinned in test_cert_api.py, where the client fixtures
    live. Each is asserted separately rather than trusting the shared helper,
    because these two do not go through that helper at all.

    The one thing it does not do is make a failed install succeed. That is
    reported, not swallowed: enabling capture that cannot work is the state all
    of this exists to prevent, and it is not improved by the user having opted
    into automatic installation.
    """

    UNTRUSTING = [{"udid": "AAAA1111", "name": "iPhone 16 Pro"}]

    @pytest.fixture(autouse=True)
    def _consent(self, monkeypatch):
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: True)

    @pytest.fixture(autouse=True)
    def _machine_state(self, monkeypatch, tmp_path):
        """Pin what the CLI gate reads off the machine.

        Two dependencies, both introduced by this branch and both invisible
        locally. The CA-existence guard means these tests would pass on a
        developer's Mac (which has ~/.mitmproxy) and fail on CI (which does
        not), reporting the opposite of the behaviour under test. And the gate
        now runs after the no-op early return, so a sibling test that persists
        a capture list into the sandboxed config.json makes this one skip the
        gate entirely -- which is how it passed alone and failed in the suite.
        """
        ca = tmp_path / "mitmproxy-ca-cert.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\n")
        monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: ca)
        from server import main

        monkeypatch.setattr(main, "get_local_capture_processes", lambda: [])
        monkeypatch.setattr(main, "read_state", lambda: None)

    @pytest.fixture
    def _untrusting(self, monkeypatch):
        async def _missing(_controller):
            return list(self.UNTRUSTING)

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", _missing
        )

    @pytest.fixture
    def _installs(self, monkeypatch):
        done = []

        async def _install(_controller, udid, device_name=None):
            done.append(udid)
            return True

        monkeypatch.setattr("server.proxy.cert_manager.install_cert", _install)
        return done

    def test_the_cli_command_is_not_refused(
        self, monkeypatch, _untrusting, _installs, capsys
    ):
        from server import main

        monkeypatch.setattr(
            "server.device.controller.DeviceController", lambda: object()
        )
        wrote = []
        monkeypatch.setattr(main, "set_local_capture_processes", wrote.append)
        monkeypatch.setattr(main, "get_local_capture_processes", lambda: [])
        monkeypatch.setattr(main, "read_state", lambda: None)

        main._cmd_enable_local_capture(["MyApp"])

        # The subject is that the command got past the gate and wrote
        # something, not what the list contains. Asserting the exact list
        # pinned the old replace-the-defaults behaviour, so changing that
        # broke a test about certificates.
        assert wrote and "MyApp" in wrote[0], "the CLI refused despite the setting"
        assert _installs == ["AAAA1111"]

    async def test_the_startup_check_installs_instead_of_warning(
        self, _untrusting, _installs, caplog
    ):
        """The path with no caller to read a warning."""
        with caplog.at_level("WARNING"):
            still_missing = await warn_if_capture_lacks_trust(object(), ["MyApp"])
        assert _installs == ["AAAA1111"]
        assert still_missing == []
        assert "do(es) not trust" not in caplog.text, (
            "it warned about a device it had just fixed"
        )

    async def test_a_failed_install_is_still_reported(self, _untrusting, monkeypatch, caplog):
        """Consent does not make a broken install work, and saying nothing
        would leave capture silently failing for the user who opted in."""
        async def _boom(_controller, udid, device_name=None):
            raise RuntimeError("no CA file")

        monkeypatch.setattr("server.proxy.cert_manager.install_cert", _boom)
        with caplog.at_level("WARNING"):
            still_missing = await warn_if_capture_lacks_trust(object(), ["MyApp"])
        assert [d["udid"] for d in still_missing] == ["AAAA1111"]
        assert "failed" in caplog.text

    def test_a_failed_install_refuses_the_cli_rather_than_proceeding(
        self, monkeypatch, _untrusting, capsys
    ):
        from server import main

        async def _boom(_controller, udid, device_name=None):
            raise RuntimeError("no CA file")

        monkeypatch.setattr("server.proxy.cert_manager.install_cert", _boom)
        monkeypatch.setattr(
            "server.device.controller.DeviceController", lambda: object()
        )
        wrote = []
        monkeypatch.setattr(main, "set_local_capture_processes", wrote.append)

        with pytest.raises(SystemExit) as exc:
            main._cmd_enable_local_capture(["MyApp"])
        assert exc.value.code == 1
        assert wrote == [], "capture was enabled after the install failed"
        assert "installing the CA failed" in capsys.readouterr().out

    def test_it_does_not_refuse_before_the_ca_exists(self, monkeypatch, tmp_path):
        """The CA is generated on the first proxy start, so a fresh machine
        reaches this command without one -- which is the ordering the README's
        own example implies.

        Refusing there would name three ways out of which two cannot be done:
        you cannot install a CA that has not been generated, and
        `set-auto-install-cert on` would then fail on the same missing file.
        Shipping a refusal whose resolutions are unreachable is the exact bug
        this branch exists to fix.
        """
        from server import main

        called = []

        async def _missing(_controller):
            called.append(True)
            return [{"udid": "AAAA1111", "name": "iPhone 16 Pro"}]

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", _missing
        )
        monkeypatch.setattr(
            "server.proxy.cert_manager.get_cert_path",
            lambda: tmp_path / "nope" / "mitmproxy-ca-cert.pem",
        )  # overrides _machine_state
        # No SystemExit, and it does not even ask.
        main._local_capture_cert_gate(["MyApp"], skip_cert_check=False)
        assert called == [], "it queried devices about a CA that does not exist"

    def test_the_skip_flag_is_wired_from_the_command_line(self, monkeypatch):
        """Dropping `args.skip_cert_check` at the dispatch left the whole suite
        green: the flag was parsed, documented and honoured by the function,
        with nothing pinning the wire between them."""
        import sys

        from server import main

        seen = {}
        monkeypatch.setattr(
            main, "_cmd_enable_local_capture",
            lambda processes, skip=False, **kw: seen.update(
                processes=processes, skip=skip or kw.get("skip_cert_check", False)
            ),
        )
        monkeypatch.setattr(
            sys, "argv", ["quern", "enable-local-capture", "MyApp", "--skip-cert-check"],
        )
        main.cli()
        assert seen.get("processes") == ["MyApp"]
        assert seen.get("skip") is True, "the --skip-cert-check flag never reached the command"


class TestAnInstallSurvivesShutdown:
    """The startup check runs in the warmup task, which is cancelled at
    shutdown. `install_cert` runs simctl and then records what it did, and
    cancelling the await does not stop the subprocess that already ran -- so an
    unshielded cancel between those two steps leaves the device trusting a CA
    that `cert-state.json` says is absent.

    Self-correcting, because nothing trusts that record any more and the next
    check asks the device. Still not worth writing deliberately.
    """

    async def test_a_cancel_mid_install_still_records_it(self, monkeypatch):
        recorded = []

        async def _slow_install(_controller, udid, device_name=None):
            await asyncio.sleep(0.05)
            recorded.append(udid)  # stands in for update_cert_state
            return True

        async def _missing(_controller):
            return [{"udid": "AAAA1111", "name": "iPhone 16 Pro"}]

        monkeypatch.setattr(
            "server.proxy.cert_preflight.simulators_without_cert", _missing
        )
        monkeypatch.setattr("server.proxy.cert_manager.install_cert", _slow_install)
        monkeypatch.setattr("server.config.get_auto_install_cert", lambda: True)

        task = asyncio.create_task(warn_if_capture_lacks_trust(object(), ["MyApp"]))
        await asyncio.sleep(0.01)  # let it reach the install
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)  # give the shielded install time to finish

        assert recorded == ["AAAA1111"], (
            "the install was abandoned mid-flight, so simctl may have changed "
            "the TrustStore with nothing written to the record"
        )
