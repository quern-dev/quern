"""A capture must not report started while it is breaking, or blind to, its simulator (#414).

A CI run started a recording against a simulator without quern's CA and lost
about 4½ minutes of logins: every HTTPS request failed and no flow was recorded.
The gaps that let that through:

- `skip_cert_check` decrypted simulators known NOT to trust the CA, so every
  request from them failed while capture reported started;
- starting a recording or a capture session checked nothing about the CA;
- a simulator refusing the proxy's certificate left no trace a recording shows.

The route tests run the real guard and fake only the edge -- the controller,
the trust check, the CA file. A review found the first version patched the
guard itself, so a route hard-wired to `allow_passthrough=True` passed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.models import DeviceType, TlsRejection
from server.proxy import sim_tls
from server.proxy.cert_preflight import SKIP_CERT_CHECK_DEPRECATION, refusal_detail

SIM = "AAAAAAAA-0000-0000-0000-000000000001"
OTHER = "BBBBBBBB-0000-0000-0000-000000000002"


def _controller(kind=DeviceType.SIMULATOR, lookup_error=None):
    c = MagicMock()
    c._ensure_device_type_cached = AsyncMock(side_effect=lookup_error)
    c._device_type = MagicMock(return_value=kind)
    c._active_udid = None
    c.resolve_udid = AsyncMock(side_effect=lambda udid=None: udid)
    return c


def _adapter(*, local_capture=("MyApp",), in_set=(), confirms=True):
    a = MagicMock()
    a.local_capture = list(local_capture)
    a.is_running = True
    a.trusted_simulators = list(in_set)
    a.passthrough_counts = MagicMock(return_value={})
    a.set_trusted_simulators = AsyncMock()
    a.addon_trust_seq = 0
    a.addon_decrypts = AsyncMock(return_value=confirms)
    a._tls_rejections = []
    return a


def _app(*, local_capture=("MyApp",), kind=DeviceType.SIMULATOR, in_set=(SIM,),
         trusted=True, lookup_error=None, confirms=True):
    return SimpleNamespace(state=SimpleNamespace(
        proxy_adapter=_adapter(local_capture=local_capture, in_set=in_set, confirms=confirms),
        device_controller=_controller(kind, lookup_error),
        decrypt_all_simulators=False,
        simulator_trust=[{"udid": SIM, "name": "iPhone 17e", "trusted": trusted}],
    ))


@pytest.fixture
def device(monkeypatch, tmp_path):
    """The edge: what the simulator answers, installs asked of it, the CA file."""
    state = SimpleNamespace(answers=[True], installs=[], install_error=None, auto=False,
                            system_proxy=False, cert=tmp_path / "ca.pem")
    state.cert.write_text("CA")

    async def is_cert_installed(_c, udid, **_kw):
        answer = state.answers.pop(0) if len(state.answers) > 1 else state.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def install_cert(_c, udid, **_kw):
        state.installs.append(udid)
        if state.install_error:
            raise RuntimeError(state.install_error)

    monkeypatch.setattr("server.proxy.cert_manager.is_cert_installed", is_cert_installed)
    monkeypatch.setattr("server.proxy.cert_manager.install_cert", install_cert)
    monkeypatch.setattr("server.proxy.cert_manager._get_device_name",
                        AsyncMock(return_value="iPhone 17e"))
    monkeypatch.setattr("server.proxy.cert_manager.get_cert_path", lambda: state.cert)
    monkeypatch.setattr("server.config.get_auto_install_cert", lambda: state.auto)
    monkeypatch.setattr(sim_tls, "_system_proxy_configured", lambda: state.system_proxy)
    monkeypatch.setattr(sim_tls, "refresh", AsyncMock())
    return state


class TestEnsureCapturable:
    async def test_a_trusting_simulator_already_decrypted_costs_no_refresh(self, device):
        """A refresh makes the addon rebind every simulator it decrypts; one per
        capture session briefly passed every simulator through (review)."""
        check = await sim_tls.ensure_capturable(_app(), SIM)
        assert check.entry.tls == "decrypted" and check.warnings == []
        sim_tls.refresh.assert_not_awaited()

    async def test_a_stale_set_is_refreshed_and_the_addon_must_confirm(self, device):
        app = _app(in_set=())
        await sim_tls.ensure_capturable(app, SIM)
        sim_tls.refresh.assert_awaited_once_with(app)
        app.state.proxy_adapter.addon_decrypts.assert_awaited_once()

    async def test_an_unconfirmed_set_is_said(self, device):
        check = await sim_tls.ensure_capturable(_app(in_set=(), confirms=None), SIM)
        assert any("not yet confirmed" in w for w in check.warnings)

    async def test_an_untrusting_one_is_refused_with_the_ways_out(self, device):
        device.answers = [False]
        with pytest.raises(sim_tls.CaptureNotReady) as exc:
            await sim_tls.ensure_capturable(_app(trusted=False, in_set=()), SIM)
        assert exc.value.status_code == 428
        detail = exc.value.detail
        assert detail["devices"] == [{"udid": SIM, "name": "iPhone 17e"}]
        assert [r["action"] for r in detail["resolutions"]] == [
            "install_proxy_cert", "set_auto_install_cert", "allow_passthrough"]
        assert device.installs == [], "installed without auto_install_cert"

    async def test_auto_install_cert_installs_and_asks_again(self, device):
        device.auto = True
        device.answers = [False, True]
        await sim_tls.ensure_capturable(_app(in_set=()), SIM)
        assert device.installs == [SIM]
        sim_tls.refresh.assert_awaited_once()

    async def test_an_install_the_device_still_denies_is_refused(self, device):
        """Asked again, not assumed: an install that 'succeeded' and changed
        nothing must not start a capture that cannot capture."""
        device.auto = True
        device.answers = [False, False]
        with pytest.raises(sim_tls.CaptureNotReady):
            await sim_tls.ensure_capturable(_app(trusted=False, in_set=()), SIM)

    async def test_an_install_that_fails_is_a_500_as_the_shared_helper_says(self, device):
        device.auto = True
        device.answers = [False]
        device.install_error = "simctl keychain failed"
        with pytest.raises(sim_tls.CaptureNotReady) as exc:
            await sim_tls.ensure_capturable(_app(trusted=False, in_set=()), SIM)
        assert exc.value.status_code == 500
        assert "simctl keychain failed" in exc.value.detail["message"]

    async def test_allow_passthrough_starts_and_says_what_is_in_effect(self, device):
        device.answers = [False]
        check = await sim_tls.ensure_capturable(
            _app(trusted=False, in_set=()), SIM, allow_passthrough=True)
        assert check.entry.tls == "passed_through"

    async def test_a_check_that_cannot_run_lets_it_through_and_says_so(self, device):
        device.answers = [OSError("TrustStore unreadable")]
        check = await sim_tls.ensure_capturable(_app(trusted=None, in_set=()), SIM)
        assert any("could not check" in w for w in check.warnings)

    async def test_a_device_lookup_that_fails_is_not_silent(self, device):
        """It read exactly like "not a simulator" -- a failed check passing as one."""
        check = await sim_tls.ensure_capturable(
            _app(lookup_error=RuntimeError("simctl list failed")), SIM)
        assert check.entry is None
        assert any("could not tell what device" in w for w in check.warnings)

    async def test_no_ca_yet_is_said_not_refused(self, device):
        device.cert.unlink()
        device.answers = [False]
        check = await sim_tls.ensure_capturable(_app(trusted=False), SIM)
        assert any("has not created its CA" in w for w in check.warnings)

    async def test_the_system_proxy_is_guarded_too(self, device):
        """There an untrusting simulator fails, not passes through (review)."""
        device.system_proxy = True
        device.answers = [False]
        app = _app(local_capture=(), trusted=False, in_set=())
        with pytest.raises(sim_tls.CaptureNotReady) as exc:
            await sim_tls.ensure_capturable(app, SIM)
        assert "would fail" in exc.value.detail["message"]
        assert "set_local_capture" in [r["action"] for r in exc.value.detail["resolutions"]]
        check = await sim_tls.ensure_capturable(app, SIM, allow_passthrough=True)
        assert any("fail for as long as this runs" in w for w in check.warnings)

    @pytest.mark.parametrize("app", [
        pytest.param(lambda: _app(local_capture=()), id="no capture at all"),
        pytest.param(lambda: _app(kind=DeviceType.DEVICE), id="not a simulator"),
    ])
    async def test_nothing_to_guard(self, device, app):
        device.answers = [False]
        check = await sim_tls.ensure_capturable(app(), SIM)
        assert check.entry is None and check.warnings == []


class TestTheRefusalNoLongerOffersSkipCertCheck:
    def test_the_shared_refusal(self):
        actions = [r["action"] for r in refusal_detail([{"udid": SIM, "name": "x"}])["resolutions"]]
        assert "skip_cert_check" not in actions
        assert {"set_local_capture", "shutdown_device"} <= set(actions), (
            "the ways out that trust nothing must name real tools")


def _rejection(udid, first: datetime, last: datetime | None = None,
               sni="api.example.com", count=1) -> TlsRejection:
    return TlsRejection(sni=sni, simulator_udid=udid, count=count, id="r",
                        first_at=first.isoformat(), last_at=(last or first).isoformat())


class TestRejectionsAreReported:
    def test_only_this_simulators_and_only_inside_the_window(self):
        now = datetime.now(UTC)
        app = _app()
        app.state.proxy_adapter._tls_rejections = [
            _rejection(SIM, now - timedelta(hours=2), sni="before.example"),
            _rejection(SIM, now, sni="during.example", count=3),
            _rejection(SIM, now + timedelta(hours=2), sni="after.example"),
            _rejection(OTHER, now, sni="other.example"),
        ]
        note = sim_tls.rejection_note(app, SIM, since=(now - timedelta(minutes=5)).isoformat(),
                                      until=(now + timedelta(minutes=5)).isoformat())
        assert note and "3 time(s)" in note and "during.example" in note
        for absent in ("before.example", "after.example", "other.example"):
            assert absent not in note

    def test_one_from_before_the_simulator_trusted_the_ca_is_not_blamed(self):
        """Installing the CA needs no restart, so a refusal from before it would
        go on blaming a certificate that is now fine."""
        now = datetime.now(UTC)
        app = _app()
        app.state.proxy_adapter._tls_rejections = [_rejection(SIM, now - timedelta(minutes=10))]
        app.state.sim_trusted_since = {SIM: (now - timedelta(minutes=1)).isoformat()}
        assert sim_tls.rejection_note(app, SIM) is None

    def test_newly_trusted_is_noted_when_trust_arrives(self):
        app = SimpleNamespace(state=SimpleNamespace(
            simulator_trust=[{"udid": SIM, "name": "x", "trusted": False}]))
        sim_tls._note_newly_trusted(app, [{"udid": SIM, "name": "x", "trusted": True}])
        assert SIM in app.state.sim_trusted_since


# ── the routes, with the real guard ────────────────────────────────────────


def _recording_client(monkeypatch, *, trusted, in_set=()):
    from tests.test_recording import Sources
    from tests.test_recording import _app as recording_app

    app = recording_app(Sources())
    app.state.proxy_adapter = _adapter(in_set=in_set)
    app.state.device_controller = _controller()
    app.state.simulator_trust = [{"udid": SIM, "name": "iPhone 17e", "trusted": trusted}]
    app.state.decrypt_all_simulators = False
    return app, TestClient(app)


class TestTheRecordingRoute:
    def test_an_untrusting_simulator_is_a_428_and_nothing_is_written(
        self, device, monkeypatch, tmp_path,
    ):
        device.answers = [False]
        app, client = _recording_client(monkeypatch, trusted=False)
        with client:
            r = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "kinds": ["flows"]})
            assert r.status_code == 428, r.text
            assert r.json()["detail"]["error"] == "capture_without_cert"
            assert not (tmp_path / "r").exists(), "a refused recording left files behind"
            assert client.get("/api/v1/recordings").json()["recordings"] == []

    def test_allow_passthrough_is_what_lets_it_start(self, device, monkeypatch, tmp_path):
        device.answers = [False]
        app, client = _recording_client(monkeypatch, trusted=False)
        with client:
            r = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "allow_passthrough": True})
            assert r.status_code == 200, r.text
            assert r.json()["simulator_tls"]["tls"] == "passed_through"
            assert any("passed through" in w for w in r.json()["warnings"])

    def test_a_recording_without_flows_checks_nothing(self, device, monkeypatch, tmp_path):
        device.answers = [False]
        app, client = _recording_client(monkeypatch, trusted=False)
        with client:
            r = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "kinds": ["actions"]})
            assert r.status_code == 200

    def test_stop_and_list_carry_the_refusals(self, device, monkeypatch, tmp_path):
        app, client = _recording_client(monkeypatch, trusted=True, in_set=(SIM,))
        with client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r")}).json()["id"]
            app.state.proxy_adapter._tls_rejections = [
                _rejection(SIM, datetime.now(UTC), sni="pinned.example")]
            listed = client.get("/api/v1/recordings").json()["recordings"][0]
            assert any("pinned.example" in w for w in listed["warnings"])
            stopped = client.post(f"/api/v1/recordings/{rid}/stop").json()
            assert any("pinned.example" in w for w in stopped["warnings"])

    def test_flow_results_carry_them(self, device):
        from server.proxy.flow_store import FlowStore

        app = _proxy_app()
        with TestClient(app) as client:
            app.state.flow_store = FlowStore()
            _set_state(app, _adapter(in_set=(SIM,)))
            app.state.proxy_adapter._tls_rejections = [
                _rejection(SIM, datetime.now(UTC), sni="pinned.example")]
            r = client.get(f"/api/v1/proxy/flows?simulator_udid={SIM}", headers=AUTH)
            assert r.status_code == 200, r.text
            assert "pinned.example" in (r.json().get("simulator_tls_note") or "")


def _proxy_app():
    from server.config import ServerConfig
    from server.main import create_app

    return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                      enable_crash=False, enable_proxy=False)


def _set_state(app, adapter):
    """After startup, which installs its own controller and adapter."""
    app.state.device_controller = _controller()
    app.state.simulator_trust = [{"udid": SIM, "name": "iPhone 17e", "trusted": False}]
    app.state.decrypt_all_simulators = False
    app.state.proxy_adapter = adapter


AUTH = {"Authorization": "Bearer k"}


class TestTheCaptureSessionRoute:
    def test_refused_without_a_session_left_behind(self, device):
        device.answers = [False]
        app = _proxy_app()
        with TestClient(app) as client:
            _set_state(app, _adapter())
            r = client.post("/api/v1/proxy/capture/start", headers=AUTH,
                            json={"simulator_udid": SIM})
            assert r.status_code == 428, r.text
            assert not app.state.capture_sessions._sessions, "a refused start left a session"

    def test_allow_passthrough_is_what_lets_it_start(self, device):
        device.answers = [False]
        app = _proxy_app()
        with TestClient(app) as client:
            _set_state(app, _adapter())
            r = client.post("/api/v1/proxy/capture/start", headers=AUTH,
                            json={"simulator_udid": SIM, "allow_passthrough": True})
            assert r.status_code == 200, r.text


def _running_adapter():
    a = MagicMock()
    a.is_running = False
    a.listen_host, a.listen_port = "0.0.0.0", 9101
    a.started_at = a._intercept_pattern = a._active_filter = None
    a._mock_rules, a._held_flows, a._error = [], {}, None
    a.get_bypass_patterns = MagicMock(return_value=[])
    a.local_capture = []
    a.passthrough_counts = MagicMock(return_value={})
    a.trusted_simulators = []
    a.start = AsyncMock()
    a.reconfigure = MagicMock()
    return a


class TestSkipCertCheckIsDeprecated:
    def test_start_proxy_says_so(self, monkeypatch):
        app = _proxy_app()
        monkeypatch.setattr("server.api.proxy.update_state", lambda **_k: None)
        with TestClient(app) as client:
            _set_state(app, _running_adapter())
            r = client.post("/api/v1/proxy/start", headers=AUTH, json={"skip_cert_check": True})
            assert r.status_code == 200, r.text
            assert r.json()["deprecations"] == [SKIP_CERT_CHECK_DEPRECATION]
            app.state.proxy_adapter.is_running = False
            r = client.post("/api/v1/proxy/start", headers=AUTH, json={})
            assert r.json()["deprecations"] is None

    def test_configure_system_says_so(self, monkeypatch):
        app = _proxy_app()
        snap = SimpleNamespace(interface="Wi-Fi", http_proxy_enabled=False, to_dict=lambda: {})
        monkeypatch.setattr("server.lifecycle.state.read_state", lambda: {})
        monkeypatch.setattr("server.api.proxy.update_state", lambda **_k: None)
        monkeypatch.setattr("server.api.proxy.detect_and_configure", lambda *a: snap)
        with TestClient(app) as client:
            adapter = _running_adapter()
            adapter.is_running = True
            _set_state(app, adapter)
            r = client.post("/api/v1/proxy/configure-system", headers=AUTH,
                            json={"skip_cert_check": True})
            assert r.status_code == 200, r.text
            assert r.json()["deprecations"] == [SKIP_CERT_CHECK_DEPRECATION]

    def test_set_local_capture_says_so(self, monkeypatch):
        app = _proxy_app()
        monkeypatch.setattr("server.api.proxy._ensure_ca_is_trusted", AsyncMock())
        monkeypatch.setattr("server.config.set_local_capture_processes", lambda _p: None)
        with TestClient(app) as client:
            _set_state(app, _running_adapter())
            app.state.local_capture_processes = []
            r = client.post("/api/v1/proxy/local-capture", headers=AUTH,
                            json={"processes": ["MyApp"], "skip_cert_check": True})
            assert r.status_code == 200, r.text
            assert r.json()["deprecations"] == [SKIP_CERT_CHECK_DEPRECATION]

    def test_the_cli_flag_says_so(self, capsys):
        """A list starting with an exclusion is refused right after the warning,
        before anything is written: no config, no signal to a running server."""
        from server import main

        with pytest.raises(SystemExit):
            main._cmd_enable_local_capture(["!123"], skip_cert_check=True)
        assert "skip_cert_check is deprecated" in capsys.readouterr().err

    def test_the_schema_marks_it(self):
        from server.models import ConfigureSystemProxyRequest

        prop = ConfigureSystemProxyRequest.model_json_schema()["properties"]["skip_cert_check"]
        assert prop.get("deprecated") is True


class TestTheRecordCli:
    def test_allow_passthrough_is_sent(self):
        from server.recording import cli

        sent = {}

        def call(method, path, body=None):
            sent.update(body=body)
            return 200, {"id": "rec_1", "udid": SIM, "output_dir": "/x", "warnings": []}

        with patch.object(cli, "_call", call):
            assert cli.main(["start", "--udid", SIM, "--allow-passthrough"]) == 0
        assert sent["body"]["allow_passthrough"] is True

    def test_a_refusal_reads_as_words(self, capsys):
        from server.recording import cli

        detail = refusal_detail([{"udid": SIM, "name": "iPhone 17e"}])
        with patch.object(cli, "_call", lambda m, p, b=None: (428, {"detail": detail})):
            assert cli.main(["start", "--udid", SIM]) == 2
        err = capsys.readouterr().err
        assert "do not trust the mitmproxy CA" in err and "install_proxy_cert" in err
        assert "{" not in err
