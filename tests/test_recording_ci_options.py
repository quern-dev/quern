"""Recording options for runs quern does not drive (#415, #416).

A CI suite run by XCUITest records its simulator's flows and publishes the
recording when a build fails. Two things made that work badly:

- size: 911 flows came to 18 MB, two thirds of it one API's response bodies --
  requests the recording needs and bodies it almost never does (#416);
- seek points: with video, only quern's actions asked for a keyframe, and a
  run quern does not drive has none, so jumping to the request that mattered
  could land arbitrarily far before it (#415).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.models import FlowRecord, FlowRequest, FlowResponse
from server.recording import recorder as rec_mod
from server.recording.recorder import (
    Filters,
    RecordingError,
    RecordingNotFilming,
    host_matches,
    shape_bodies,
)
from tests.test_recording import SIM, Sources, _events, _settle
from tests.test_recording_video import FakeVideo
from tests.test_recording_video import Sources as VideoSources


def _flow(status=200, body="x" * 1000, ctype="application/json", response=True,
          error=None, encoding="utf-8") -> FlowRecord:
    return FlowRecord(
        id="f1", timestamp="2026-10-04T10:00:00+00:00", error=error,
        request=FlowRequest(method="POST", url="https://api.example.com/search",
                            host="api.example.com", path="/search",
                            headers={"Content-Type": "application/json"},
                            body='{"q":"x"}', body_size=9),
        response=FlowResponse(status_code=status, headers={"Content-Type": ctype},
                              body=body, body_size=len(body), body_encoding=encoding)
        if response else None,
        simulator_udid=SIM)


def _shaped(filters: Filters, kind="flow", **flow) -> dict:
    return shape_bodies(kind, _flow(**flow).model_dump(mode="json"), filters)


class TestHostGlobs:
    @pytest.mark.parametrize("host, pattern, matches", [
        ("gs-strapi.s3.us-east-1.amazonaws.com", "*.s3.*.amazonaws.com", True),
        ("gs-strapi.s3.us-east-1.amazonaws.com", "s3.amazonaws.com", False),
        ("ec2.us-east-1.amazonaws.com", "*.s3.*.amazonaws.com", False),
        ("api.example.com", "*.example.com", True),
        ("example.com", "*.example.com", False),
        ("API.Example.com", "*.example.COM", True),
        # Plain patterns are unchanged: a domain and its subdomains, never a lookalike.
        ("api.example.com", "example.com", True),
        ("badexample.com", "example.com", False),
    ])
    def test_matching(self, host, pattern, matches):
        assert host_matches(host, [pattern]) is matches


class TestBodies:
    def test_by_default_nothing_changes(self):
        assert _shaped(Filters())["response"]["body"] == "x" * 1000

    def test_none_keeps_every_flow_and_no_body(self):
        data = _shaped(Filters(bodies="none"))
        for side in ("request", "response"):
            assert data[side]["body"] is None
            assert data[side]["body_omitted"] == "bodies=none"
        assert data["response"]["body_size"] == 1000, "the size was lost with the body"
        assert data["response"]["status_code"] == 200

    @pytest.mark.parametrize("flow, kept", [
        (dict(status=200), False),
        (dict(status=204), False),
        (dict(status=301), True),
        (dict(status=404), True),
        (dict(status=500), True),
        (dict(response=False), True),             # never answered
        (dict(status=200, error="reset"), True),  # answered, then failed
    ])
    def test_errors_keeps_what_a_failure_needs(self, flow, kept):
        data = _shaped(Filters(bodies="errors"), **flow)
        assert (data["request"]["body"] is not None) is kept
        if data.get("response"):
            assert (data["response"]["body"] is not None) is kept

    def test_errors_keeps_a_starts_request_body(self):
        """At its start nothing is known; if no answer comes, it is the only line."""
        data = _shaped(Filters(bodies="errors"), kind="request_started", response=False)
        assert data["request"]["body"] == '{"q":"x"}'

    def test_max_body_bytes_truncates_and_says_so(self):
        data = _shaped(Filters(max_body_bytes=10))
        assert data["response"]["body"] == "x" * 10
        assert data["response"]["body_truncated"] is True
        assert data["response"]["body_size"] == 1000

    def test_truncation_never_splits_a_character(self):
        # 2 bytes each: 5 bytes cuts the third character in half.
        data = _shaped(Filters(max_body_bytes=5), body="éééé")
        assert data["response"]["body"] == "éé"

    def test_a_truncated_base64_body_still_decodes(self):
        encoded = base64.b64encode(bytes(range(200))).decode()
        data = _shaped(Filters(max_body_bytes=10), body=encoded, encoding="base64")
        kept = data["response"]["body"]
        assert len(kept) % 4 == 0 and len(kept) <= 10
        base64.b64decode(kept)

    def test_content_types_are_excluded_by_prefix_any_case(self):
        data = _shaped(Filters(exclude_content_types=["image/"]), ctype="Image/PNG")
        assert data["response"]["body"] is None
        assert data["response"]["body_omitted"] == "content type excluded"
        assert data["request"]["body"] is not None, "a JSON request body was dropped"

    def test_the_options_are_validated(self):
        with pytest.raises(RecordingError, match="bodies"):
            Filters(bodies="some")
        with pytest.raises(RecordingError, match="negative"):
            Filters(max_body_bytes=-1)
        with pytest.raises(RecordingError, match="keyframes"):
            Filters(keyframes=("taps",))


class TestTheManifestKeepsThem:
    def test_round_trip(self):
        f = Filters(bodies="errors", max_body_bytes=512, exclude_content_types=["image/"],
                    keyframes=("requests",))
        assert Filters.from_dict(f.as_dict()) == f

    def test_a_manifest_from_before_resumes_as_it_was_recorded(self):
        """Before #415 only actions made keyframes; a recording resumed after
        an upgrade keeps that, rather than gaining request keyframes mid-run."""
        old = {"kinds": ["flows"], "hosts": None, "exclude_hosts": None,
               "include_unattributed": False, "video": True}
        f = Filters.from_dict(old)
        assert f.keyframes == ("actions",)
        assert (f.bodies, f.max_body_bytes, f.exclude_content_types) == ("all", None, None)


class TestThroughTheRecorder:
    async def test_bodies_are_shaped_in_the_file(self, tmp_path: Path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("flows",),
                                                                    bodies="errors"))
        await src.flows.add(_flow(status=200))
        await src.flows.add(_flow(status=500))
        await _settle()
        await manager.stop(rec.id)
        flows = [e["data"] for e in _events(tmp_path / "r") if e["type"] == "flow"]
        assert [f["response"]["body"] is not None for f in flows] == [False, True]


class TestRequestKeyframes:
    async def _filming(self, tmp_path, **filters):
        video = FakeVideo()
        src = VideoSources()
        manager = src.manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True, **filters))
        return src, manager, rec, video

    async def test_a_request_makes_a_seek_point_once_a_second(self, tmp_path, monkeypatch):
        src, manager, rec, video = await self._filming(tmp_path)
        clock = [100.0]
        # The recorder's own seam: patching time.monotonic would freeze asyncio's clock.
        monkeypatch.setattr(rec_mod, "_monotonic", lambda: clock[0])
        for _ in range(5):                       # a burst: one keyframe
            src.flows.note_started(_flow(response=False))
        await _settle()
        assert len(video.keyframes) == 1
        clock[0] += rec_mod.REQUEST_KEYFRAME_INTERVAL
        src.flows.note_started(_flow(response=False))
        await _settle()
        assert len(video.keyframes) == 2
        await manager.stop(rec.id)

    @pytest.mark.parametrize("first", ["action", "caller"])
    async def test_a_request_just_after_another_keyframe_adds_none(
        self, tmp_path, monkeypatch, first,
    ):
        """The requests an action sets off start just after it: their keyframe
        is the action's, already there. Measured, 7 of 12 were."""
        src, manager, rec, video = await self._filming(tmp_path)
        clock = [100.0]
        monkeypatch.setattr(rec_mod, "_monotonic", lambda: clock[0])
        if first == "action":
            manager._on_action_device(SIM, object())
        else:
            await manager.keyframe(rec.id)
        clock[0] += rec_mod.REQUEST_KEYFRAME_INTERVAL / 2
        src.flows.note_started(_flow(response=False))
        await _settle()
        assert len(video.keyframes) == 1
        clock[0] += rec_mod.REQUEST_KEYFRAME_INTERVAL
        src.flows.note_started(_flow(response=False))
        await _settle()
        assert len(video.keyframes) == 2, "a request between actions is a seek point"
        await manager.stop(rec.id)

    async def test_an_action_just_after_a_request_still_gets_one(self, tmp_path, monkeypatch):
        """The action is the seek point that matters; requests yield to it,
        never the other way round."""
        src, manager, rec, video = await self._filming(tmp_path)
        clock = [100.0]
        monkeypatch.setattr(rec_mod, "_monotonic", lambda: clock[0])
        src.flows.note_started(_flow(response=False))
        await _settle()
        clock[0] += rec_mod.REQUEST_KEYFRAME_INTERVAL / 10
        manager._on_action_device(SIM, object())
        await _settle()
        assert len(video.keyframes) == 2
        await manager.stop(rec.id)

    async def test_actions_only_asks_none_for_requests(self, tmp_path):
        src, manager, rec, video = await self._filming(tmp_path, keyframes=("actions",))
        src.flows.note_started(_flow(response=False))
        await _settle()
        assert video.keyframes == []
        await manager.stop(rec.id)

    async def test_requests_only_asks_none_for_actions(self, tmp_path):
        src, manager, rec, video = await self._filming(tmp_path, keyframes=("requests",))
        manager._on_action_device(SIM, object())
        await _settle()
        assert video.keyframes == []
        await manager.stop(rec.id)

    async def test_an_outside_driver_can_ask(self, tmp_path):
        src, manager, rec, video = await self._filming(tmp_path)
        assert await manager.keyframe(rec.id) is True
        assert len(video.keyframes) == 1
        await manager.stop(rec.id)
        with pytest.raises(RecordingNotFilming):
            await manager.keyframe(rec.id)

    async def test_a_recording_without_video_cannot_be_asked(self, tmp_path):
        manager = Sources().manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        with pytest.raises(RecordingNotFilming, match="not recording video"):
            await manager.keyframe(rec.id)
        await manager.stop(rec.id)


