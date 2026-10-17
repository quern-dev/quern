"""The LAN viewer (#449): settings, tokens, the listener's app, and the API.

The first quern listener meant to be reached from another machine, so the
refusals are tested as carefully as the happy path.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from server import config as config_mod
from server.viewer.app import create_viewer_app
from server.viewer.grants import ViewerGrants
from server.viewer.listener import start_viewer

LAN = "192.168.1.50"


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "USER_CONFIG_FILE", path)

    def write(obj):
        path.write_text(json.dumps(obj))

    return write


class TestSettings:
    def test_off_by_default(self, user_config):
        user_config({})
        s = config_mod.viewer_settings()
        assert (s["enabled"], s["host"], s["port"], s["grace_seconds"]) == (
            False,
            "0.0.0.0",
            9120,
            600,
        )

    @pytest.mark.parametrize("value", ["true", 1, "yes", None, {}])
    def test_only_a_literal_true_enables_it(self, user_config, value):
        user_config({"viewer": {"enabled": value}})
        assert config_mod.viewer_settings()["enabled"] is False

    def test_bind_hosts_and_jmuxer(self, user_config):
        user_config(
            {
                "viewer": {
                    "enabled": True,
                    "bind": "192.168.1.50:9200",
                    "hosts": ["ci.lan", 7, ""],
                    "jmuxer_path": "/x/jmuxer.js",
                }
            }
        )
        s = config_mod.viewer_settings()
        assert (s["enabled"], s["host"], s["port"], s["hosts"], s["jmuxer_path"]) == (
            True,
            "192.168.1.50",
            9200,
            ["ci.lan"],
            "/x/jmuxer.js",
        )

    @pytest.mark.parametrize("bind", ["9120", "host:", "host:0", "host:70000", "host:abc"])
    def test_a_malformed_bind_falls_back_and_says_so(self, user_config, bind):
        user_config({"viewer": {"enabled": True, "bind": bind}})
        s = config_mod.viewer_settings()
        assert (s["host"], s["port"]) == ("0.0.0.0", 9120)
        assert bind in s["bind_error"]


def _recording(state="recording", stopped_at=None, video=True, port=0, udid="SIM-A"):
    return SimpleNamespace(
        state=state,
        stopped_at=stopped_at,
        filters=SimpleNamespace(video=video),
        port=port,
        udid=udid,
    )


class _Recordings:
    """A RecordingManager stand-in with the viewer's three calls."""

    def __init__(self, **recs):
        self.recs = recs
        self.keyframes = []

    def get(self, rid):
        if rid not in self.recs:
            raise KeyError(rid)
        return self.recs[rid]

    def viewer_stream(self, rid):
        rec = self.recs.get(rid)
        if rec is None or rec.state != "recording" or not rec.port:
            return None
        return rec.port, rec.udid

    async def viewer_keyframe(self, rid):
        self.keyframes.append(rid)
        return rid in self.recs and self.recs[rid].state == "recording"


class TestGrants:
    def test_issue_look_up_revoke(self):
        grants = ViewerGrants(_Recordings(r1=_recording()), grace_seconds=600)
        token = grants.issue("r1")
        assert len(token) >= 40
        assert grants.recording_for(token) == "r1"
        assert grants.recording_for(token + "x") is None
        assert grants.revoke("r1") == 1
        assert grants.recording_for(token) is None

    def test_only_a_hash_is_kept(self):
        grants = ViewerGrants(_Recordings(r1=_recording()), grace_seconds=600)
        token = grants.issue("r1")
        assert token not in repr(grants._by_digest)

    def test_it_expires_after_the_grace_from_the_recordings_own_stop(self):
        stopped = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        recs = _Recordings(r1=_recording(state="stopped", stopped_at=stopped))
        grants = ViewerGrants(recs, grace_seconds=600)
        token = grants.issue("r1")
        assert grants.recording_for(token, now=stopped + timedelta(seconds=599)) == "r1"
        assert grants.recording_for(token, now=stopped + timedelta(seconds=601)) is None
        assert grants.count("r1") == 0, "an expired token is pruned"

    def test_a_recording_the_server_no_longer_knows_is_not_viewable(self):
        grants = ViewerGrants(_Recordings(), grace_seconds=600)
        assert grants.recording_for(grants.issue("gone")) is None

    @pytest.mark.parametrize("token", ["", "x" * 129])
    def test_absurd_tokens_are_refused_outright(self, token, monkeypatch):
        """Refused before hashing: an attacker's path segment can be long."""
        from server.viewer import grants as grants_mod

        hashed = []
        real = grants_mod._digest
        monkeypatch.setattr(grants_mod, "_digest", lambda t: hashed.append(t) or real(t))
        assert ViewerGrants(_Recordings(), grace_seconds=600).recording_for(token) is None
        assert hashed == []


