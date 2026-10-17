"""The LAN viewer (#449): settings, tokens, the listener's app, and the API.

The first quern listener meant to be reached from another machine, so the
refusals are tested as carefully as the happy path.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
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
    def test_absurd_tokens_are_refused_outright(self, token):
        assert ViewerGrants(_Recordings(), grace_seconds=600).recording_for(token) is None


class _FakeQuernMedia:
    """A loopback /frames server, standing in for a recording's quern-media."""

    def __init__(
        self,
        status=200,
        body=b"--quernframe\r\nContent-Type: video/h264\r\nContent-Length: 3\r\n\r\nabc\r\n",
    ):
        outer = self
        self.paths = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                self.send_response(status)
                self.send_header(
                    "Content-Type", "application/octet-stream" if status == 200 else "text/plain"
                )
                self.end_headers()
                self.wfile.write(body if status == 200 else b"another simulator is on this port")

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
        recordings=recs, grants=grants, allowed_hosts=set(hosts), jmuxer_path=jmuxer_path
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

    def test_frames_passes_a_refusal_through(self):
        upstream = _FakeQuernMedia(status=409)
        try:
            client, grants = _client(_Recordings(r1=_recording(port=upstream.port)))
            r = client.get(f"/v/{grants.issue('r1')}/frames")
            assert r.status_code == 409 and "another simulator" in r.text
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
            base_url=f"http://{LAN}:9120",
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
            base_url=f"http://{LAN}:9120",
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