class TestTheRoutesAndTheCli:
    def _client(self):
        from tests.test_recording import _app
        return TestClient(_app(Sources()))

    def test_the_options_reach_the_recording(self, tmp_path):
        with self._client() as client:
            r = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "bodies": "errors",
                "max_body_bytes": 64, "exclude_content_types": ["image/"],
                "keyframes": ["requests"]})
            assert r.status_code == 200, r.text
            filters = r.json()["filters"]
            assert (filters["bodies"], filters["max_body_bytes"]) == ("errors", 64)
            assert filters["exclude_content_types"] == ["image/"]
            assert filters["keyframes"] == ["requests"]
            assert client.post("/api/v1/recordings", json={
                "udid": SIM, "bodies": "some"}).status_code == 422

    def test_the_keyframe_route(self, tmp_path):
        with self._client() as client:
            assert client.post("/api/v1/recordings/rec_nope/keyframe").status_code == 404
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r")}).json()["id"]
            r = client.post(f"/api/v1/recordings/{rid}/keyframe")
            assert r.status_code == 409 and "not recording video" in r.json()["detail"]

    def test_the_cli_sends_the_options(self):
        from server.recording import cli

        sent = {}

        def call(method, path, body=None):
            sent.update(method=method, path=path, body=body)
            return 200, {"id": "rec_1", "udid": SIM, "output_dir": "/x", "warnings": []}

        with patch.object(cli, "_call", call):
            assert cli.main(["start", "--udid", SIM, "--bodies", "errors",
                             "--max-body-bytes", "64", "--exclude-content-type", "image/",
                             "--keyframes", "requests"]) == 0
        assert sent["body"]["bodies"] == "errors"
        assert sent["body"]["max_body_bytes"] == 64
        assert sent["body"]["exclude_content_types"] == ["image/"]
        assert sent["body"]["keyframes"] == ["requests"]

    def test_the_cli_keyframe(self, capsys):
        from server.recording import cli

        with patch.object(cli, "_call", lambda m, p, b=None: (200, {"requested": True})):
            assert cli.main(["keyframe", "rec_1"]) == 0
        with patch.object(cli, "_call", lambda m, p, b=None: (409, {"detail": "not filming"})):
            assert cli.main(["keyframe", "rec_1"]) == 2
        assert "not filming" in capsys.readouterr().err

    def test_a_structured_refusal_reads_as_words(self, capsys):
        """#414's 428 carries a message and its ways out; printed as a dict it
        was the least useful way to say it."""
        from server.recording import cli

        detail = {"message": "iPhone 17e does not trust the CA.",
                  "resolutions": [{"action": "install_proxy_cert"},
                                  {"action": "allow_passthrough"}]}
        with patch.object(cli, "_call", lambda m, p, b=None: (428, {"detail": detail})):
            assert cli.main(["start", "--udid", SIM]) == 2
        err = capsys.readouterr().err
        assert "does not trust the CA" in err and "install_proxy_cert" in err
        assert "{" not in err