class _FakeQuernMedia:
    """A loopback /frames server, standing in for a recording's quern-media."""

    def __init__(
        self,
        status=200,
        body=b"--quernframe\r\nContent-Type: video/h264\r\nContent-Length: 3\r\n\r\nabc\r\n",
        error_body=b"another simulator is on this port",
        hold=False,
    ):
        """`hold`: after the body, keep the stream open with keepalives until
        the reader goes away, counting each one that does in `closed`."""
        outer = self
        self.paths = []
        self.closed = 0

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                self.send_response(status)
                self.send_header(
                    "Content-Type", "application/octet-stream" if status == 200 else "text/plain"
                )
                self.end_headers()
                self.wfile.write(body if status == 200 else error_body)
                if not hold:
                    return
                try:
                    for _ in range(400):  # 20s at most
                        time.sleep(0.05)
                        self.wfile.write(b"--quernframe\r\nContent-Length: 0\r\n\r\n\r\n")
                        self.wfile.flush()
                except OSError:
                    outer.closed += 1

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def _client(recs, *, jmuxer_path=None, hosts=(LAN,)):
    grants = ViewerGrants(recs, grace_seconds=600)
    app = create_viewer_app(
        recordings=recs, grants=grants, allowed_hosts=lambda: set(hosts), jmuxer_path=jmuxer_path
    )
    return TestClient(app, base_url=f"http://{LAN}:9120"), grants


