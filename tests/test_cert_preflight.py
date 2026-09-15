"""Tests for the cert preflight on enabling the system proxy.

The failure this prevents: capture through a device that does not trust the
mitmproxy CA fails every HTTPS request, and the symptom -- a blank screen, an
app with no network -- points nowhere near the proxy.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.models import DeviceCertState, DeviceInfo, DeviceState, DeviceType
from server.proxy.cert_preflight import (
    refusal_detail,
    simulators_without_cert,
    trust_is_stale,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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


class TestEveryRefusalNamesOnlyReachableActions:
    """`skip_cert_check` is one of the three ways out the 428 offers, so every
    path that can raise it has to accept it -- over HTTP *and* through the MCP
    tool an agent actually holds.

    A refusal that names an action the caller cannot take is worse than one
    that offers fewer options. The agent has been told the request is fine and
    the world is not ready for it, and the one resolution that does not require
    the user's consent is the one it cannot reach; what is left is retrying a
    refusal that will never clear on its own.

    `set_local_capture` shipped precisely that gap. The gate and the request
    field were both added to the endpoint, and the MCP tool's schema -- which
    is `strictParams`, so `.strict()` -- was not, meaning the field the error
    message names was rejected by validation before a request was ever made.
    The sibling path `configure_system_proxy` had it from the start, which is
    what made the omission easy to miss.
    """

    MCP_TOOLS_DIR = REPO_ROOT / "mcp" / "src" / "tools"
    PROXY_API = REPO_ROOT / "server" / "api" / "proxy.py"
    GATE = "_ensure_ca_is_trusted"

    def _gated_routes(self) -> dict[str, str]:
        """{full path: body model name} for handlers that call the gate.

        Read from the source rather than listed here on purpose: a third
        capture path added later is caught by this test rather than by the
        person who hits the refusal.
        """
        tree = ast.parse(self.PROXY_API.read_text())
        prefix = re.search(
            r'APIRouter\(\s*prefix="([^"]*)"', self.PROXY_API.read_text()
        ).group(1)

        routes: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            calls = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if self.GATE not in calls:
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and dec.args):
                    continue
                path = getattr(dec.args[0], "value", None)
                if not isinstance(path, str):
                    continue
                # The body model, unwrapped from `Model | None`.
                model = None
                for arg in node.args.args:
                    if arg.arg != "body" or arg.annotation is None:
                        continue
                    ann = arg.annotation
                    if isinstance(ann, ast.BinOp):
                        ann = ann.left
                    model = getattr(ann, "id", None)
                routes[prefix + path] = model
        return routes

    def _tool_blocks(self) -> dict[str, str]:
        """{tool name: its registerTool(...) source}, split at each call."""
        blocks: dict[str, str] = {}
        for ts_file in sorted(self.MCP_TOOLS_DIR.glob("*.ts")):
            chunks = ts_file.read_text().split("server.registerTool(")
            for chunk in chunks[1:]:
                name = re.match(r'\s*"([a-z_0-9]+)"', chunk)
                if name:
                    blocks[name.group(1)] = chunk
        return blocks

    def test_the_gate_guards_more_than_one_endpoint(self):
        """Guards the parse. A regex that silently matched nothing would make
        every assertion below vacuous, and the whole point is that this is a
        shared gate rather than one endpoint's business."""
        routes = self._gated_routes()
        assert len(routes) >= 2, f"expected the shared gate on both capture paths, got {routes}"
        assert "/api/v1/proxy/local-capture" in routes
        assert "/api/v1/proxy/configure-system" in routes

    def test_every_gated_endpoint_accepts_the_parameter(self):
        """The HTTP half: the model behind the body has the field."""
        import server.models as models

        for path, model_name in self._gated_routes().items():
            assert model_name, f"{path} is gated but takes no typed body"
            model = getattr(models, model_name)
            assert "skip_cert_check" in model.model_fields, (
                f"{path} can refuse with 428 naming skip_cert_check, but "
                f"{model_name} has no such field"
            )

    def test_every_gated_endpoint_exposes_the_parameter_to_agents(self):
        """The half that was missing. An MCP tool posting to a gated path must
        take `skip_cert_check`, or the resolution is unreachable for the caller
        the refusal was written for."""
        blocks = self._tool_blocks()
        for path in self._gated_routes():
            posting = {
                name: src for name, src in blocks.items() if f'"{path}"' in src
            }
            assert posting, f"no MCP tool posts to {path}"
            for name, src in posting.items():
                schema = src.split("inputSchema:", 1)
                assert len(schema) == 2, f"{name} has no inputSchema to check"
                # Cut at the handler so a mention in the body cannot pass for
                # a declared parameter.
                declared = schema[1].split("}, async", 1)[0]
                assert "skip_cert_check" in declared, (
                    f"{name} posts to {path}, which refuses with 428 and tells "
                    f"the caller to pass skip_cert_check -- but the tool's "
                    f"schema is strict and does not accept it"
                )