class TestTheReviewFindings:
    async def test_drain_shapes_what_the_pumps_had_not_taken(self, tmp_path):
        """`stop()` drains what is still queued; that path wrote bodies whole."""
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("flows",),
                                                                    bodies="none"))
        src.flows._fanout.publish(_flow(status=200))   # no await: the pump has not run
        before = len(rec._pending)
        manager._drain(rec)
        drained = [json.loads(line) for line in rec._pending[before:]]
        assert drained and drained[0]["type"] == "flow"
        assert drained[0]["data"]["response"]["body"] is None
        await manager.stop(rec.id)

    def test_content_type_patterns_are_case_blind_and_blanks_ignored(self):
        data = _shaped(Filters(exclude_content_types=["Image/"]), ctype="image/png")
        assert data["response"]["body"] is None
        kept = _shaped(Filters(exclude_content_types=["", "  "]))
        assert kept["response"]["body"] is not None, "an empty pattern matched everything"

    async def test_a_start_line_is_shaped_too(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("flows",),
                                                                    bodies="none"))
        src.flows.note_started(_flow(response=False))
        await _settle()
        await manager.stop(rec.id)
        starts = [e["data"] for e in _events(tmp_path / "r") if e["type"] == "request_started"]
        assert starts and starts[0]["request"]["body"] is None

    async def test_a_flow_whose_start_was_never_seen_is_a_seek_point(self, tmp_path,
                                                                     monkeypatch):
        """A mocked request never reports a start, and a fast response can
        beat its start report; either way it was a request (review)."""
        video = FakeVideo()
        src = VideoSources()
        manager = src.manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        clock = [100.0]
        monkeypatch.setattr(rec_mod, "_monotonic", lambda: clock[0])
        await src.flows.add(_flow())
        await _settle()
        assert len(video.keyframes) == 1
        clock[0] += rec_mod.REQUEST_KEYFRAME_INTERVAL
        started = _flow(response=False)
        src.flows.note_started(started)
        await _settle()
        clock[0] += rec_mod.REQUEST_KEYFRAME_INTERVAL
        await src.flows.add(_flow())                  # same id as the start: seen
        await _settle()
        assert len(video.keyframes) == 2
        await manager.stop(rec.id)

    async def test_an_outside_keyframe_leaves_a_mark(self, tmp_path):
        video = FakeVideo()
        manager = VideoSources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await manager.keyframe(rec.id, label="test_login step 3")
        await manager.stop(rec.id)
        loaded = rec_mod.load(tmp_path / "r")
        assert [m["label"] for m in loaded.marks] == ["test_login step 3"]
        page = rec_mod.read_events(tmp_path / "r", ("actions",), detail="summary")
        marks = [e for e in page["events"] if e["type"] == "mark"]
        assert marks[0]["data"]["label"] == "test_login step 3"
        assert marks[0]["data"]["keyframe_requested"] is True

    def test_the_keyframe_route_answers_and_marks(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        from server.models import DeviceType
        from tests.test_recording import _app
        from tests.test_recording_video import Sources as VSources

        vsrc = VSources()
        app = _app(Sources())
        app.state.recordings = vsrc.manager(FakeVideo())
        controller = MagicMock()        # video is refused for anything but a simulator
        controller._ensure_device_type_cached = AsyncMock()
        controller._device_type = MagicMock(return_value=DeviceType.SIMULATOR)
        app.state.device_controller = controller
        with TestClient(app) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "video": True}).json()["id"]
            r = client.post(f"/api/v1/recordings/{rid}/keyframe", json={"label": "boom"})
            assert r.status_code == 200 and r.json()["requested"] is True
            client.post(f"/api/v1/recordings/{rid}/stop")
            marks = [e["data"] for e in _events(tmp_path / "r") if e["type"] == "mark"]
            assert [m["label"] for m in marks] == ["boom"], "the label did not reach the file"
            r = client.post(f"/api/v1/recordings/{rid}/keyframe")
            assert r.status_code == 409, "a stopped recording is not filming"

    def test_the_cli_sends_a_label_and_reads_a_422(self, capsys):
        from server.recording import cli

        sent = {}

        def call(method, path, body=None):
            sent.update(body=body)
            return 200, {"requested": True}

        with patch.object(cli, "_call", call):
            assert cli.main(["keyframe", "rec_1", "--label", "step 3"]) == 0
        assert sent["body"] == {"label": "step 3"}
        detail = [{"loc": ["body", "keyframes", 0],
                   "msg": "Input should be 'actions' or 'requests'"}]
        with patch.object(cli, "_call", lambda m, p, b=None: (422, {"detail": detail})):
            assert cli.main(["start", "--udid", SIM, "--keyframes", "taps"]) == 2
        err = capsys.readouterr().err
        assert "keyframes.0: Input should be" in err and "'loc'" not in err