class TestTheListenersApp:
    def test_an_unknown_token_is_404_everywhere(self):
        client, _ = _client(_Recordings(r1=_recording()))
        for method, path in [
            ("GET", "/v/nope/"),
            ("GET", "/v/nope/frames"),
            ("POST", "/v/nope/keyframe"),
            ("GET", "/v/nope/fmp4.js"),
        ]:
            assert client.request(method, path).status_code == 404, path

    def test_no_api_route_is_reachable(self):
        client, grants = _client(_Recordings(r1=_recording()))
        token = grants.issue("r1")
        for path in [
            "/api/v1/recordings",
            "/health",
            "/docs",
            f"/v/{token}/../../api/v1/recordings",
        ]:
            assert client.get(path).status_code == 404, path

    def test_the_page_is_served_under_the_token_directory(self):
        client, grants = _client(_Recordings(r1=_recording()))
        token = grants.issue("r1")
        r = client.get(f"/v/{token}", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].endswith(f"/v/{token}/")
        page = client.get(f"/v/{token}/")
        assert page.status_code == 200 and "quern viewer" in page.text
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "Fmp4Muxer" in client.get(f"/v/{token}/fmp4.js").text

    def test_host_origin_and_cross_site_checks(self):
        client, grants = _client(_Recordings(r1=_recording()))
        token = grants.issue("r1")
        assert (
            client.get(f"/v/{token}/", headers={"Host": "rebind.attacker.example"}).status_code
            == 421
        )
        assert (
            client.get(f"/v/{token}/", headers={"Origin": "http://evil.example"}).status_code == 403
        )
        assert (
            client.get(f"/v/{token}/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        )
        assert (
            client.get(f"/v/{token}/", headers={"Origin": f"http://{LAN}:9120"}).status_code == 200
        )
        assert client.get(f"/v/{token}/", headers={"Host": "localhost:9120"}).status_code == 200

    def test_frames_relays_the_recordings_stream_by_source(self):
        upstream = _FakeQuernMedia()
        try:
            client, grants = _client(_Recordings(r1=_recording(port=upstream.port, udid="SIM-A")))
            r = client.get(f"/v/{grants.issue('r1')}/frames")
            assert r.status_code == 200
            assert r.headers["content-type"] == "application/octet-stream"
            assert r.content.endswith(b"abc\r\n")
            # By source, so a port another simulator took answers 409 (#425).
            assert upstream.paths == ["/frames?source=SIM-A"]
        finally:
            upstream.close()

    def test_frames_reports_a_refusal_as_a_bad_gateway(self):
        upstream = _FakeQuernMedia(status=409)
        try:
            client, grants = _client(_Recordings(r1=_recording(port=upstream.port)))
            r = client.get(f"/v/{grants.issue('r1')}/frames")
            assert r.status_code == 502 and "409" in r.text and "another simulator" in r.text
        finally:
            upstream.close()

    def test_frames_when_not_filming(self):
        client, grants = _client(_Recordings(r1=_recording(port=0)))
        assert client.get(f"/v/{grants.issue('r1')}/frames").status_code == 503

    def test_keyframe_asks_without_marking(self):
        recs = _Recordings(r1=_recording())
        client, grants = _client(recs)
        assert client.post(f"/v/{grants.issue('r1')}/keyframe").status_code == 204
        assert recs.keyframes == ["r1"]

    def test_jmuxer_only_when_configured(self, tmp_path):
        recs = _Recordings(r1=_recording())
        client, grants = _client(recs)
        token = grants.issue("r1")
        assert client.get(f"/v/{token}/jmuxer.js").status_code == 404
        copy = tmp_path / "jmuxer.min.js"
        copy.write_text("window.JMuxer = function () {};")
        client2, grants2 = _client(recs, jmuxer_path=str(copy))
        assert client2.get(f"/v/{grants2.issue('r1')}/jmuxer.js").text.startswith("window.JMuxer")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestTheListener:
    async def test_off_means_no_listener(self):
        assert await start_viewer(_Recordings(), {"enabled": False}) is None

    async def test_it_listens_and_links_point_at_it(self):
        port = _free_port()
        viewer = await start_viewer(
            _Recordings(r1=_recording()),
            {
                "enabled": True,
                "host": "127.0.0.1",
                "port": port,
                "hosts": [],
                "jmuxer_path": None,
                "grace_seconds": 600,
                "bind_error": None,
            },
        )
        try:
            assert viewer.base_url == f"http://127.0.0.1:{port}"
            url = viewer.url_for(viewer.grants.issue("r1"))
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            path = url.split(str(port), 1)[1]
            writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            await writer.drain()
            head = (await reader.read(200)).decode()
            writer.close()
            assert head.startswith("HTTP/1.1 200")
        finally:
            await viewer.stop()

    async def test_a_busy_port_is_reported_not_moved(self):
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen()
            port = held.getsockname()[1]
            viewer = await start_viewer(
                _Recordings(),
                {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": port,
                    "hosts": [],
                    "jmuxer_path": None,
                    "grace_seconds": 600,
                    "bind_error": None,
                },
            )
        assert viewer.base_url is None and str(port) in viewer.error
        assert viewer.url_for("anything") is None


# ── the recorder's viewer helpers, on a real RecordingManager ───────────────


class TestTheRecorderHelpers:
    async def test_stream_is_looked_up_live_and_keyframes_leave_no_mark(
        self, tmp_path, monkeypatch
    ):
        from server.recording.recorder import Filters
        from tests.test_recording_video import SIM, FakeVideo, Sources, _events, _settle

        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")
        video = FakeVideo()
        manager = Sources().manager(video=video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("actions",), video=True))
        rec._segment.port = 8490
        assert manager.viewer_stream(rec.id) == (8490, SIM)
        assert manager.viewer_stream("nope") is None
        # A restart's new segment, or a quern-media moved to another port, is
        # seen at once: nothing caches the port.
        rec._segment.port = 8491
        assert manager.viewer_stream(rec.id) == (8491, SIM)

        assert await manager.viewer_keyframe(rec.id) is True
        assert video.keyframes, "the movie was asked"
        await _settle()
        await manager.stop(rec.id)
        marks = [e for e in _events(tmp_path / "r") if e.get("type") == "mark"]
        assert marks == [], "someone watching must not write into the recording"
        assert manager.viewer_stream(rec.id) is None, "a stopped recording isn't filming"
        assert await manager.viewer_keyframe(rec.id) is False

    async def test_viewer_keyframes_are_throttled_and_not_counted(self, tmp_path, monkeypatch):
        from server.recording import recorder as recorder_mod
        from server.recording.recorder import Filters
        from tests.test_recording_video import SIM, FakeVideo, Sources

        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")
        now = [1000.0]
        monkeypatch.setattr(recorder_mod, "_monotonic", lambda: now[0])
        video = FakeVideo()
        manager = Sources().manager(video=video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("actions",), video=True))
        assert await manager.viewer_keyframe(rec.id) is True
        assert await manager.viewer_keyframe(rec.id) is None, "within the interval"
        now[0] += recorder_mod.VIEWER_KEYFRAME_INTERVAL
        assert await manager.viewer_keyframe(rec.id) is True
        assert video.keyframe_counted == [False, False], "asked uncounted, twice"
        await manager.stop(rec.id)


class TestUncountedKeyframes:
    async def test_counted_false_leaves_the_segment_counts_alone(self):
        from server.recording.video import Segment, VideoRecorder

        seg = Segment(
            path=None, udid="SIM", process=SimpleNamespace(returncode=None), port=_free_port()
        )  # nothing listens: it fails
        video = VideoRecorder(binary=None)
        assert await video.keyframe(seg, counted=False) is False
        assert (seg.keyframes_requested, seg.keyframes_failed) == (0, 0)
        assert await video.keyframe(seg) is False
        assert (seg.keyframes_requested, seg.keyframes_failed) == (1, 1)


# ── the API ─────────────────────────────────────────────────────────────────


def _api_request(viewer=None, recordings=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(viewer=viewer, recordings=recordings, device_controller=None)
        )
    )


class TestTheAPI:
    async def test_a_viewer_needs_video(self):
        from fastapi import HTTPException

        from server.api.recordings import start_recording
        from server.models import RecordingStartRequest

        with pytest.raises(HTTPException) as e:
            await start_recording(
                _api_request(recordings=_Recordings()),
                RecordingStartRequest(udid="SIM-A", viewer=True),
            )
        assert e.value.status_code == 400 and "video" in e.value.detail

    def test_a_link_says_why_when_there_is_none(self):
        from server.api.recordings import _issue_viewer_link

        url, why = _issue_viewer_link(_api_request(viewer=None), "r1")
        assert url is None and "viewer.enabled" in why
        broken = SimpleNamespace(base_url=None, error="could not listen on 0.0.0.0:9120")
        url, why = _issue_viewer_link(_api_request(viewer=broken), "r1")
        assert url is None and "9120" in why

    def test_a_link_when_the_viewer_is_running(self):
        from server.api.recordings import _issue_viewer_link
        from server.viewer.listener import ViewerListener

        viewer = ViewerListener(
            grants=ViewerGrants(_Recordings(r1=_recording()), grace_seconds=600),
            port=9120,
            lan=lambda: LAN,
        )
        url, why = _issue_viewer_link(_api_request(viewer=viewer), "r1")
        assert why is None and url.startswith(f"http://{LAN}:9120/v/") and url.endswith("/")
        token = url.rstrip("/").rsplit("/", 1)[1]
        assert viewer.grants.recording_for(token) == "r1"

    async def test_more_links_only_while_filming_and_revoke_all(self):
        from fastapi import HTTPException

        from server.api.recordings import recording_viewer_link, recording_viewer_revoke
        from server.viewer.listener import ViewerListener

        class Manager:
            def get(self, rid):
                if rid == "filming":
                    return SimpleNamespace(filters=SimpleNamespace(video=True), state="recording")
                if rid == "stopped":
                    return SimpleNamespace(filters=SimpleNamespace(video=True), state="stopped")
                from server.recording.recorder import RecordingError

                raise RecordingError(f"no recording {rid}")

        viewer = ViewerListener(
            grants=ViewerGrants(_Recordings(filming=_recording()), grace_seconds=600),
            port=9120,
            lan=lambda: LAN,
        )
        req = _api_request(viewer=viewer, recordings=Manager())
        first = await recording_viewer_link(req, "filming")
        second = await recording_viewer_link(req, "filming")
        assert first["viewer_url"] != second["viewer_url"]
        with pytest.raises(HTTPException) as e:
            await recording_viewer_link(req, "stopped")
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:
            await recording_viewer_link(req, "nope")
        assert e.value.status_code == 404
        assert (await recording_viewer_revoke(req, "filming"))["revoked"] == 2


# ── the security review's findings ──────────────────────────────────────────


class TestSettingsParsing:
    @pytest.mark.parametrize(
        "bind,host,port",
        [
            ("[::]:9120", "::", 9120),
            ("[fe80::1]:9200", "fe80::1", 9200),
            (":9300", "0.0.0.0", 9300),
        ],
    )
    def test_ipv6_binds_in_brackets(self, user_config, bind, host, port):
        user_config({"viewer": {"enabled": True, "bind": bind}})
        s = config_mod.viewer_settings()
        assert (s["host"], s["port"], s["bind_error"]) == (host, port, None)

    @pytest.mark.parametrize("bind", ["::1:9120", "[::1:9120", "[[::1]]:9120"])
    def test_an_unbracketed_or_broken_ipv6_bind_is_refused(self, user_config, bind):
        user_config({"viewer": {"enabled": True, "bind": bind}})
        s = config_mod.viewer_settings()
        assert (s["host"], s["port"]) == ("0.0.0.0", 9120) and s["bind_error"]

    def test_hosts_lose_their_ports(self, user_config):
        user_config({"viewer": {"hosts": ["CI.lan:9120", "[fe80::1]:9120", "mac.local", "[bad"]}})
        assert config_mod.viewer_settings()["hosts"] == ["ci.lan", "fe80::1", "mac.local"]


def _raw_guarded(app, headers):
    """Call the guarded app with exactly these headers -- TestClient always
    adds a Host -- and return the status."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v/x/",
        "raw_path": b"/v/x/",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "http_version": "1.1",
        "server": (LAN, 9120),
        "client": ("192.168.1.9", 5000),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


class TestTheGuards:
    def test_no_host_at_all_is_refused(self):
        recs = _Recordings(r1=_recording())
        app = create_viewer_app(
            recordings=recs,
            grants=ViewerGrants(recs, grace_seconds=600),
            allowed_hosts=lambda: {LAN},
        )
        assert _raw_guarded(app, []) == 421
        assert _raw_guarded(app, [(b"host", b"")]) == 421

    def test_origin_null_is_refused(self):
        client, grants = _client(_Recordings(r1=_recording()))
        r = client.get(f"/v/{grants.issue('r1')}/", headers={"Origin": "null"})
        assert r.status_code == 403

    def test_header_case_does_not_get_past_them(self):
        client, grants = _client(_Recordings(r1=_recording()))
        token = grants.issue("r1")
        assert (
            client.get(f"/v/{token}/", headers={"Sec-Fetch-Site": "Cross-Site"}).status_code == 403
        )
        assert client.get(f"/v/{token}/", headers={"Host": "LOCALHOST:9120"}).status_code == 200
        assert (
            client.get(f"/v/{token}/", headers={"Host": "192.168.1.50:9120".upper()}).status_code
            == 200
        )
        assert (
            client.get(f"/v/{token}/", headers={"Origin": "HTTP://EVIL.example"}).status_code == 403
        )

    def test_the_allowed_hosts_follow_the_lan_address_and_never_a_wildcard(self):
        from server.viewer.listener import allowed_hosts

        now = {"lan": LAN}
        hosts = allowed_hosts("0.0.0.0", ["ci.lan"], lambda: now["lan"])
        assert hosts() == {LAN, "ci.lan"}
        now["lan"] = "10.0.0.7"
        assert hosts() == {"10.0.0.7", "ci.lan"}, "a Mac that moved networks"
        for wildcard in ("0.0.0.0", "::"):
            assert wildcard not in allowed_hosts(wildcard, [], lambda w=wildcard: w)()
        assert allowed_hosts("10.1.1.1", [], lambda: "10.1.1.1")() == {"10.1.1.1"}

    def test_the_allowed_hosts_are_asked_per_request(self):
        recs = _Recordings(r1=_recording())
        grants = ViewerGrants(recs, grace_seconds=600)
        now = {LAN}
        app = create_viewer_app(recordings=recs, grants=grants, allowed_hosts=lambda: now)
        client = TestClient(app, base_url="http://10.0.0.7:9120")
        token = grants.issue("r1")
        assert client.get(f"/v/{token}/").status_code == 421
        now = {"10.0.0.7"}  # noqa: F841 -- read by the lambda
        assert client.get(f"/v/{token}/").status_code == 200


class TestThePage:
    def test_a_csp_and_no_inline_script(self):
        client, grants = _client(_Recordings(r1=_recording()))
        token = grants.issue("r1")
        page = client.get(f"/v/{token}/")
        csp = page.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp.split("style-src")[0]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert '<script type="module" src="viewer.js"></script>' in page.text
        assert page.text.count("<script") == 1, "no inline script for the CSP to forbid"
        js = client.get(f"/v/{token}/viewer.js")
        assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
        assert 'from "./fmp4.js"' in js.text and "textContent = await" not in js.text
        assert client.get("/v/nope/viewer.js").status_code == 404


class TestTheRelay:
    def test_head_opens_no_relay(self):
        upstream = _FakeQuernMedia()
        try:
            client, grants = _client(_Recordings(r1=_recording(port=upstream.port)))
            assert client.head(f"/v/{grants.issue('r1')}/frames").status_code == 405
            assert upstream.paths == []
        finally:
            upstream.close()

    def test_an_upstream_404_is_not_read_as_an_expired_link(self):
        upstream = _FakeQuernMedia(status=404, error_body=b"no such source")
        try:
            client, grants = _client(_Recordings(r1=_recording(port=upstream.port)))
            r = client.get(f"/v/{grants.issue('r1')}/frames")
            assert r.status_code == 502 and "no such source" in r.text
        finally:
            upstream.close()

    def test_an_upstream_error_body_is_bounded(self):
        upstream = _FakeQuernMedia(status=500, error_body=b"x" * 100_000)
        try:
            client, grants = _client(_Recordings(r1=_recording(port=upstream.port)))
            r = client.get(f"/v/{grants.issue('r1')}/frames")
            assert r.status_code == 502 and len(r.content) < 400
        finally:
            upstream.close()

    def test_keyframes_are_throttled(self):
        class Throttled(_Recordings):
            async def viewer_keyframe(self, rid):
                return None

        client, grants = _client(Throttled(r1=_recording()))
        r = client.post(f"/v/{grants.issue('r1')}/keyframe")
        assert r.status_code == 429 and r.headers["retry-after"] == "1"


_SETTINGS = {
    "enabled": True,
    "host": "127.0.0.1",
    "hosts": [],
    "jmuxer_path": None,
    "grace_seconds": 600,
    "bind_error": None,
}


@pytest.fixture
def no_fd_limit(monkeypatch):
    """The listener raises the process's file-descriptor limit; not the test
    process's."""
    from server.viewer import listener

    monkeypatch.setattr(listener, "raise_fd_limit", lambda *a: None)


async def _open_stream(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    await writer.drain()
    head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode()
    return reader, writer, head


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _ends(reader, timeout=5.0):
    """True when the server ends the response (EOF or the chunked terminator)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = await asyncio.wait_for(reader.read(65536), 0.5)
        except TimeoutError:
            continue
        except OSError:
            return True
        if not data or data.endswith(b"0\r\n\r\n"):
            return True
    return False


@pytest.mark.usefixtures("no_fd_limit")
class TestTheLiveListener:
    async def _viewer(self, recs):
        port = _free_port()
        viewer = await start_viewer(recs, {**_SETTINGS, "port": port})
        assert viewer.error is None
        return viewer, port

    async def test_revoking_ends_a_stream_already_open(self, monkeypatch):
        from server.viewer import app as app_mod

        monkeypatch.setattr(app_mod, "REVALIDATE_SECONDS", 0.2)
        upstream = _FakeQuernMedia(hold=True)
        recs = _Recordings(r1=_recording(port=upstream.port))
        viewer, port = await self._viewer(recs)
        try:
            token = viewer.grants.issue("r1")
            reader, writer, head = await _open_stream(port, f"/v/{token}/frames")
            assert head.startswith("HTTP/1.1 200")
            await asyncio.sleep(0.4)
            assert not reader.at_eof(), "still streaming before the revoke"
            viewer.grants.revoke("r1")
            assert await _ends(reader), "the open stream ended"
            assert await _until(lambda: upstream.closed == 1), "and let go of quern-media"
            writer.close()
        finally:
            await viewer.stop()
            upstream.close()

    async def test_a_viewer_leaving_closes_the_upstream_and_frees_its_slot(self, monkeypatch):
        from server.viewer import app as app_mod

        monkeypatch.setattr(app_mod, "MAX_STREAMS_PER_RECORDING", 1)
        upstream = _FakeQuernMedia(hold=True)
        viewer, port = await self._viewer(_Recordings(r1=_recording(port=upstream.port)))
        try:
            token = viewer.grants.issue("r1")
            _, writer, head = await _open_stream(port, f"/v/{token}/frames")
            assert head.startswith("HTTP/1.1 200")
            r2, w2, head2 = await _open_stream(port, f"/v/{token}/frames")
            assert head2.startswith("HTTP/1.1 503"), "the cap"
            assert b"too many viewers" in await asyncio.wait_for(r2.read(200), 2)
            w2.close()
            writer.close()
            assert await _until(lambda: upstream.closed == 1), "quern-media let go"
            _, w3, head3 = await _open_stream(port, f"/v/{token}/frames")
            assert head3.startswith("HTTP/1.1 200"), "the slot came back"
            w3.close()
        finally:
            await viewer.stop()
            upstream.close()

    async def test_streams_are_capped_over_all_recordings(self, monkeypatch):
        from server.viewer import app as app_mod

        monkeypatch.setattr(app_mod, "MAX_STREAMS", 2)
        upstream = _FakeQuernMedia(hold=True)
        recs = _Recordings(**{f"r{i}": _recording(port=upstream.port) for i in range(3)})
        viewer, port = await self._viewer(recs)
        writers = []
        try:
            heads = []
            for i in range(3):
                r, w, head = await _open_stream(port, f"/v/{viewer.grants.issue(f'r{i}')}/frames")
                writers.append(w)
                heads.append(head.split("\r\n", 1)[0])
            # The app's cap, not uvicorn's own 503.
            assert b"too many viewers" in await asyncio.wait_for(r.read(200), 2)
            assert heads == [
                "HTTP/1.1 200 OK",
                "HTTP/1.1 200 OK",
                "HTTP/1.1 503 Service Unavailable",
            ]
        finally:
            for w in writers:
                w.close()
            await viewer.stop()
            upstream.close()

    async def test_stop_ends_open_streams_quickly_and_quietly(self):
        import logging

        errors = []
        handler = logging.Handler(logging.ERROR)
        handler.emit = errors.append
        logging.getLogger("uvicorn.error").addHandler(handler)
        upstream = _FakeQuernMedia(hold=True)
        viewer, port = await self._viewer(_Recordings(r1=_recording(port=upstream.port)))
        try:
            reader, writer, head = await _open_stream(
                port, f"/v/{viewer.grants.issue('r1')}/frames"
            )
            assert head.startswith("HTTP/1.1 200")
            began = time.monotonic()
            await viewer.stop()
            assert time.monotonic() - began < 1.5, "ended, not waited out"
            assert await _ends(reader, 1), "the stream was ended"
            assert [r.getMessage() for r in errors] == [], "no traceback per open stream"
            writer.close()
        finally:
            logging.getLogger("uvicorn.error").removeHandler(handler)
            upstream.close()

    async def test_idle_and_half_sent_connections_are_closed(self, monkeypatch):
        from server.viewer import protocol

        monkeypatch.setattr(protocol, "REQUEST_TIMEOUT", 0.3)
        viewer, port = await self._viewer(_Recordings())
        try:
            idle_r, idle_w = await asyncio.open_connection("127.0.0.1", port)
            slow_r, slow_w = await asyncio.open_connection("127.0.0.1", port)
            slow_w.write(b"GET /v/x/ HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # never finished
            await slow_w.drain()
            assert await asyncio.wait_for(idle_r.read(), 3) == b""
            assert await asyncio.wait_for(slow_r.read(), 3) == b""
            idle_w.close()
            slow_w.close()
            # A complete request in time is answered as before.
            _, w, head = await _open_stream(port, "/v/nope/")
            assert head.startswith("HTTP/1.1 404")
            w.close()
        finally:
            await viewer.stop()

    async def test_a_stray_byte_after_a_response_does_not_hold_the_connection(self, monkeypatch):
        """uvicorn's keep-alive timer is cancelled by any byte and re-armed
        only by a response: one byte after a response held the connection for
        ever, and 64 of them locked everyone out without a token (review)."""
        from server.viewer import protocol

        monkeypatch.setattr(protocol, "REQUEST_TIMEOUT", 0.3)
        viewer, port = await self._viewer(_Recordings())
        try:
            reader, writer, head = await _open_stream(port, "/v/nope/")
            assert head.startswith("HTTP/1.1 404")
            await reader.readexactly(len("not found"))
            writer.write(b"G")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 3) == b""
            writer.close()
        finally:
            await viewer.stop()

    async def test_a_silent_upstream_times_out_and_frees_its_slot(self, monkeypatch):
        from server.viewer import app as app_mod

        monkeypatch.setattr(app_mod, "UPSTREAM_TIMEOUT", 0.3)
        monkeypatch.setattr(app_mod, "MAX_STREAMS_PER_RECORDING", 1)
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen()  # accepts, never answers
        recs = _Recordings(r1=_recording(port=silent.getsockname()[1]))
        viewer, port = await self._viewer(recs)
        try:
            token = viewer.grants.issue("r1")
            for _ in range(2):  # the second gets past the cap of 1
                reader, writer, head = await _open_stream(port, f"/v/{token}/frames")
                assert head.startswith("HTTP/1.1 502"), head
                assert b"timed out" in await asyncio.wait_for(reader.read(200), 2)
                writer.close()
        finally:
            await viewer.stop()
            silent.close()

    @pytest.mark.parametrize("status", [404, 500])
    async def test_an_upstream_refusal_frees_its_slot(self, monkeypatch, status):
        from server.viewer import app as app_mod

        monkeypatch.setattr(app_mod, "MAX_STREAMS_PER_RECORDING", 1)
        upstream = _FakeQuernMedia(status=status)
        viewer, port = await self._viewer(_Recordings(r1=_recording(port=upstream.port)))
        try:
            token = viewer.grants.issue("r1")
            for _ in range(2):
                _, writer, head = await _open_stream(port, f"/v/{token}/frames")
                assert head.startswith("HTTP/1.1 502"), head
                writer.close()
            assert len(upstream.paths) == 2, "the second was not refused by the cap"
        finally:
            await viewer.stop()
            upstream.close()

    async def test_an_unreachable_upstream_frees_its_slot(self, monkeypatch):
        from server.viewer import app as app_mod

        monkeypatch.setattr(app_mod, "MAX_STREAMS_PER_RECORDING", 1)
        viewer, port = await self._viewer(_Recordings(r1=_recording(port=_free_port())))
        try:
            token = viewer.grants.issue("r1")
            for _ in range(2):
                reader, writer, head = await _open_stream(port, f"/v/{token}/frames")
                assert head.startswith("HTTP/1.1 502"), head
                assert b"not answering" in await asyncio.wait_for(reader.read(200), 2)
                writer.close()
        finally:
            await viewer.stop()

    async def test_connections_past_the_cap_are_refused(self, monkeypatch):
        from server.viewer import protocol

        monkeypatch.setattr(protocol, "MAX_CONNECTIONS", 3)
        viewer, port = await self._viewer(_Recordings())
        held = []
        try:
            # The last connection the cap admits is answered, not given
            # uvicorn's own 503 (its limit_concurrency counted itself).
            for _ in range(2):
                held.append(await asyncio.open_connection("127.0.0.1", port))
            _, w, head = await _open_stream(port, "/v/nope/")
            assert head.startswith("HTTP/1.1 404"), head
            w.close()
            await asyncio.sleep(0.2)
            for _ in range(1):
                held.append(await asyncio.open_connection("127.0.0.1", port))
            await asyncio.sleep(0.1)
            extra_r, extra_w = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(extra_r.read(), 2) == b"", "closed at once"
            extra_w.close()
            for _, w in held:
                w.close()
            await asyncio.sleep(0.2)
            _, w, head = await _open_stream(port, "/v/nope/")
            assert head.startswith("HTTP/1.1 404"), "room again once they left"
            w.close()
        finally:
            await viewer.stop()

    async def test_the_listener_leaves_logging_alone(self, monkeypatch):
        """uvicorn's access_log=False empties the process-wide uvicorn.access
        logger -- the main server's -- so the viewer must not use it (review).
        Set to a known state first: the test must not depend on what ran
        before it."""
        import logging

        from server.viewer.protocol import GuardedH11Protocol

        records = []
        handler = logging.Handler()
        handler.emit = records.append
        access = logging.getLogger("uvicorn.access")
        monkeypatch.setattr(access, "handlers", [handler])
        monkeypatch.setattr(access, "propagate", True)
        # setLevel, not the attribute: it clears the logger's isEnabledFor
        # cache, and a level cached by an earlier test made INFO read as off
        # -- so no access line could be seen and the check could not fail.
        monkeypatch.setattr(access, "level", access.level)  # restored after
        access.setLevel(logging.INFO)
        monkeypatch.setattr(access, "_cache", {})
        names = ("uvicorn", "uvicorn.error", "uvicorn.access", "")

        def state():
            return {
                n: (
                    logging.getLogger(n).level,
                    list(logging.getLogger(n).handlers),
                    logging.getLogger(n).propagate,
                )
                for n in names
            }

        before = state()
        viewer, port = await self._viewer(_Recordings())
        try:
            assert state() == before
            cfg = viewer._server.config
            assert cfg.log_config is None and cfg.log_level is None
            assert cfg.http is GuardedH11Protocol and cfg.timeout_graceful_shutdown == 2
            _, w, head = await _open_stream(port, "/v/secret-token/")
            assert head.startswith("HTTP/1.1 404")
            w.close()
            await asyncio.sleep(0.1)
            assert records == [], "no access line: every viewer URL carries a token"
        finally:
            await viewer.stop()


class TestTheLanAddress:
    def test_looked_up_again_after_the_refresh(self):
        from server.viewer.listener import LAN_REFRESH_SECONDS, LanAddress

        answers, now = iter(["10.0.0.1", "10.0.0.2"]), [0.0]
        lan = LanAddress("0.0.0.0", lookup=lambda _h: next(answers), clock=lambda: now[0])
        assert lan() == "10.0.0.1"
        now[0] = LAN_REFRESH_SECONDS - 1
        assert lan() == "10.0.0.1", "cached"
        now[0] = LAN_REFRESH_SECONDS + 1
        assert lan() == "10.0.0.2"

    def test_links_follow_it(self):
        from server.viewer.listener import ViewerListener

        now = {"lan": "10.0.0.1"}
        viewer = ViewerListener(
            grants=ViewerGrants(_Recordings(), grace_seconds=600), port=9120, lan=lambda: now["lan"]
        )
        assert viewer.url_for("t") == "http://10.0.0.1:9120/v/t/"
        now["lan"] = "fe80::1"
        assert viewer.url_for("t") == "http://[fe80::1]:9120/v/t/"


class TestTheFdLimit:
    def _fake(self, monkeypatch, soft, hard, fail=False):
        from server.viewer import listener

        calls = []

        class R:
            RLIMIT_NOFILE = 8
            RLIM_INFINITY = -1

            @staticmethod
            def getrlimit(_):
                return soft, hard

            @staticmethod
            def setrlimit(_, value):
                if fail:
                    raise ValueError("not permitted")
                calls.append(value)

        monkeypatch.setattr(listener, "resource", R)
        return listener.raise_fd_limit, calls

    def test_raised_toward_the_target_within_the_hard_limit(self, monkeypatch):
        raise_it, calls = self._fake(monkeypatch, 256, -1)
        assert raise_it(10240) == 10240 and calls == [(10240, -1)]
        raise_it, calls = self._fake(monkeypatch, 256, 4096)
        assert raise_it(10240) == 4096 and calls == [(4096, 4096)]

    def test_never_lowered(self, monkeypatch):
        raise_it, calls = self._fake(monkeypatch, 20000, -1)
        assert raise_it(10240) == 20000 and calls == []

    def test_a_refusal_is_not_an_error(self, monkeypatch):
        raise_it, _ = self._fake(monkeypatch, 256, -1, fail=True)
        assert raise_it(10240) is None
